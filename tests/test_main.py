import asyncio
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
            return dict(self.orders[params["origClientOrderId"]])
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

    def fill(self, client_id: str) -> None:
        order = self.orders[client_id]
        order["status"] = "closed"
        order["filled"] = order["amount"]
        if order["side"] == "buy":
            self.base_free += Decimal(order["filled"])
        else:
            self.quote_free += Decimal(order["cost"])


class GridBotTests(unittest.TestCase):
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

    def make_bot(self, path: Path):
        config = GridConfig(
            "BTC/USDT", Decimal("1000"), Decimal("80"), Decimal("120"),
            Decimal("10"), Decimal("50"), Decimal("70"), 2,
        )
        exchange = FakeSpotExchange()
        bot = GridBot(config, exchange, GridDatabase(path))
        bot.prepare(persist=True)
        return bot, exchange

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

    def test_safety_pause_cancels_only_buy_limits_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.sqlite3"
            bot, exchange = self.make_bot(path)
            bot.run_cycle()
            bot.run_cycle()
            held_btc = exchange.base_free + sum(
                Decimal(order["amount"]) for order in exchange.fetch_open_orders("BTC/USDT")
                if order["side"] == "sell"
            )
            sell_ids = {
                order["id"] for order in exchange.fetch_open_orders("BTC/USDT")
                if order["side"] == "sell"
            }
            exchange.price = Decimal("69")

            bot.run_cycle()
            self.assertTrue(bot.is_paused)
            self.assertEqual(bot.database.get_state("safety_mode"), "PAUSED_DOWNSIDE")
            self.assertFalse(bot.stop_controller.stop_requested.is_set())
            self.assertFalse(any(order["side"] == "buy" for order in
                                 exchange.fetch_open_orders("BTC/USDT")))
            self.assertEqual(
                {order["id"] for order in exchange.fetch_open_orders("BTC/USDT")
                 if order["side"] == "sell"}, sell_ids,
            )
            self.assertEqual(
                exchange.base_free + sum(
                    Decimal(order["amount"])
                    for order in exchange.fetch_open_orders("BTC/USDT")
                    if order["side"] == "sell"
                ), held_btc,
            )
            self.assertEqual(sum(order["type"] == "market" and order["side"] == "sell"
                                 for order in exchange.orders.values()), 0)
            notifier = SimpleNamespace(
                notify_safety_pause=AsyncMock(),
                notify_safety_recovery=AsyncMock(),
                notify_safety_resume=AsyncMock(),
            )
            asyncio.run(bot._notify_safety_state(notifier))
            notifier.notify_safety_pause.assert_awaited_once()
            before = len(exchange.orders)
            bot.run_cycle()
            self.assertEqual(len(exchange.orders), before)

            reopened = GridBot(bot.config, exchange, GridDatabase(path))
            reopened.prepare(persist=False)
            self.assertTrue(reopened.is_paused)
            exchange.price = Decimal("80")
            reopened.run_cycle()
            self.assertTrue(reopened.is_paused)
            exchange.price = Decimal("80.01")
            reopened.run_cycle()
            self.assertFalse(reopened.is_paused)
            self.assertIsNone(reopened.database.get_state("safety_mode"))
            self.assertEqual(reopened.database.get_state("safety_resume_notice_pending"), "1")
            self.assertFalse(any(order["side"] == "buy" for order in
                                 exchange.fetch_open_orders("BTC/USDT")))
            asyncio.run(reopened._notify_safety_state(notifier))
            notifier.notify_safety_recovery.assert_awaited_once()
            notifier.notify_safety_resume.assert_not_awaited()
            exchange.price = Decimal("82")
            reopened.run_cycle()
            self.assertEqual(
                len([order for order in exchange.fetch_open_orders("BTC/USDT")
                     if order["side"] == "buy"]), 1,
            )
            self.assertTrue(reopened._missing_paused_buys())
            exchange.price = Decimal("91")
            reopened.run_cycle()
            self.assertFalse(reopened._missing_paused_buys())
            self.assertEqual(
                len([order for order in exchange.fetch_open_orders("BTC/USDT")
                     if order["side"] == "buy"]), len(reopened.levels),
            )
            asyncio.run(reopened._notify_safety_state(notifier))
            notifier.notify_safety_resume.assert_awaited_once()

    def test_safety_pause_keeps_partially_filled_buy_inventory(self) -> None:
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

            latest = bot.database.fetch_latest_orders_by_level()[1]
            self.assertEqual(latest["side"], "SELL")
            self.assertEqual(latest["status"], "OPEN")
            self.assertEqual(latest["parent_order_id"], buy["order_id"])
            self.assertLessEqual(Decimal(latest["amount"]), partial)
            self.assertTrue(bot.is_paused)
            self.assertFalse(any(order["side"] == "buy" for order in
                                 exchange.fetch_open_orders("BTC/USDT")))

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
