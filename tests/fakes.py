"""Deterministic Spot exchange and engine fixtures; no network calls."""

from decimal import Decimal, ROUND_DOWN
import time
import json
from dataclasses import replace

import ccxt

from database import GridDatabase
from engine import GridBot
from models import GridConfig


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
        self.cancel_calls = []
        self.cancel_timeout = None
        self.cancel_not_found_once = False
        self.market_sell_calls = 0
        self.market_sell_timeout = None
        self.market_sell_network_errors = 0
        self.market_sell_exchange_errors = 0
        self.market_sell_rate_limits = 0
        self.market_sell_partial_once = False
        self.market_sell_no_id_after_accept = False
        self.market_sell_insufficient = False
        self.market_sell_crash_after_accept = False
        self.limit_sell_calls = 0

    def load_markets(self) -> None:
        pass

    def market(self, _symbol: str) -> dict:
        return {
            "spot": True, "active": True, "base": "BTC", "quote": "USDT",
            "limits": {"amount": {"min": 0.001}, "cost": {"min": 10}},
        }

    def fetch_ticker(self, _symbol: str) -> dict:
        return {"last": str(self.price)}

    def fetch_ohlcv(self, _symbol, timeframe='1d', since=None, limit=201):
        if timeframe != '1d':
            raise ValueError('Fake only supplies daily macro history.')
        day = int(time.time() * 1000) // 86_400_000 * 86_400_000
        return [[day - i * 86_400_000, 50, 51, 49, 50, 1000]
                for i in range(limit - 1, -1, -1)]

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
            "symbol": _symbol,
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
        self.cancel_calls.append(order["clientOrderId"])
        if self.cancel_not_found_once and order["side"] == "buy":
            self.cancel_not_found_once = False
            self.fill(order["clientOrderId"])
            raise ccxt.OrderNotFound("Order filled before cancel")
        if self.cancel_timeout == "before":
            self.cancel_timeout = None
            raise ccxt.RequestTimeout("Simulated cancel request timeout")
        if order["status"] == "open":
            self.orders[order["clientOrderId"]]["status"] = "canceled"
            if order["side"] == "buy":
                self.quote_free += Decimal(order["cost"])
            else:
                self.base_free += Decimal(order["amount"]) - Decimal(order["filled"])
        result = dict(self.orders[order["clientOrderId"]])
        if self.cancel_timeout == "after":
            self.cancel_timeout = None
            raise ccxt.RequestTimeout("Simulated cancel response timeout")
        return result

    def fetch_open_orders(self, _symbol: str = None) -> list:
        return [dict(order) for order in self.orders.values()
                if order["status"] == "open" and
                (_symbol is None or order.get("symbol") in (None, _symbol))]

    def create_market_sell_order(self, symbol: str, amount: float,
                                 params: dict = None) -> dict:
        self.market_sell_calls += 1
        if self.market_sell_network_errors:
            self.market_sell_network_errors -= 1
            raise ccxt.NetworkError("Simulated temporary network failure")
        if self.market_sell_exchange_errors:
            self.market_sell_exchange_errors -= 1
            raise ccxt.ExchangeError("Simulated temporary exchange failure")
        if self.market_sell_rate_limits:
            self.market_sell_rate_limits -= 1
            raise ccxt.RateLimitExceeded("Simulated rate limit")
        if self.market_sell_insufficient:
            raise ccxt.InsufficientFunds("Simulated insufficient BTC")
        if self.market_sell_timeout == "before":
            self.market_sell_timeout = None
            raise ccxt.RequestTimeout("Simulated request timeout")
        response = self.create_order(symbol, "market", "sell", amount, None, params or {})
        if self.market_sell_partial_once:
            self.market_sell_partial_once = False
            quantity = Decimal(str(amount))
            sold = (quantity / 2).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
            unsold = quantity - sold
            self.base_free += unsold
            self.quote_free -= unsold * self.price
            self.orders[response["clientOrderId"]].update(
                status="canceled", filled=str(sold), cost=str(sold * self.price)
            )
            response = dict(self.orders[response["clientOrderId"]])
        if self.market_sell_crash_after_accept:
            self.market_sell_crash_after_accept = False
            raise KeyboardInterrupt("Simulated process interruption after acceptance")
        if self.market_sell_timeout == "after":
            self.market_sell_timeout = None
            raise ccxt.RequestTimeout("Simulated response timeout")
        if self.market_sell_no_id_after_accept:
            self.market_sell_no_id_after_accept = False
            response.pop("id")
        return response

    def create_limit_sell_order(self, symbol: str, amount: float, price: float,
                                params: dict = None) -> dict:
        self.limit_sell_calls += 1
        if self.market_sell_network_errors:
            self.market_sell_network_errors -= 1
            raise ccxt.NetworkError("Simulated temporary network failure")
        if self.market_sell_exchange_errors:
            self.market_sell_exchange_errors -= 1
            raise ccxt.ExchangeError("Simulated temporary exchange failure")
        if self.market_sell_rate_limits:
            self.market_sell_rate_limits -= 1
            raise ccxt.RateLimitExceeded("Simulated rate limit")
        if self.market_sell_insufficient:
            raise ccxt.InsufficientFunds("Simulated insufficient BTC")
        if self.market_sell_timeout == "before":
            self.market_sell_timeout = None
            raise ccxt.RequestTimeout("Simulated request timeout")
        response = self.create_order(symbol, "limit", "sell", amount, price, params or {})
        if self.market_sell_partial_once:
            self.market_sell_partial_once = False
            self.fill(response["clientOrderId"])
            quantity = Decimal(str(amount))
            sold = (quantity / 2).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
            unsold = quantity - sold
            self.base_free += unsold
            self.quote_free -= unsold * Decimal(str(price))
            self.orders[response["clientOrderId"]].update(
                status="canceled", filled=str(sold), cost=str(sold * Decimal(str(price)))
            )
            response = dict(self.orders[response["clientOrderId"]])
        if self.market_sell_crash_after_accept:
            self.market_sell_crash_after_accept = False
            raise KeyboardInterrupt("Simulated process interruption after acceptance")
        if self.market_sell_timeout == "after":
            self.market_sell_timeout = None
            raise ccxt.RequestTimeout("Simulated response timeout")
        if self.market_sell_no_id_after_accept:
            self.market_sell_no_id_after_accept = False
            response.pop("id")
        return response

    def fill(self, client_id: str) -> None:
        order = self.orders[client_id]
        order["status"] = "closed"
        order["filled"] = order["amount"]
        if order["side"] == "buy":
            self.base_free += Decimal(order["filled"])
        else:
            self.quote_free += Decimal(order["cost"])


def make_bot(path, *, width=None, initial_base=Decimal("1"), exchange=None):
    config = GridConfig(
        "BTC/USDT", Decimal("1000"), Decimal("80"), Decimal("120"),
        Decimal("10"), Decimal("50"), Decimal("70"), 2, width,
    )
    exchange = exchange if exchange is not None else FakeSpotExchange()
    exchange.base_free = initial_base
    bot = GridBot(config, exchange, GridDatabase(path))
    bot.prepare(persist=True)
    return bot, exchange


def restore_retained_floor(bot, floor, high):
    """Model a durable pre-Iteration-2 floor; price changes cannot create one now."""
    floor, high = Decimal(str(floor)), Decimal(str(high))
    config = replace(bot.config, stop_loss_price=floor)
    run = json.loads(bot.database.get_state('grid_run'))
    run['fingerprint'] = config.fingerprint()
    active = dict(lower=str(config.lower_price), upper=str(config.upper_price),
                  spacing=str(config.spacing_percent), investment_quote=str(config.investment_quote),
                  stop_loss_price=str(floor))
    if config.buy_grid_levels is not None:
        active.update(buy_levels=config.buy_grid_levels, sell_levels=config.sell_grid_levels)
    risk = dict(high_water_mark=str(high), stop_loss_distance=str(high - floor))
    bot.database.update_runtime_grid_settings(json.dumps(run), json.dumps(active),
                                              trailing_stop=json.dumps(risk), allow_pending_reset=True)
    bot.config, bot.high_water_mark, bot.stop_loss_distance = config, high, high - floor
