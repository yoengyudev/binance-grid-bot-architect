import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr
from decimal import Decimal, ROUND_DOWN
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import ccxt

import main as grid_main
from database import GridDatabase
from main import (
    GridBot, GridConfig, TradingHalt, UncertainOrderError, _apply_active_grid_config,
    geometric_levels,
)


class FakeSpotExchange:
    def __init__(self) -> None:
        self.price = Decimal("100")
        self.base_free = Decimal("1")  # Pre-existing account inventory.
        self.quote_free = Decimal("10000")
        self.orders = {}
        self.next_id = 1
        self.fail_create = False
        self.reject_post_only_once = False
        self.reject_post_only_side = None
        self.cancel_all_calls = 0
        self.cancel_all_timeout = None
        self.market_sell_calls = 0
        self.market_sell_timeout = None
        self.market_sell_insufficient = False
        self.market_sell_crash_after_accept = False

    def load_markets(self) -> None:
        pass

    def market(self, _symbol: str) -> dict:
        return {
            "spot": True, "active": True, "base": "BTC", "quote": "USDT",
            "limits": {"amount": {"min": 0.001}, "cost": {"min": 10}},
        }

    def fetch_ticker(self, _symbol: str) -> dict:
        return {"last": str(self.price)}

    def price_to_precision(self, _symbol: str, price: str) -> str:
        return str(Decimal(price).quantize(Decimal("0.01"), rounding=ROUND_DOWN))

    def amount_to_precision(self, _symbol: str, amount: str) -> str:
        return str(Decimal(amount).quantize(Decimal("0.001"), rounding=ROUND_DOWN))

    def fetch_balance(self, _params: dict) -> dict:
        return {"BTC": {"free": str(self.base_free)},
                "USDT": {"free": str(self.quote_free)}}

    def create_order(
        self, _symbol: str, order_type: str, side: str,
        amount: float, price: float, params: dict,
    ) -> dict:
        if self.fail_create:
            raise RuntimeError("Simulated uncertain response")
        if (self.reject_post_only_once and order_type == "limit" and
                (self.reject_post_only_side is None or side == self.reject_post_only_side)):
            self.reject_post_only_once = False
            raise ccxt.OrderImmediatelyFillable(
                'binance {"code":-2010,"msg":"Order would immediately match and take."}'
            )
        order_id = str(self.next_id)
        self.next_id += 1
        client_id = params["newClientOrderId"]
        quantity = Decimal(str(amount))
        status = "closed" if order_type == "market" else "open"
        execution_price = self.price if status == "closed" else Decimal(str(price))
        cost = Decimal(str(params.get("quoteOrderQty", quantity * execution_price)))
        if side == "buy":
            self.quote_free -= cost
            if status == "closed":
                self.base_free += quantity
        else:
            self.base_free -= quantity
            if status == "closed":
                self.quote_free += cost
        self.orders[client_id] = {
            "id": order_id,
            "clientOrderId": client_id,
            "side": side,
            "type": order_type,
            "amount": str(quantity),
            "status": status,
            "filled": str(quantity if status == "closed" else 0),
            "average": str(execution_price),
            "price": str(price) if price is not None else None,
            "cost": str(cost),
            "params": dict(params),
            "fees": [],
        }
        return dict(self.orders[client_id])

    def fetch_order(self, order_id: str, _symbol: str, params: dict = None) -> dict:
        if params:
            try:
                return dict(self.orders[params["origClientOrderId"]])
            except KeyError as error:
                raise ccxt.OrderNotFound("Simulated missing client order") from error
        for order in self.orders.values():
            if order["id"] == order_id:
                return dict(order)
        raise KeyError(order_id)

    def cancel_order(self, order_id: str, symbol: str, params: dict = None) -> dict:
        order = self.fetch_order(order_id, symbol, params)
        if order["status"] == "open":
            self.orders[order["clientOrderId"]]["status"] = "canceled"
            if order["side"] == "buy":
                self.quote_free += Decimal(order["cost"])
            else:
                self.base_free += Decimal(order["amount"]) - Decimal(order["filled"])
        return dict(self.orders[order["clientOrderId"]])

    def fetch_open_orders(self, _symbol: str) -> list:
        return [dict(order) for order in self.orders.values()
                if order["status"] == "open"]

    def cancel_all_orders(self, symbol: str) -> list:
        self.cancel_all_calls += 1
        if self.cancel_all_timeout == "before":
            self.cancel_all_timeout = None
            raise ccxt.RequestTimeout("Simulated cancel request timeout")
        results = [self.cancel_order(order["id"], symbol)
                   for order in self.fetch_open_orders(symbol)]
        if self.cancel_all_timeout == "after":
            self.cancel_all_timeout = None
            raise ccxt.RequestTimeout("Simulated cancel response timeout")
        return results

    def create_market_sell_order(self, symbol: str, amount: float,
                                 params: dict = None) -> dict:
        self.market_sell_calls += 1
        if self.market_sell_insufficient:
            raise ccxt.InsufficientFunds("Simulated insufficient BTC")
        if self.market_sell_timeout == "before":
            self.market_sell_timeout = None
            raise ccxt.RequestTimeout("Simulated request timeout")
        response = self.create_order(symbol, "market", "sell", amount, None, params or {})
        if self.market_sell_crash_after_accept:
            self.market_sell_crash_after_accept = False
            raise KeyboardInterrupt("Simulated process interruption after acceptance")
        if self.market_sell_timeout == "after":
            self.market_sell_timeout = None
            raise ccxt.RequestTimeout("Simulated response timeout")
        return response

    def fill(self, client_id: str) -> None:
        order = self.orders[client_id]
        order["status"] = "closed"
        order["filled"] = order["amount"]
        if order["side"] == "buy":
            self.base_free += Decimal(order["filled"])
        else:
            self.quote_free += Decimal(order["cost"])


class GridBotTests(unittest.TestCase):
    def test_configured_trailing_width_survives_runtime_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config_file = path / "config.json"
            config_file.write_text(json.dumps({
                "exchange": {"name": "binance", "market_type": "spot", "sandbox": True},
                "grid": {
                    "symbol": "BTC/USDT", "investment_quote": 1000,
                    "lower_price": 80, "upper_price": 120,
                    "auto_center_percent": 15, "spacing_percent": 10,
                    "initial_inventory_percent": 50, "stop_loss_price": 70,
                    "poll_seconds": 2,
                },
            }), encoding="utf-8")
            database = GridDatabase(path / "grid.sqlite3")
            database.set_state("active_grid_config", json.dumps({
                "lower": "90", "upper": "130", "spacing": "2.5",
                "stop_loss_price": "70",
            }))
            config = _apply_active_grid_config(GridConfig.load(config_file), database)
            bot = GridBot(config, FakeSpotExchange(), database)
            self.assertEqual(bot.breakout_width_percent, Decimal("15"))
            self.assertEqual(bot.config.spacing_percent, Decimal("2.5"))

    def test_process_exit_codes_separate_retry_from_safety_halt(self) -> None:
        for error, expected in (
            (ccxt.NetworkError("Temporary network failure"), 1),
            (ccxt.AuthenticationError("Invalid API credentials"), 2),
            (TradingHalt("Order state requires review"), 2),
            (ValueError("Invalid configuration"), 2),
            (KeyboardInterrupt(), 130),
        ):
            with self.subTest(error=type(error).__name__):
                with (patch("sys.argv", ["main.py", "--execute"]),
                      patch.object(grid_main, "_credentials", side_effect=error),
                      patch.object(grid_main.LOGGER, "addHandler"),
                      redirect_stderr(StringIO())):
                    self.assertEqual(grid_main.main(), expected)

    def make_bot(self, path: Path, *, width: Decimal = None):
        config = GridConfig(
            "BTC/USDT", Decimal("1000"), Decimal("80"), Decimal("120"),
            Decimal("10"), Decimal("50"), Decimal("70"), 2, width,
        )
        exchange = FakeSpotExchange()
        bot = GridBot(config, exchange, GridDatabase(path))
        bot.prepare(persist=True)
        return bot, exchange

    def test_market_price_and_confirmed_fills_support_portfolio_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            self.assertEqual(
                bot.database.get_state(grid_main.LAST_MARKET_PRICE_KEY), "100"
            )
            bot.run_cycle()
            bot.run_cycle()
            seed = bot.database.fetch_latest_orders_by_level()[0]
            snapshot = json.loads(bot.database.get_state(
                grid_main.FILL_SNAPSHOT_PREFIX + seed["order_id"]
            ))
            self.assertEqual(Decimal(snapshot["filled_base"]), Decimal("5"))
            self.assertEqual(Decimal(snapshot["filled_quote"]), Decimal("500"))
            self.assertEqual(grid_main._portfolio_wallet(bot.database)["btc_held"], 5.0)
            exchange.price = Decimal("105")
            bot.run_cycle()
            self.assertEqual(
                bot.database.get_state(grid_main.LAST_MARKET_PRICE_KEY), "105"
            )
            self.assertEqual(
                grid_main._portfolio_wallet(bot.database)["unrealized_pnl"], 25.0
            )

    def test_geometric_levels_and_buy_sell_cycle(self) -> None:
        self.assertEqual(
            geometric_levels(Decimal("100"), Decimal("80"), Decimal("10")),
            [Decimal("90.0"), Decimal("81.00")],
        )
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            first_buy = bot.database.fetch_latest_orders_by_level()[1]
            self.assertEqual(first_buy["side"], "BUY")
            seed = bot.database.fetch_latest_orders_by_level()[0]
            self.assertEqual(seed["status"], "FILLED")
            self.assertEqual(
                exchange.orders[seed["client_order_id"]]["params"]["quoteOrderQty"],
                "500",
            )
            self.assertNotIn(
                "timeInForce", exchange.orders[seed["client_order_id"]]["params"]
            )
            for order in exchange.orders.values():
                if order["type"] == "limit":
                    self.assertEqual(order["params"]["timeInForce"], "PO")
            self.assertEqual(bot.database.fetch_latest_orders_by_level()[-1]["side"], "SELL")
            exchange.fill(first_buy["client_order_id"])

            events = bot.run_cycle()
            self.assertEqual(events[0][0], "filled")
            sell = bot.database.fetch_latest_orders_by_level()[1]
            self.assertEqual(sell["side"], "SELL")
            self.assertEqual(Decimal(sell["price"]), Decimal("100.00"))
            exchange.fill(sell["client_order_id"])

            bot.run_cycle()
            self.assertEqual(bot.database.fetch_latest_orders_by_level()[1]["side"], "BUY")
            self.assertEqual(len(bot.database.fetch_trade_history()), 1)
            self.assertEqual(bot.database.get_state("grid_run") is not None, True)

    def test_upper_sell_rearms_buy_then_sell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            upper_sell = bot.database.fetch_latest_orders_by_level()[-1]
            exchange.fill(upper_sell["client_order_id"])

            bot.run_cycle()
            upper_buy = bot.database.fetch_latest_orders_by_level()[-1]
            self.assertEqual(upper_buy["side"], "BUY")
            self.assertEqual(Decimal(upper_buy["price"]), Decimal("100.00"))
            self.assertEqual(len(bot.database.fetch_trade_history()), 1)

            exchange.fill(upper_buy["client_order_id"])
            bot.run_cycle()
            next_sell = bot.database.fetch_latest_orders_by_level()[-1]
            self.assertEqual(next_sell["side"], "SELL")
            self.assertEqual(Decimal(next_sell["price"]), Decimal("110.00"))

    def test_hard_stop_cancels_all_and_sells_only_bot_btc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.sqlite3"
            bot, exchange = self.make_bot(path)
            bot.run_cycle()
            bot.run_cycle()
            held_btc = exchange.base_free + sum(
                Decimal(order["amount"]) for order in exchange.fetch_open_orders("BTC/USDT")
                if order["side"] == "sell"
            ) - Decimal("1")
            exchange.price = Decimal("69")

            bot.run_cycle()
            self.assertTrue(bot.is_paused)
            self.assertEqual(bot.database.get_state("safety_mode"), grid_main.LIQUIDATED)
            self.assertEqual(bot.database.get_state("halt_reason"), "hard_stop_liquidated")
            self.assertFalse(bot.stop_controller.stop_requested.is_set())
            self.assertEqual(exchange.cancel_all_calls, 1)
            self.assertEqual(exchange.fetch_open_orders("BTC/USDT"), [])
            self.assertEqual(exchange.market_sell_calls, 1)
            sale = [row for row in exchange.orders.values()
                    if row["type"] == "market" and row["side"] == "sell"][0]
            self.assertLessEqual(Decimal(sale["amount"]), held_btc)
            self.assertEqual(exchange.base_free, Decimal("1") + held_btc -
                             Decimal(sale["amount"]))
            notifier = SimpleNamespace(notify_hard_stop=AsyncMock())
            asyncio.run(bot._notify_liquidation(notifier))
            notifier.notify_hard_stop.assert_awaited_once_with(True)
            before = len(exchange.orders)
            exchange.price = Decimal("100")
            bot.run_cycle()
            self.assertEqual(len(exchange.orders), before)

            reopened = GridBot(bot.config, exchange, GridDatabase(path))
            reopened.prepare(persist=False)
            self.assertTrue(reopened.is_paused)
            reopened.run_cycle()
            self.assertEqual(len(exchange.orders), before)
            with self.assertRaises(TradingHalt):
                reopened.set_manual_pause(False)

    def test_manual_pause_cancels_buys_keeps_sells_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.sqlite3"
            bot, exchange = self.make_bot(path)
            bot.run_cycle()
            bot.run_cycle()
            sell_ids = {order["id"] for order in exchange.fetch_open_orders("BTC/USDT")
                        if order["side"] == "sell"}
            self.assertTrue(any(order["side"] == "buy" for order in
                                exchange.fetch_open_orders("BTC/USDT")))

            self.assertEqual(bot.set_manual_pause(True), grid_main.PAUSED_MANUAL)
            self.assertFalse(any(order["side"] == "buy" for order in
                                 exchange.fetch_open_orders("BTC/USDT")))
            self.assertEqual({order["id"] for order in
                              exchange.fetch_open_orders("BTC/USDT")
                              if order["side"] == "sell"}, sell_ids)
            exchange.price = Decimal("91")
            bot.run_cycle()
            self.assertTrue(bot.is_paused)

            reopened = GridBot(bot.config, exchange, GridDatabase(path))
            reopened.prepare(persist=False)
            self.assertTrue(reopened.is_paused)
            reopened.run_cycle()
            self.assertEqual(reopened.database.get_state("safety_mode"),
                             grid_main.PAUSED_MANUAL)
            self.assertIsNone(reopened.set_manual_pause(False))
            reopened.run_cycle()
            self.assertFalse(reopened.is_paused)
            self.assertTrue(any(order["side"] == "buy" for order in
                                exchange.fetch_open_orders("BTC/USDT")))

    def test_manual_unpause_respects_downside_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            bot.set_manual_pause(True)
            exchange.price = Decimal("69")
            self.assertEqual(bot.set_manual_pause(False), grid_main.PAUSED_DOWNSIDE)
            self.assertTrue(bot.is_paused)
            bot.run_cycle()
            self.assertEqual(bot.database.get_state(grid_main.SAFETY_MODE_KEY),
                             grid_main.LIQUIDATED)

    def test_hard_stop_includes_partially_filled_buy_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            buy = bot.database.fetch_latest_orders_by_level()[1]
            partial = Decimal(buy["amount"]) / 2
            exchange.orders[buy["client_order_id"]]["filled"] = str(partial)
            exchange.base_free += partial
            exchange.price = Decimal("69")

            bot.run_cycle()

            state = json.loads(bot.database.get_state(grid_main.LIQUIDATION_KEY))
            self.assertEqual(bot.database.get_state(grid_main.SAFETY_MODE_KEY),
                             grid_main.LIQUIDATED)
            self.assertGreater(Decimal(state["sold_base"]), partial)
            self.assertEqual(exchange.fetch_open_orders("BTC/USDT"), [])

    def test_hard_stop_insufficient_funds_halts_without_resubmitting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            exchange.market_sell_insufficient = True
            exchange.price = Decimal("69")
            bot.run_cycle()
            self.assertEqual(bot.database.get_state(grid_main.SAFETY_MODE_KEY),
                             grid_main.LIQUIDATION_HALTED)
            self.assertEqual(exchange.market_sell_calls, 1)
            self.assertEqual(exchange.fetch_open_orders("BTC/USDT"), [])
            bot.run_cycle()
            self.assertEqual(exchange.market_sell_calls, 1)
            notifier = SimpleNamespace(notify_hard_stop=AsyncMock())
            asyncio.run(bot._notify_liquidation(notifier))
            notifier.notify_hard_stop.assert_awaited_once_with(False)

    def test_hard_stop_retries_only_unaccepted_timed_out_sell(self) -> None:
        for timeout, expected_calls in (("before", 2), ("after", 1)):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as directory:
                bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
                bot.run_cycle()
                bot.run_cycle()
                exchange.market_sell_timeout = timeout
                exchange.price = Decimal("69")
                bot.run_cycle()
                self.assertEqual(exchange.market_sell_calls, expected_calls)
                self.assertEqual(bot.database.get_state(grid_main.SAFETY_MODE_KEY),
                                 grid_main.LIQUIDATED)
                self.assertEqual(len([row for row in exchange.orders.values()
                                      if row["type"] == "market" and row["side"] == "sell"]), 1)

    def test_hard_stop_verifies_timed_out_cancel_all(self) -> None:
        for timeout, expected_calls in (("before", 2), ("after", 1)):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as directory:
                bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
                bot.run_cycle()
                bot.run_cycle()
                exchange.cancel_all_timeout = timeout
                exchange.price = Decimal("69")
                bot.run_cycle()
                self.assertEqual(exchange.cancel_all_calls, expected_calls)
                self.assertEqual(bot.database.get_state(grid_main.SAFETY_MODE_KEY),
                                 grid_main.LIQUIDATED)

    def test_hard_stop_preempts_pending_grid_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            bot.request_manual_recenter("102", "20", "30")
            exchange.price = Decimal("69")
            bot.run_cycle()
            self.assertEqual(bot.database.get_state(grid_main.SAFETY_MODE_KEY),
                             grid_main.LIQUIDATED)
            self.assertIsNone(bot.database.get_state("grid_reset"))
            self.assertFalse(bot.grid_needs_reset)

    def test_hard_stop_restart_recovers_accepted_sell_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.sqlite3"
            bot, exchange = self.make_bot(path)
            bot.run_cycle()
            bot.run_cycle()
            exchange.market_sell_crash_after_accept = True
            exchange.price = Decimal("69")
            with self.assertRaises(KeyboardInterrupt):
                bot.run_cycle()
            self.assertEqual(exchange.market_sell_calls, 1)
            reopened = GridBot(bot.config, exchange, GridDatabase(path))
            reopened.prepare(persist=False)
            reopened.run_cycle()
            self.assertEqual(exchange.market_sell_calls, 1)
            self.assertEqual(reopened.database.get_state(grid_main.SAFETY_MODE_KEY),
                             grid_main.LIQUIDATED)

    def test_admin_recenter_resets_completed_liquidation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.sqlite3"
            bot, exchange = self.make_bot(path)
            bot.run_cycle()
            bot.run_cycle()
            exchange.price = Decimal("69")
            bot.run_cycle()
            with self.assertRaises(TradingHalt):
                bot.set_manual_pause(False)
            exchange.price = Decimal("100")
            self.assertEqual(bot.request_manual_recenter("102", "20", "30"),
                             (Decimal("81.6"), Decimal("122.4")))
            self.assertIsNone(bot.database.get_state(grid_main.SAFETY_MODE_KEY))
            self.assertIsNone(bot.database.get_state(grid_main.LIQUIDATION_KEY))
            self.assertIsNone(bot.database.get_state("halt_reason"))
            self.assertTrue(bot.grid_needs_reset)
            self.assertTrue(bot.reset_grid())
            self.assertFalse(bot.grid_needs_reset)
            self.assertEqual(bot.config.stop_loss_price, Decimal("71.4"))
            self.assertTrue(exchange.fetch_open_orders("BTC/USDT"))

    def test_upper_breakout_timer_resets_on_dip_gap_and_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.sqlite3"
            bot, exchange = self.make_bot(path, width=Decimal("15"))
            self.assertEqual(bot.breakout_width_percent, Decimal("15"))
            self.assertFalse(bot._observe_upper_breakout(Decimal("121"), now=0))
            self.assertFalse(bot._observe_upper_breakout(Decimal("121"), now=60))
            self.assertIsNotNone(bot.database.get_state(grid_main.BREAKOUT_TIMER_KEY))
            self.assertFalse(bot._observe_upper_breakout(Decimal("120"), now=120))
            self.assertIsNone(bot.database.get_state(grid_main.BREAKOUT_TIMER_KEY))

            self.assertFalse(bot._observe_upper_breakout(Decimal("121"), now=180))
            reopened = GridBot(bot.config, exchange, GridDatabase(path))
            reopened.prepare(persist=True)
            self.assertEqual(reopened.breakout_width_percent, Decimal("15"))
            self.assertFalse(reopened._observe_upper_breakout(Decimal("121"), now=240))
            state = json.loads(reopened.database.get_state(grid_main.BREAKOUT_TIMER_KEY))
            self.assertEqual(state["started_at"], 180)

            self.assertFalse(reopened._observe_upper_breakout(Decimal("121"), now=301))
            state = json.loads(reopened.database.get_state(grid_main.BREAKOUT_TIMER_KEY))
            self.assertEqual(state["started_at"], 301)
            for second in range(361, 301 + grid_main.BREAKOUT_COOLDOWN_SECONDS, 60):
                self.assertFalse(
                    reopened._observe_upper_breakout(Decimal("121"), now=second)
                )
            self.assertFalse(reopened.grid_needs_reset)
            self.assertTrue(reopened._observe_upper_breakout(
                Decimal("121"), now=301 + grid_main.BREAKOUT_COOLDOWN_SECONDS
            ))
            self.assertTrue(reopened.grid_needs_reset)

    def test_confirmed_breakout_recenters_without_selling_carried_btc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.sqlite3"
            bot, exchange = self.make_bot(
                path, width=Decimal("15")
            )
            bot.run_cycle()
            bot.run_cycle()
            existing_btc = exchange.base_free + sum(
                Decimal(order["amount"])
                for order in exchange.fetch_open_orders("BTC/USDT")
                if order["side"] == "sell"
            )
            old_orders = len(exchange.orders)
            exchange.price = Decimal("140")
            bot.database.set_state(grid_main.BREAKOUT_TIMER_KEY, json.dumps({
                "started_at": 0, "last_seen_at": 14340,
                "grid_fingerprint": bot.config.fingerprint(),
            }))
            with patch.object(grid_main.time, "time", return_value=14400):
                self.assertEqual(bot.run_cycle(), [])
            self.assertTrue(bot.grid_needs_reset)
            self.assertEqual(len(exchange.orders), old_orders)
            request = json.loads(bot.database.get_state("grid_reset"))
            self.assertEqual(request["source"], "breakout")

            restarted = GridBot(bot.config, exchange, GridDatabase(path))
            restarted.prepare(persist=False)
            self.assertTrue(restarted.grid_needs_reset)
            exchange.price = Decimal("145")  # Use execution-time price as the center.
            exchange.reject_post_only_once = True
            exchange.reject_post_only_side = "sell"
            self.assertFalse(restarted.reset_grid())
            self.assertIsNone(restarted.database.get_state(grid_main.BREAKOUT_NOTICE_KEY))
            self.assertTrue(restarted.reset_grid())
            self.assertEqual(restarted.anchor, Decimal("145"))
            self.assertEqual(restarted.config.lower_price, Decimal("123.25"))
            self.assertEqual(restarted.config.upper_price, Decimal("166.75"))
            self.assertEqual(restarted.config.spacing_percent, Decimal("10"))
            self.assertFalse(restarted.grid_needs_reset)
            self.assertIsNone(restarted.database.get_state(grid_main.BREAKOUT_TIMER_KEY))
            self.assertEqual(restarted.database.get_state(grid_main.BREAKOUT_NOTICE_KEY), "1")
            self.assertIsNone(restarted.database.get_state("grid_reset_notification_pending"))
            self.assertEqual(sum(order["type"] == "market" and order["side"] == "buy"
                                 for order in exchange.orders.values()), 1)
            self.assertEqual(sum(order["type"] == "market" and order["side"] == "sell"
                                 for order in exchange.orders.values()), 0)
            self.assertEqual(
                exchange.base_free + sum(
                    Decimal(order["amount"])
                    for order in exchange.fetch_open_orders("BTC/USDT")
                    if order["side"] == "sell"
                ), existing_btc,
            )
            self.assertTrue(all(
                order["params"]["timeInForce"] == "PO"
                for order in exchange.fetch_open_orders("BTC/USDT")
            ))
            exchange.price = Decimal("200")
            restarted._request_breakout_reset(exchange.price)
            self.assertTrue(restarted.reset_grid())
            self.assertEqual(restarted.config.lower_price, Decimal("170.00"))
            self.assertEqual(restarted.config.upper_price, Decimal("230.00"))
            self.assertEqual(restarted.breakout_width_percent, Decimal("15"))

    def test_breakout_fade_before_cancellation_keeps_old_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            old_ids = {order["id"] for order in exchange.fetch_open_orders("BTC/USDT")}
            exchange.price = Decimal("121")
            bot._request_breakout_reset(exchange.price)
            exchange.price = Decimal("120")
            self.assertFalse(bot.reset_grid())
            self.assertFalse(bot.grid_needs_reset)
            self.assertIsNone(bot.database.get_state("grid_reset"))
            self.assertEqual(
                {order["id"] for order in exchange.fetch_open_orders("BTC/USDT")},
                old_ids,
            )
            self.assertIsNone(bot.database.get_state(grid_main.BREAKOUT_NOTICE_KEY))

    def test_manual_recenter_uses_saved_reset_and_carries_btc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.sqlite3"
            bot, exchange = self.make_bot(path)
            bot.run_cycle()
            bot.run_cycle()
            held_before = exchange.base_free + sum(
                Decimal(row["amount"]) for row in exchange.fetch_open_orders("BTC/USDT")
                if row["side"] == "sell"
            )
            old_ids = {row["id"] for row in exchange.fetch_open_orders("BTC/USDT")}
            self.assertEqual(bot.request_manual_recenter("102", "25"),
                             (Decimal("76.50"), Decimal("127.50")))
            self.assertEqual({row["id"] for row in
                              exchange.fetch_open_orders("BTC/USDT")}, old_ids)
            request = json.loads(bot.database.get_state("grid_reset"))
            self.assertEqual(request["source"], "manual_recenter")

            restarted = GridBot(bot.config, exchange, GridDatabase(path))
            restarted.prepare(persist=False)
            self.assertTrue(restarted.reset_grid())
            self.assertEqual(restarted.anchor, Decimal("102"))
            self.assertEqual(restarted.config.lower_price, Decimal("76.50"))
            self.assertEqual(restarted.config.upper_price, Decimal("127.50"))
            self.assertEqual(restarted.breakout_width_percent, Decimal("25"))
            self.assertEqual(restarted.database.get_state(grid_main.BREAKOUT_WIDTH_KEY),
                             "25")
            self.assertEqual(restarted.database.get_state("grid_reset"), None)
            self.assertEqual(restarted.database.get_state(
                "grid_reset_notification_pending"), "1")
            self.assertEqual(
                exchange.base_free + sum(
                    Decimal(row["amount"]) for row in
                    exchange.fetch_open_orders("BTC/USDT") if row["side"] == "sell"
                ), held_before,
            )
            self.assertTrue(all(row["params"]["timeInForce"] == "PO"
                                for row in exchange.fetch_open_orders("BTC/USDT")))
            self.assertEqual(sum(row["type"] == "market" and row["side"] == "sell"
                                 for row in exchange.orders.values()), 0)

    def test_manual_recenter_updates_pause_trigger_after_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.sqlite3"
            bot, exchange = self.make_bot(path)
            bot.run_cycle()
            bot.run_cycle()
            old_ids = {row["id"] for row in exchange.fetch_open_orders("BTC/USDT")}

            self.assertEqual(bot.request_manual_recenter("102", "20", "30"),
                             (Decimal("81.6"), Decimal("122.4")))
            self.assertEqual(bot.config.stop_loss_price, Decimal("70"))
            self.assertEqual({row["id"] for row in exchange.fetch_open_orders("BTC/USDT")},
                             old_ids)
            with self.assertRaisesRegex(ValueError, "below the projected lower bound"):
                bot.request_manual_recenter("102", "20", "10")

            restarted = GridBot(bot.config, exchange, GridDatabase(path))
            restarted.prepare(persist=False)
            self.assertTrue(restarted.reset_grid())
            self.assertEqual(restarted.config.stop_loss_price, Decimal("71.4"))
            saved_config = _apply_active_grid_config(bot.config, GridDatabase(path))
            self.assertEqual(saved_config.stop_loss_price, Decimal("71.4"))
            self.assertEqual(saved_config.lower_price, Decimal("81.6"))
            GridBot(saved_config, exchange, GridDatabase(path)).prepare(persist=False)

    def test_stale_manual_recenter_keeps_original_pause_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            bot.request_manual_recenter("102", "20", "30")
            original_cancel = exchange.cancel_order

            def cancel_and_drift(order_id, symbol, params=None):
                response = original_cancel(order_id, symbol, params)
                exchange.price = Decimal("90")
                return response

            exchange.cancel_order = cancel_and_drift
            self.assertTrue(bot.reset_grid())
            self.assertEqual(bot.config.lower_price, Decimal("80"))
            self.assertEqual(bot.config.stop_loss_price, Decimal("70"))

    def test_manual_recenter_rejects_invalid_or_stale_request_without_canceling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            old_ids = {row["id"] for row in exchange.fetch_open_orders("BTC/USDT")}
            for center, width in (("nan", "25"), ("100", "35"),
                                  ("130", "25"), ("100", "100")):
                with self.assertRaises(ValueError):
                    bot.request_manual_recenter(center, width)
            self.assertIsNone(bot.database.get_state("grid_reset"))
            bot.request_manual_recenter("102", "25")
            with self.assertRaises(TradingHalt):
                bot.request_manual_recenter("102", "25")
            exchange.price = Decimal("130")
            self.assertFalse(bot.reset_grid())
            self.assertIsNone(bot.database.get_state("grid_reset"))
            self.assertEqual({row["id"] for row in
                              exchange.fetch_open_orders("BTC/USDT")}, old_ids)

    def test_manual_recenter_market_drift_rebuilds_old_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            bot.request_manual_recenter("102", "25")
            original_cancel = exchange.cancel_order

            def cancel_and_drift(order_id, symbol, params=None):
                response = original_cancel(order_id, symbol, params)
                exchange.price = Decimal("90")
                return response

            exchange.cancel_order = cancel_and_drift
            self.assertTrue(bot.reset_grid())
            self.assertEqual(bot.config.lower_price, Decimal("80"))
            self.assertEqual(bot.config.upper_price, Decimal("120"))
            self.assertEqual(bot.anchor, Decimal("90"))
            self.assertEqual(bot.breakout_width_percent, Decimal("20"))
            self.assertEqual(bot.database.get_state(grid_main.BREAKOUT_WIDTH_KEY),
                             "20")

    def test_manual_recenter_does_not_override_safety_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            bot.set_manual_pause(True)
            with self.assertRaises(TradingHalt):
                bot.request_manual_recenter("102", "25")
            self.assertIsNone(bot.database.get_state("grid_reset"))

    def test_breakout_fade_during_cancellation_rebuilds_old_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            exchange.price = Decimal("121")
            bot._request_breakout_reset(exchange.price)
            original_cancel = exchange.cancel_order

            def cancel_and_fade(order_id, symbol, params=None):
                response = original_cancel(order_id, symbol, params)
                exchange.price = Decimal("100")
                return response

            exchange.cancel_order = cancel_and_fade
            self.assertTrue(bot.reset_grid())
            self.assertEqual(bot.config.lower_price, Decimal("80"))
            self.assertEqual(bot.config.upper_price, Decimal("120"))
            self.assertEqual(bot.anchor, Decimal("100"))
            self.assertIsNone(bot.database.get_state(grid_main.BREAKOUT_NOTICE_KEY))
            self.assertEqual(
                bot.database.get_state("grid_reset_notification_pending"), "1"
            )

    def test_uncertain_submission_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            exchange.fail_create = True
            with self.assertRaises(UncertainOrderError):
                bot.run_cycle()
            rows = bot.database.fetch_active_grids()
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["exchange_order_id"])

    def test_post_only_rejection_skips_one_order_and_retries_next_cycle(self) -> None:
        for side, skipped_level in (("sell", -1), ("buy", 1)):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as directory:
                bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
                bot.run_cycle()  # The seed market buy is deliberately not post-only.
                exchange.reject_post_only_once = True
                exchange.reject_post_only_side = side
                with self.assertLogs(grid_main.LOGGER, level="WARNING") as logs:
                    bot.run_cycle()
                self.assertIn("skipping this order", logs.output[0])
                self.assertTrue(bot._post_only_rejected_in_cycle)
                self.assertNotIn(skipped_level, bot.database.fetch_latest_orders_by_level())
                self.assertEqual(
                    len(bot.database.fetch_active_grids()),
                    len(exchange.fetch_open_orders("BTC/USDT")),
                )

                bot.run_cycle()
                self.assertFalse(bot._post_only_rejected_in_cycle)
                self.assertIn(skipped_level, bot.database.fetch_latest_orders_by_level())
                self.assertEqual(
                    len(bot.database.fetch_active_grids()),
                    len(exchange.fetch_open_orders("BTC/USDT")),
                )

    def test_other_immediate_order_error_remains_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            with patch.object(
                exchange, "create_order",
                side_effect=ccxt.OrderImmediatelyFillable("Order would trigger immediately."),
            ), self.assertRaises(UncertainOrderError):
                bot.run_cycle()
            self.assertEqual(len(bot.database.fetch_active_grids()), 1)

    def test_setgrid_carries_btc_and_rebuilds_without_market_sale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.sqlite3"
            bot, exchange = self.make_bot(path)
            bot.run_cycle()
            bot.run_cycle()
            first_seed = bot.database.fetch_latest_orders_by_level()[0]
            first_seed_amount = Decimal(exchange.orders[first_seed["client_order_id"]]["filled"])
            bot.request_grid_reset("75", "125")
            self.assertTrue(bot.grid_needs_reset)
            self.assertEqual(bot.database.get_state("grid_reset") is not None, True)
            self.assertTrue(bot.reset_grid())
            self.assertFalse(bot.grid_needs_reset)
            self.assertEqual(bot.config.spacing_percent, Decimal("2.5"))
            self.assertEqual(exchange.base_free, Decimal("1") + first_seed_amount -
                             sum(Decimal(order["amount"])
                                 for order in exchange.fetch_open_orders("BTC/USDT")
                                 if order["side"] == "sell"))
            self.assertFalse(any(order["side"] == "sell" and order["type"] == "market"
                                 for order in exchange.orders.values()))
            carry = bot.database.fetch_latest_orders_by_level()[0]
            self.assertEqual(carry["status"], "FILLED")
            self.assertTrue(carry["order_id"].startswith("carry-"))
            self.assertEqual(Decimal(carry["amount"]), first_seed_amount)
            self.assertEqual(len(exchange.fetch_open_orders("BTC/USDT")),
                             len(bot.levels) + len(bot.upper_levels))
            with closing(sqlite3.connect(path)) as connection:
                archived = connection.execute(
                    "SELECT COUNT(*) FROM archived_grid_orders"
                ).fetchone()[0]
            self.assertGreater(archived, 0)
            self.assertEqual(bot.database.get_state("grid_reset"), None)

            reopened = GridBot(bot.config, exchange, GridDatabase(path))
            reopened.prepare(persist=False)
            self.assertEqual(reopened._bot_base_exposure(), first_seed_amount)

            reopened.request_grid_reset("74", "126")
            self.assertTrue(reopened.reset_grid())
            carried_again = reopened.database.fetch_latest_orders_by_level()[0]
            self.assertEqual(Decimal(carried_again["amount"]), first_seed_amount)
            self.assertEqual(Decimal(carried_again["price"]),
                             Decimal(first_seed["price"]))

    def test_grid_reset_waits_after_post_only_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            bot.request_grid_reset("75", "125")
            exchange.reject_post_only_once = True
            with self.assertLogs(grid_main.LOGGER, level="WARNING"):
                self.assertFalse(bot.reset_grid())
            self.assertTrue(bot.grid_needs_reset)
            self.assertIsNotNone(bot.database.get_state("grid_reset"))

            self.assertTrue(bot.reset_grid())
            self.assertFalse(bot.grid_needs_reset)
            self.assertIsNone(bot.database.get_state("grid_reset"))

    def test_setgrid_rejects_bad_bounds_without_canceling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            before = len(exchange.fetch_open_orders("BTC/USDT"))
            for lower, upper in (("nan", "120"), ("120", "80"),
                                 ("90", "95"), ("60", "120")):
                with self.assertRaises(ValueError):
                    bot.request_grid_reset(lower, upper)
            self.assertEqual(len(exchange.fetch_open_orders("BTC/USDT")), before)
            self.assertIsNone(bot.database.get_state("grid_reset"))

    def test_setstop_updates_saved_run_without_touching_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.sqlite3"
            bot, exchange = self.make_bot(path)
            original_config = bot.config
            bot.run_cycle()
            bot.run_cycle()
            original_ids = {row["id"] for row in exchange.fetch_open_orders("BTC/USDT")}

            self.assertEqual(bot.set_stop_loss("74"), Decimal("74"))

            self.assertEqual(bot.config.stop_loss_price, Decimal("74"))
            self.assertFalse(bot.grid_needs_reset)
            self.assertIsNone(bot.database.get_state("grid_reset"))
            self.assertEqual(
                {row["id"] for row in exchange.fetch_open_orders("BTC/USDT")},
                original_ids,
            )
            saved_config = _apply_active_grid_config(original_config, GridDatabase(path))
            self.assertEqual(saved_config.stop_loss_price, Decimal("74"))
            reopened = GridBot(saved_config, exchange, GridDatabase(path))
            reopened.prepare(persist=False)

            reopened.request_grid_reset("75", "125")
            self.assertTrue(reopened.reset_grid())
            after_reset = _apply_active_grid_config(original_config, GridDatabase(path))
            self.assertEqual(after_reset.stop_loss_price, Decimal("74"))
            GridBot(after_reset, exchange, GridDatabase(path)).prepare(persist=False)

    def test_setstop_rejects_invalid_price_without_touching_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            original_ids = {row["id"] for row in exchange.fetch_open_orders("BTC/USDT")}
            for price in ("nan", "inf", "abc", "0", "-1"):
                with self.assertRaisesRegex(ValueError, "valid positive"):
                    bot.set_stop_loss(price)
            for price in ("80", "90"):
                with self.assertRaisesRegex(ValueError, "lower than the current lower bound"):
                    bot.set_stop_loss(price)
            self.assertEqual(bot.config.stop_loss_price, Decimal("70"))
            self.assertEqual(
                {row["id"] for row in exchange.fetch_open_orders("BTC/USDT")},
                original_ids,
            )
            self.assertIsNone(bot.database.get_state("active_grid_config"))

    def test_status_counts_live_limit_orders_and_nearest_prices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            exchange.orders["external-buy"] = {
                "id": "external-buy", "type": "limit", "side": "buy",
                "status": "open", "price": "99",
            }
            exchange.orders["external-market"] = {
                "id": "external-market", "type": "market", "side": "buy",
                "status": "open", "price": "100",
            }

            self.assertEqual(
                bot.open_order_summary(Decimal("100")),
                (3, 1, Decimal("99"), Decimal("110.00")),
            )
            self.assertEqual(
                bot.open_order_summary(None), (3, 1, None, None)
            )

    def test_wallet_balances_reads_spot_free_and_used_without_trading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            with patch.object(exchange, "fetch_balance", return_value={
                "USDT": {"free": "250.00", "used": "150.00"},
                "BTC": {"free": "0.01234567", "used": "0.004"},
            }) as fetch_balance:
                self.assertEqual(bot.wallet_balances(), {
                    "USDT": {"free": Decimal("250.00"), "used": Decimal("150.00")},
                    "BTC": {"free": Decimal("0.01234567"), "used": Decimal("0.004")},
                })
                fetch_balance.assert_called_once_with({"type": "spot"})
            self.assertEqual(exchange.orders, {})

            with patch.object(exchange, "fetch_balance", return_value={
                "free": {"USDT": 250}, "used": {"USDT": 0},
            }):
                self.assertEqual(bot.wallet_balances(), {
                    "USDT": {"free": Decimal("250"), "used": Decimal("0")},
                    "BTC": {"free": None, "used": None},
                })
            with patch.object(exchange, "fetch_balance", return_value={
                "USDT": {"free": "-1", "used": "0"},
            }):
                with self.assertRaisesRegex(ValueError, "invalid balance value"):
                    bot.wallet_balances()

    def test_orders_lists_all_live_orders_sorted_by_price(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            exchange.orders["external-buy"] = {
                "id": "external-buy", "side": "buy", "status": "open",
                "type": "limit", "amount": "0.10", "remaining": "0.03", "price": "95",
            }
            exchange.orders["external-sell"] = {
                "id": "external-sell", "side": "sell", "status": "open",
                "type": "limit", "amount": "0.04", "price": "105",
            }
            exchange.orders["no-price"] = {
                "id": "no-price", "side": "buy", "status": "open",
                "type": "market", "amount": "0.02", "price": None,
            }

            base, quote, buys, sells = bot.list_open_orders()

            self.assertEqual((base, quote), ("BTC", "USDT"))
            self.assertEqual([price for _, price in buys],
                             [Decimal("95"), Decimal("90"), Decimal("81"), None])
            self.assertEqual(buys[0][0], Decimal("0.03"))
            self.assertEqual([price for _, price in sells],
                             [Decimal("105"), Decimal("110")])

    def test_reset_preserves_cost_basis_after_old_sell_fill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            lower_buy = bot.database.fetch_latest_orders_by_level()[1]
            exchange.fill(lower_buy["client_order_id"])
            bot.run_cycle()
            upper_sell = bot.database.fetch_latest_orders_by_level()[-1]
            sold = Decimal(upper_sell["amount"])
            exchange.fill(upper_sell["client_order_id"])
            bot.request_grid_reset("75", "125")

            self.assertTrue(bot.reset_grid())

            carry = bot.database.fetch_latest_orders_by_level()[0]
            bought = Decimal(lower_buy["amount"])
            self.assertEqual(Decimal(carry["amount"]), Decimal("5") + bought - sold)
            expected_cost = Decimal("500") * (Decimal("5") - sold) / 5 + (
                bought * Decimal(lower_buy["price"]))
            self.assertAlmostEqual(Decimal(carry["price"]),
                                   expected_cost / Decimal(carry["amount"]), places=8)
            self.assertEqual(len(bot.database.fetch_trade_history()), 1)
            self.assertFalse(any(order["type"] == "market" and order["side"] == "sell"
                                 for order in exchange.orders.values()))

    def test_reset_halts_if_carried_btc_cannot_fund_upper_sells(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(Path(directory) / "grid.sqlite3")
            bot.run_cycle()
            bot.run_cycle()
            old_upper = bot.database.fetch_latest_orders_by_level()[-1]
            exchange.fill(old_upper["client_order_id"])
            bot.request_grid_reset("75", "125")

            with self.assertRaisesRegex(ValueError, "market minimum"):
                bot.reset_grid()

            self.assertTrue(bot.grid_needs_reset)
            self.assertEqual(bot.database.get_state("grid_reset") is not None, True)
            self.assertFalse(exchange.fetch_open_orders("BTC/USDT"))
            self.assertFalse(any(order["type"] == "market" and order["side"] == "sell"
                                 for order in exchange.orders.values()))


if __name__ == "__main__":
    unittest.main()
