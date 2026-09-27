"""Phase 4: Binance Spot Testnet geometric grid bot."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple

import ccxt
from dotenv import load_dotenv

from database import GridDatabase
from exchange_handler import config_path, create_exchange, load_config
from telegram_bot import StopController, TelegramBot, load_telegram_credentials


BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)
MAX_LEVELS = 50
SELL_AMOUNT_BUFFER = Decimal("0.002")
RESET_SPACING_PERCENT = Decimal("2.5")
OpenOrderEntry = Tuple[Optional[Decimal], Optional[Decimal]]


class TradingHalt(RuntimeError):
    """An unsafe or unresolved state that requires the bot to stop."""


class UncertainOrderError(TradingHalt):
    """The exchange may have accepted an order without returning its ID."""


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Set grid.{name} to a positive number in config.json.")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"grid.{name} must be a valid number.") from error
    if not result.is_finite() or result <= 0:
        raise ValueError(f"grid.{name} must be positive and finite.")
    return result


def _order_decimal(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value if value is not None else default))


@dataclass(frozen=True)
class GridConfig:
    symbol: str
    investment_quote: Decimal
    lower_price: Decimal
    upper_price: Decimal
    spacing_percent: Decimal
    initial_inventory_percent: Decimal
    stop_loss_price: Decimal
    poll_seconds: int

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "GridConfig":
        raw = load_config(path)
        grid = raw["grid"]
        investment = _decimal(grid.get("investment_quote"), "investment_quote")
        lower = _decimal(grid.get("lower_price"), "lower_price")
        upper = _decimal(grid.get("upper_price"), "upper_price")
        spacing = _decimal(grid.get("spacing_percent"), "spacing_percent")
        inventory_percent = _decimal(
            grid.get("initial_inventory_percent", 50), "initial_inventory_percent"
        )
        stop_loss = _decimal(grid.get("stop_loss_price"), "stop_loss_price")
        poll_seconds = grid.get("poll_seconds", 10)
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, int):
            raise ValueError("grid.poll_seconds must be an integer.")
        if not 2 <= poll_seconds <= 3600:
            raise ValueError("grid.poll_seconds must be between 2 and 3600.")
        if not 0 < stop_loss < lower < upper:
            raise ValueError("Require stop_loss_price < lower_price < upper_price.")
        if spacing >= 100:
            raise ValueError("grid.spacing_percent must be below 100.")
        if inventory_percent >= 100:
            raise ValueError("grid.initial_inventory_percent must be below 100.")
        return cls(raw["grid"]["symbol"], investment, lower, upper,
                   spacing, inventory_percent, stop_loss, poll_seconds)

    def fingerprint(self) -> str:
        parameters = {
            "symbol": self.symbol,
            "investment_quote": str(self.investment_quote),
            "lower_price": str(self.lower_price),
            "upper_price": str(self.upper_price),
            "spacing_percent": str(self.spacing_percent),
            "initial_inventory_percent": str(self.initial_inventory_percent),
            "stop_loss_price": str(self.stop_loss_price),
        }
        encoded = json.dumps(parameters, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def geometric_levels(anchor: Decimal, lower: Decimal, spacing_percent: Decimal) -> List[Decimal]:
    """Each next buy price is a percentage below the previous level."""
    ratio = Decimal(1) - spacing_percent / Decimal(100)
    prices: List[Decimal] = []
    price = anchor * ratio
    while price >= lower:
        prices.append(price)
        if len(prices) > MAX_LEVELS:
            raise ValueError(f"Grid exceeds the {MAX_LEVELS}-level safety cap.")
        price *= ratio
    if not prices:
        raise ValueError("Bounds contain no buy level at this spacing.")
    return prices


def geometric_upper_levels(
    anchor: Decimal, upper: Decimal, spacing_percent: Decimal
) -> List[Decimal]:
    """Each next sell price is a percentage above the previous level."""
    ratio = Decimal(1) + spacing_percent / Decimal(100)
    prices: List[Decimal] = []
    price = anchor * ratio
    while price <= upper:
        prices.append(price)
        if len(prices) > MAX_LEVELS:
            raise ValueError(f"Upper grid exceeds the {MAX_LEVELS}-level safety cap.")
        price *= ratio
    if not prices:
        raise ValueError("Bounds contain no upper sell level at this spacing.")
    return prices


def _fees_in_asset(order: Dict[str, Any], asset: str) -> Decimal:
    fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
    return sum(
        (_order_decimal(fee.get("cost")) for fee in fees if fee.get("currency") == asset),
        Decimal(0),
    )


class GridBot:
    def __init__(self, config: GridConfig, exchange: Any, database: GridDatabase) -> None:
        self.config = config
        self.exchange = exchange
        self.database = database
        self.exchange_lock = RLock()
        self.stop_controller = StopController(
            exchange, database, config.symbol, self.exchange_lock
        )
        self.market: Dict[str, Any] = {}
        self.anchor: Optional[Decimal] = None
        self.levels: List[Decimal] = []
        self.upper_levels: List[Decimal] = []
        self.baseline_base: Optional[Decimal] = None
        self.stop_loss_triggered: Optional[Decimal] = None
        self._post_only_rejected_in_cycle = False
        self._grid_lock = RLock()
        pending_text = self.database.get_state("grid_reset")
        pending = json.loads(pending_text) if pending_text else None
        self.pending_grid_bounds: Optional[Tuple[Decimal, Decimal]] = (
            (Decimal(pending["lower"]), Decimal(pending["upper"])) if pending else None
        )
        self.grid_needs_reset = pending is not None

    def grid_status(self) -> Tuple[Decimal, Decimal, Decimal, int, Decimal]:
        price = self._ticker_price()
        lower, upper, levels, stop_loss = self.grid_configuration()
        return price, lower, upper, levels, stop_loss

    def wallet_balances(self) -> Dict[str, Dict[str, Optional[Decimal]]]:
        """Read free and locked Spot balances for the configured pair."""
        with self._grid_lock:
            base, quote = self.config.symbol.split("/")
        response = self._call(self.exchange.fetch_balance, {"type": "spot"})
        if not isinstance(response, dict):
            raise ValueError("Exchange returned an invalid balance response.")

        balances: Dict[str, Dict[str, Optional[Decimal]]] = {}
        for asset in (quote, base):
            asset_data = response.get(asset)
            balances[asset] = {}
            for field in ("free", "used"):
                raw = asset_data.get(field) if isinstance(asset_data, dict) else None
                if raw is None:
                    by_field = response.get(field)
                    raw = by_field.get(asset) if isinstance(by_field, dict) else None
                if raw is None:
                    balances[asset][field] = None
                    continue
                try:
                    value = Decimal(str(raw))
                except InvalidOperation as error:
                    raise ValueError("Exchange returned an invalid balance value.") from error
                if not value.is_finite() or value < 0:
                    raise ValueError("Exchange returned an invalid balance value.")
                balances[asset][field] = value
        return balances

    def grid_configuration(self) -> Tuple[Decimal, Decimal, int, Decimal]:
        """Read the active settings without making an exchange request."""
        with self._grid_lock:
            return (self.config.lower_price, self.config.upper_price,
                    len(self.levels) + len(self.upper_levels),
                    self.config.stop_loss_price)

    def open_order_summary(
        self, current_price: Optional[Decimal]
    ) -> Tuple[int, int, Optional[Decimal], Optional[Decimal]]:
        """Count live Spot limit orders, including orders outside local SQLite tracking."""
        orders = self._live_open_orders()
        buy_count = sell_count = 0
        buy_prices: List[Decimal] = []
        sell_prices: List[Decimal] = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            info = order.get("info")
            raw_type = order.get("type") or (
                info.get("type") if isinstance(info, dict) else None
            )
            order_type = str(raw_type or "").lower()
            if order_type not in ("limit", "limit_maker"):
                continue
            if str(order.get("status") or "open").lower() != "open":
                continue
            side = str(order.get("side") or "").lower()
            if side == "buy":
                buy_count += 1
                prices = buy_prices
            elif side == "sell":
                sell_count += 1
                prices = sell_prices
            else:
                continue
            try:
                price = Decimal(str(order.get("price")))
            except InvalidOperation:
                continue
            if price.is_finite() and price > 0:
                prices.append(price)
        closest_buy = (
            min(buy_prices, key=lambda price: abs(price - current_price))
            if buy_prices and current_price is not None else None
        )
        closest_sell = (
            min(sell_prices, key=lambda price: abs(price - current_price))
            if sell_prices and current_price is not None else None
        )
        return buy_count, sell_count, closest_buy, closest_sell

    def _live_open_orders(self) -> List[Dict[str, Any]]:
        with self._grid_lock:
            symbol = self.config.symbol
        orders = self._call(self.exchange.fetch_open_orders, symbol)
        if not isinstance(orders, list):
            raise TradingHalt("Exchange returned an invalid open-order list.")
        return orders

    def list_open_orders(
        self,
    ) -> Tuple[str, str, List[OpenOrderEntry], List[OpenOrderEntry]]:
        """Return every live order by side, sorted nearest to market by limit price."""
        with self._grid_lock:
            base, quote = self.config.symbol.split("/")
        buys: List[OpenOrderEntry] = []
        sells: List[OpenOrderEntry] = []
        orders = self._live_open_orders()
        for order in orders:
            if not isinstance(order, dict):
                continue
            if str(order.get("status") or "open").lower() != "open":
                continue
            side = str(order.get("side") or "").lower()
            if side not in ("buy", "sell"):
                continue
            raw_amount = order.get("remaining")
            if raw_amount is None:
                raw_amount = order.get("amount")
            try:
                amount = Decimal(str(raw_amount))
                if not amount.is_finite() or amount < 0:
                    amount = None
            except InvalidOperation:
                amount = None
            try:
                price = Decimal(str(order.get("price")))
            except InvalidOperation:
                price = None
            if price is not None and (not price.is_finite() or price <= 0):
                price = None
            (buys if side == "buy" else sells).append((amount, price))
        buys.sort(key=lambda item: (item[1] is None, -(item[1] or Decimal(0))))
        sells.sort(key=lambda item: (item[1] is None, item[1] or Decimal(0)))
        return base, quote, buys, sells

    def set_stop_loss(self, price_text: str) -> Decimal:
        try:
            stop_loss = _decimal(price_text, "stop_loss_price")
        except ValueError as error:
            raise ValueError("Enter a valid positive stop-loss price.") from error
        with self._grid_lock:
            if stop_loss >= self.config.lower_price:
                raise ValueError(
                    "❌ Rejected: Stop-loss must be lower than the current lower bound."
                )
            if self.stop_controller.stop_requested.is_set():
                raise RuntimeError("Bot is stopping; stop-loss was not changed.")
            if self.grid_needs_reset:
                raise RuntimeError("A grid reset is in progress; stop-loss was not changed.")
            saved_text = self.database.get_state("grid_run")
            if saved_text is None:
                raise RuntimeError("No active grid run is available.")
            saved = json.loads(saved_text)
            if saved["fingerprint"] != self.config.fingerprint():
                raise TradingHalt("Saved grid settings do not match the active run.")
            new_config = replace(self.config, stop_loss_price=stop_loss)
            saved["fingerprint"] = new_config.fingerprint()
            active = {
                "lower": str(new_config.lower_price),
                "upper": str(new_config.upper_price),
                "spacing": str(new_config.spacing_percent),
                "stop_loss_price": str(stop_loss),
            }
            self.database.update_runtime_grid_settings(
                json.dumps(saved, sort_keys=True), json.dumps(active, sort_keys=True)
            )
            self.config = new_config
        return stop_loss

    def request_grid_reset(self, lower_text: str, upper_text: str) -> None:
        lower = _decimal(lower_text, "lower_price")
        upper = _decimal(upper_text, "upper_price")
        if not self.config.stop_loss_price < lower < upper:
            raise ValueError("Require stop-loss < lower < upper.")
        price = self._ticker_price()
        if not lower < price < upper:
            raise ValueError("Current price must be inside the new bounds.")
        geometric_levels(price, lower, RESET_SPACING_PERCENT)
        geometric_upper_levels(price, upper, RESET_SPACING_PERCENT)
        with self._grid_lock:
            if self.stop_controller.stop_requested.is_set():
                raise RuntimeError("Bot is stopping; grid was not changed.")
            if self.grid_needs_reset or self.database.get_state("grid_reset"):
                raise RuntimeError("A grid reset is already in progress.")
            request = {"phase": "canceling", "lower": str(lower),
                       "upper": str(upper), "spacing": str(RESET_SPACING_PERCENT)}
            self.database.set_state("grid_reset", json.dumps(request))
            self.pending_grid_bounds = (lower, upper)
            self.grid_needs_reset = True

    def _call(self, method: Any, *args: Any) -> Any:
        with self.exchange_lock:
            return method(*args)

    def _ticker_price(self) -> Decimal:
        ticker = self._call(self.exchange.fetch_ticker, self.config.symbol)
        price = _order_decimal(ticker.get("last"))
        if not price.is_finite() or price <= 0:
            raise TradingHalt("Ticker has no valid last price.")
        return price

    def prepare(
        self, *, persist: bool
    ) -> Tuple[Decimal, List[Tuple[int, Decimal, Decimal]], List[Tuple[int, Decimal, Decimal]]]:
        self._call(self.exchange.load_markets)
        self.market = self.exchange.market(self.config.symbol)
        if not self.market.get("spot") or self.market.get("active") is False:
            raise TradingHalt("Configured symbol is not an active Spot market.")
        current_price = self._ticker_price()

        saved = self.database.get_state("grid_run")
        if saved:
            state = json.loads(saved)
            if state["fingerprint"] != self.config.fingerprint():
                raise TradingHalt("Grid settings changed since the saved run; reconcile orders first.")
            self.anchor = Decimal(state["anchor"])
            if "baseline_base" not in state:
                raise TradingHalt("Saved run lacks its starting base balance; reconcile it first.")
            self.baseline_base = Decimal(state["baseline_base"])
        else:
            if self.database.fetch_all_orders():
                raise TradingHalt("Orders exist without a saved anchor; reconcile them first.")
            if not self.config.lower_price < current_price < self.config.upper_price:
                raise ValueError(
                    f"Current price {current_price} must be inside "
                    f"{self.config.lower_price}–{self.config.upper_price}."
                )
            self.anchor = current_price
            self.baseline_base = self._free_balance(self.market["base"])

        if not self.config.lower_price < self.anchor < self.config.upper_price:
            raise TradingHalt("Saved anchor lies outside the configured grid bounds.")
        self.levels = geometric_levels(
            self.anchor, self.config.lower_price, self.config.spacing_percent
        )
        self.upper_levels = geometric_upper_levels(
            self.anchor, self.config.upper_price, self.config.spacing_percent
        )
        seed_quote = self._seed_quote()
        quote_per_level = self._lower_quote_per_level()
        seed_amount = self._amount(seed_quote / current_price)
        self._check_order_size(current_price, seed_amount)
        planned: List[Tuple[int, Decimal, Decimal]] = []
        for level, raw_price in enumerate(self.levels, start=1):
            price = self._price(raw_price)
            amount = self._amount(quote_per_level / price)
            self._check_order_size(price, amount)
            if price * amount > quote_per_level:
                raise TradingHalt("A rounded grid order exceeds its quote allocation.")
            planned.append((level, price, amount))

        upper_amount = self._amount(
            seed_amount * (1 - SELL_AMOUNT_BUFFER) / Decimal(len(self.upper_levels))
        )
        upper_planned: List[Tuple[int, Decimal, Decimal]] = []
        for index, raw_price in enumerate(self.upper_levels, start=1):
            price = self._price(raw_price)
            self._check_order_size(price, upper_amount)
            upper_planned.append((-index, price, upper_amount))

        if persist and not saved:
            state = {
                "anchor": str(self.anchor),
                "baseline_base": str(self.baseline_base),
                "fingerprint": self.config.fingerprint(),
            }
            self.database.set_state("grid_run", json.dumps(state, sort_keys=True))
        return current_price, planned, upper_planned

    def _seed_quote(self) -> Decimal:
        return self.config.investment_quote * self.config.initial_inventory_percent / 100

    def _lower_quote_per_level(self) -> Decimal:
        lower_budget = self.config.investment_quote - self._seed_quote()
        return lower_budget / Decimal(len(self.levels))

    def _price(self, value: Decimal) -> Decimal:
        return Decimal(self.exchange.price_to_precision(self.config.symbol, str(value)))

    def _amount(self, value: Decimal) -> Decimal:
        return Decimal(self.exchange.amount_to_precision(self.config.symbol, str(value)))

    def _check_order_size(self, price: Decimal, amount: Decimal) -> None:
        if price <= 0 or amount <= 0:
            raise ValueError("Order price or amount rounded to zero.")
        limits = self.market.get("limits") or {}
        amount_min = (limits.get("amount") or {}).get("min")
        cost_min = (limits.get("cost") or {}).get("min")
        if amount_min is not None and amount < _order_decimal(amount_min):
            raise ValueError("Order amount is below the market minimum.")
        if cost_min is not None and price * amount < _order_decimal(cost_min):
            raise ValueError("Order notional is below the market minimum.")

    def _fetch_order(self, row: Dict[str, Any]) -> Dict[str, Any]:
        carry_text = self.database.get_state("carry_inventory")
        carry = json.loads(carry_text) if carry_text else {}
        if carry.get("order_id") == row["order_id"]:
            return {"id": row["order_id"], "status": "closed",
                    "filled": row["amount"], "amount": row["amount"],
                    "average": row["price"], "price": row["price"],
                    "cost": carry["cost"], "fees": []}
        reference = row.get("exchange_order_id") or row["order_id"]
        if row.get("client_order_id"):
            order = self._call(
                self.exchange.fetch_order, reference, self.config.symbol,
                {"origClientOrderId": row["client_order_id"]},
            )
        else:
            order = self._call(self.exchange.fetch_order, reference, self.config.symbol)
        if not isinstance(order, dict):
            raise TradingHalt("Exchange returned an invalid order response.")
        if order.get("id") and not row.get("exchange_order_id"):
            self.database.set_exchange_order_id(row["order_id"], str(order["id"]))
        return order

    def _reconcile_order(self, row: Dict[str, Any]) -> Optional[Tuple[str, Tuple[str, ...]]]:
        order = self._fetch_order(row)
        status = order.get("status")
        if status == "open":
            if _order_decimal(order.get("filled")) > 0 and row["status"] == "OPEN":
                self.database.update_order_status(row["order_id"], "PARTIALLY_FILLED")
            return None
        if status == "closed":
            changed = self.database.mark_order_filled(row["order_id"])
            if changed:
                amount = str(order.get("filled") or row["amount"])
                price = str(order.get("average") or order.get("price") or row["price"])
                return ("filled", (row["order_id"], row["side"], amount, price))
            return None
        if status in ("canceled", "expired", "rejected"):
            self.database.update_order_status(row["order_id"], status.upper())
            raise TradingHalt(f"Tracked order {row['order_id']} ended as {status}.")
        raise TradingHalt("Exchange returned an unknown order status.")

    def _submit_order(
        self, level: int, side: str, price: Decimal, amount: Decimal,
        *, parent_order_id: Optional[str] = None, order_type: str = "LIMIT",
        quote_cost: Optional[Decimal] = None,
    ) -> Optional[str]:
        client_id = "gb" + uuid.uuid4().hex[:30]
        with self.exchange_lock:
            liquidation = order_type == "MARKET" and side == "SELL" and level == -1
            if self.stop_controller.stop_requested.is_set() and not liquidation:
                raise TradingHalt("Stop was requested before order submission.")
            self.database.insert_order(
                client_id, level, side, price, amount,
                client_order_id=client_id, parent_order_id=parent_order_id,
                order_type=order_type,
            )
            try:
                params = {"newClientOrderId": client_id}
                if order_type == "LIMIT":
                    params["timeInForce"] = "PO"
                if quote_cost is not None:
                    params["quoteOrderQty"] = str(quote_cost)
                response = self.exchange.create_order(
                    self.config.symbol, order_type.lower(), side.lower(), float(amount),
                    float(price) if order_type == "LIMIT" else None,
                    params,
                )
            except ccxt.OrderImmediatelyFillable as error:
                if (order_type != "LIMIT" or
                        "Order would immediately match and take." not in str(error)):
                    raise UncertainOrderError(
                        f"Order {client_id} may have been accepted; inspect it before restarting."
                    ) from error
                self.database.discard_rejected_post_only_order(client_id)
                self._post_only_rejected_in_cycle = True
                LOGGER.warning(
                    "Spot Testnet rejected post-only %s %s at %s (grid level %s); "
                    "skipping this order until the next cycle.",
                    side, amount, price, level,
                )
                return None
            except Exception as error:
                raise UncertainOrderError(
                    f"Order {client_id} may have been accepted; inspect it before restarting."
                ) from error
        if not isinstance(response, dict) or not response.get("id"):
            raise UncertainOrderError(
                f"Order {client_id} returned no exchange ID; inspect it before restarting."
            )
        self.database.set_exchange_order_id(client_id, str(response["id"]))
        LOGGER.info(
            "Spot Testnet submitted %s %s %s %s at %s (grid level %s).",
            order_type, side, amount, self.config.symbol, price, level,
        )
        return client_id

    def _free_balance(self, asset: str) -> Decimal:
        balances = self._call(self.exchange.fetch_balance, {"type": "spot"})
        return _order_decimal((balances.get(asset) or {}).get("free"))

    def _free_bot_base(self) -> Decimal:
        if self.baseline_base is None:
            raise TradingHalt("Starting base balance has not been recorded.")
        return max(
            Decimal(0), self._free_balance(self.market["base"]) - self.baseline_base
        )

    def _place_seed_buy(self, current_price: Decimal) -> None:
        quote_cost = self._seed_quote()
        amount = self._amount(quote_cost / current_price)
        self._check_order_size(current_price, amount)
        if self._free_balance(self.market["quote"]) < quote_cost:
            raise TradingHalt("Insufficient free quote balance for the seed buy.")
        if not self.stop_controller.stop_requested.is_set():
            self._submit_order(
                0, "BUY", current_price, amount,
                order_type="MARKET", quote_cost=quote_cost,
            )

    def _place_seed_upper_sell(self, level: int, seed_row: Dict[str, Any]) -> None:
        seed = self._fetch_order(seed_row)
        filled = _order_decimal(seed.get("filled"))
        if filled <= 0:
            raise TradingHalt("Seed buy has no executed amount.")
        base_fee = _fees_in_asset(seed, self.market["base"])
        allocated_base = min(
            filled - base_fee, filled * (1 - SELL_AMOUNT_BUFFER)
        ) / Decimal(len(self.upper_levels))
        amount = self._amount(allocated_base)
        price = self._price(self.upper_levels[-level - 1])
        self._check_order_size(price, amount)
        if self._free_bot_base() < amount:
            return  # Account balance can lag a newly filled market buy.
        if not self.stop_controller.stop_requested.is_set():
            self._submit_order(
                level, "SELL", price, amount,
                parent_order_id=seed_row["order_id"],
            )

    def _place_buy(self, level: int, parent_order_id: Optional[str]) -> None:
        if level > 0:
            price = self._price(self.levels[level - 1])
            amount = self._amount(self._lower_quote_per_level() / price)
        else:
            sell_row = self.database.get_order(parent_order_id) if parent_order_id else None
            if sell_row is None or sell_row["side"] != "SELL":
                raise TradingHalt("Upper grid buy has no parent sell.")
            sell = self._fetch_order(sell_row)
            amount = self._amount(_order_decimal(sell.get("filled")))
            price = self._price(
                self.anchor if level == -1 else self.upper_levels[-level - 2]
            )
        self._check_order_size(price, amount)
        if self._free_balance(self.market["quote"]) < price * amount:
            raise TradingHalt("Insufficient free quote balance for the next grid buy.")
        if not self.stop_controller.stop_requested.is_set():
            self._submit_order(level, "BUY", price, amount, parent_order_id=parent_order_id)

    def _place_sell(self, row: Dict[str, Any]) -> None:
        buy = self._fetch_order(row)
        filled = _order_decimal(buy.get("filled"))
        if filled <= 0:
            raise TradingHalt("Filled buy has no executed amount.")
        base_fee = _fees_in_asset(buy, self.market["base"])
        conservative_base = min(filled - base_fee, filled * (1 - SELL_AMOUNT_BUFFER))
        amount = self._amount(conservative_base)
        if row["level"] > 0:
            ratio = Decimal(1) - self.config.spacing_percent / Decimal(100)
            target = self.levels[row["level"] - 1] / ratio
        else:
            target = self.upper_levels[-row["level"] - 1]
        price = self._price(target)
        if price > self.config.upper_price:
            raise TradingHalt("Planned sell is above the upper grid bound.")
        self._check_order_size(price, amount)
        if self._free_bot_base() < amount:
            return  # Recheck after the account balance catches up with the fill.
        if not self.stop_controller.stop_requested.is_set():
            self._submit_order(
                row["level"], "SELL", price, amount,
                parent_order_id=row["order_id"],
            )

    def _record_filled_sell(self, row: Dict[str, Any]) -> None:
        parent_id = row.get("parent_order_id")
        parent = self.database.get_order(parent_id) if parent_id else None
        if parent is None or parent["side"] != "BUY":
            raise TradingHalt("Filled sell has no tracked parent buy.")
        buy = self._fetch_order(parent)
        sell = self._fetch_order(row)
        buy_filled = _order_decimal(buy.get("filled"))
        sell_filled = _order_decimal(sell.get("filled"))
        if buy_filled <= 0 or sell_filled <= 0:
            raise TradingHalt("Filled trade has no executed amount.")
        buy_price = _order_decimal(buy.get("average") or buy.get("price"))
        sell_price = _order_decimal(sell.get("average") or sell.get("price"))
        net_base = buy_filled - _fees_in_asset(buy, self.market["base"])
        if net_base <= 0 or sell_filled > net_base:
            raise TradingHalt("Sell amount exceeds the tracked buy's net base amount.")
        allocation = sell_filled / net_base
        buy_cost = _order_decimal(buy.get("cost"), str(buy_price * buy_filled))
        sell_cost = _order_decimal(sell.get("cost"), str(sell_price * sell_filled))
        profit = (
            sell_cost - _fees_in_asset(sell, self.market["quote"])
            - allocation * (buy_cost + _fees_in_asset(buy, self.market["quote"]))
        )
        self.database.record_trade(
            buy_price, sell_price, profit, sell_order_id=row["order_id"]
        )

    def _bot_base_exposure(self) -> Decimal:
        """Estimate only this bot's acquired base, including partial fills."""
        net_base = Decimal(0)
        carry_text = self.database.get_state("carry_inventory")
        carry = json.loads(carry_text) if carry_text else {}
        if carry.get("order_id"):
            row = self.database.get_order(carry["order_id"])
            if row is not None:
                net_base += _order_decimal(row["amount"])
        for row in self.database.fetch_all_orders():
            if not row.get("client_order_id"):
                continue
            order = self._fetch_order(row)
            filled = _order_decimal(order.get("filled"))
            if row["side"] == "BUY":
                net_base += min(
                    filled - _fees_in_asset(order, self.market["base"]),
                    filled * (1 - SELL_AMOUNT_BUFFER),
                )
            else:
                net_base -= filled
        return max(Decimal(0), net_base)

    def _carry_inventory(self) -> Tuple[Decimal, Decimal]:
        """Value remaining BTC by its tracked buy lots after all cancels settle."""
        rows = self.database.fetch_all_orders()
        sold_by_buy: Dict[str, Decimal] = {}
        for row in rows:
            if row["side"] != "SELL" or row["order_type"] != "LIMIT":
                continue
            if not row.get("parent_order_id"):
                raise TradingHalt("An old sell has no cost-basis parent.")
            sell = self._fetch_order(row)
            parent = row["parent_order_id"]
            sold = _order_decimal(sell.get("filled"))
            sold_by_buy[parent] = sold_by_buy.get(parent, Decimal(0)) + sold
            if sold > 0:
                self._record_filled_sell(row)
        amount = cost = Decimal(0)
        prior_carry_text = self.database.get_state("carry_inventory")
        prior_carry = json.loads(prior_carry_text) if prior_carry_text else {}
        for row in rows:
            if row["side"] != "BUY" or not (
                row.get("client_order_id") or row["order_id"] == prior_carry.get("order_id")
            ):
                continue
            buy = self._fetch_order(row)
            filled = _order_decimal(buy.get("filled"))
            if filled <= 0:
                continue
            net = filled - _fees_in_asset(buy, self.market["base"])
            remaining = net - sold_by_buy.get(row["order_id"], Decimal(0))
            if remaining < 0:
                raise TradingHalt("Sold BTC exceeds a tracked buy lot.")
            if net <= 0 or remaining == 0:
                continue
            spent = _order_decimal(buy.get("cost"), str(filled *
                                   _order_decimal(buy.get("average") or buy.get("price"))))
            spent += _fees_in_asset(buy, self.market["quote"])
            amount += remaining
            cost += spent * remaining / net
        if amount > 0 and cost <= 0:
            raise TradingHalt("Carried BTC has no verifiable cost basis.")
        if amount > self._free_bot_base():
            raise TradingHalt("Tracked BTC is not fully available after cancellation.")
        return amount, cost

    def _validate_reset_grid(self, new_config: GridConfig, price: Decimal,
                             carry_amount: Decimal) -> Tuple[List[Decimal], List[Decimal]]:
        lowers = geometric_levels(price, new_config.lower_price,
                                  new_config.spacing_percent)
        uppers = geometric_upper_levels(price, new_config.upper_price,
                                        new_config.spacing_percent)
        seed_quote = new_config.investment_quote * new_config.initial_inventory_percent / 100
        lower_quote = (new_config.investment_quote - seed_quote) / len(lowers)
        for raw in lowers:
            level_price = self._price(raw)
            self._check_order_size(level_price, self._amount(lower_quote / level_price))
        upper_total = carry_amount if carry_amount > 0 else self._amount(seed_quote / price)
        upper_amount = self._amount(upper_total * (1 - SELL_AMOUNT_BUFFER) / len(uppers))
        for raw in uppers:
            self._check_order_size(self._price(raw), upper_amount)
        needed_quote = new_config.investment_quote if carry_amount == 0 else (
            new_config.investment_quote - seed_quote)
        if self._free_balance(self.market["quote"]) < needed_quote:
            raise TradingHalt("Insufficient free quote for the replacement grid.")
        return lowers, uppers

    def reset_grid(self) -> bool:
        """Process the persisted reset before an ordinary trading cycle."""
        request_text = self.database.get_state("grid_reset")
        if request_text is None:
            self.pending_grid_bounds = None
            self.grid_needs_reset = False
            return False
        request = json.loads(request_text)
        if self.stop_controller.stop_requested.is_set():
            raise TradingHalt("Stop requested during grid reset.")
        if request["phase"] == "canceling":
            canceled = self.stop_controller.cancel_tracked_orders()
            if canceled.unresolved or self.database.fetch_active_grids():
                raise TradingHalt("Old grid cancellation could not be verified.")
            open_orders = self._call(self.exchange.fetch_open_orders, self.config.symbol)
            if open_orders:
                raise TradingHalt("Exchange still has open orders; reset stopped.")
            carry_amount, carry_cost = self._carry_inventory()
            price = self._ticker_price()
            new_config = replace(self.config, lower_price=Decimal(request["lower"]),
                                 upper_price=Decimal(request["upper"]),
                                 spacing_percent=Decimal(request["spacing"]))
            if not new_config.lower_price < price < new_config.upper_price:
                raise TradingHalt("Price left the requested bounds during reset.")
            lowers, uppers = self._validate_reset_grid(new_config, price, carry_amount)
            baseline = self._free_balance(self.market["base"]) - carry_amount
            if baseline < 0:
                raise TradingHalt("Carried BTC exceeds the account balance.")
            state = {"anchor": str(price), "baseline_base": str(baseline),
                     "fingerprint": new_config.fingerprint()}
            active = {"lower": request["lower"], "upper": request["upper"],
                      "spacing": request["spacing"],
                      "stop_loss_price": str(new_config.stop_loss_price)}
            request["phase"] = "placing"
            carry_id = "carry-" + uuid.uuid4().hex if carry_amount > 0 else None
            self.database.complete_grid_reset(
                json.dumps(state, sort_keys=True), json.dumps(active),
                json.dumps(request), carry_order_id=carry_id,
                carry_price=str(carry_cost / carry_amount) if carry_id else None,
                carry_amount=str(carry_amount) if carry_id else None,
                carry_cost=str(carry_cost) if carry_id else None,
            )
            with self._grid_lock:
                self.config = new_config
                self.anchor = price
                self.baseline_base = baseline
                self.levels, self.upper_levels = lowers, uppers
        elif request["phase"] != "placing":
            raise TradingHalt("Unknown grid reset phase.")
        for _ in range(3):
            self.run_cycle()
            if self._post_only_rejected_in_cycle:
                return False
            latest = self.database.fetch_latest_orders_by_level()
            expected = set(range(1, len(self.levels) + 1)) | set(
                range(-1, -len(self.upper_levels) - 1, -1)) | {0}
            if expected <= latest.keys():
                self.database.finish_grid_reset()
                self.pending_grid_bounds = None
                self.grid_needs_reset = False
                return True
        raise TradingHalt("Replacement grid did not finish placing orders.")

    def _handle_stop_loss(self, price: Decimal) -> None:
        self.stop_loss_triggered = price
        self.database.set_state("halt_reason", "stop_loss")
        result = self.stop_controller.request_stop()
        if result.unresolved:
            raise TradingHalt("Stop-loss cancellation has unresolved orders.")
        exposure = self._bot_base_exposure()
        if exposure <= 0:
            return
        free_base = self._free_bot_base()
        if free_base < exposure:
            raise TradingHalt("Bot inventory is not fully available for stop-loss sale.")
        amount = self._amount(exposure)
        self._check_order_size(price, amount)
        client_id = self._submit_order(-1, "SELL", price, amount, order_type="MARKET")
        if client_id is None:
            raise TradingHalt("Stop-loss market sale was not submitted.")
        self._reconcile_order(self.database.get_order(client_id))
        if self.database.get_order(client_id)["status"] != "FILLED":
            raise TradingHalt("Stop-loss market sale could not be verified as filled.")

    def run_cycle(self) -> List[Tuple[str, Tuple[str, ...]]]:
        if not self.levels:
            raise RuntimeError("Call prepare() before run_cycle().")
        self._post_only_rejected_in_cycle = False
        current_price = self._ticker_price()
        if current_price <= self.config.stop_loss_price:
            self._handle_stop_loss(current_price)
            return [("stop_loss", (self.config.symbol, str(current_price)))]

        events: List[Tuple[str, Tuple[str, ...]]] = []
        for row in self.database.fetch_active_grids():
            if self.stop_controller.stop_requested.is_set():
                return events
            event = self._reconcile_order(row)
            if event:
                events.append(event)

        latest = self.database.fetch_latest_orders_by_level()
        seed_row = latest.get(0)
        if seed_row is None:
            self._place_seed_buy(current_price)
            return events
        if seed_row["status"] not in ("FILLED",):
            seed_row = self.database.get_order(seed_row["order_id"])
        if seed_row["status"] != "FILLED":
            return events

        latest = self.database.fetch_latest_orders_by_level()
        in_bounds = self.config.lower_price <= current_price <= self.config.upper_price
        lane_levels = (
            list(range(-1, -len(self.upper_levels) - 1, -1))
            + list(range(1, len(self.levels) + 1))
        )
        for level in lane_levels:
            if self.stop_controller.stop_requested.is_set():
                break
            row = latest.get(level)
            if row is None:
                if level < 0:
                    self._place_seed_upper_sell(level, seed_row)
                elif in_bounds:
                    self._place_buy(level, None)
            elif row["status"] in ("OPEN", "PARTIALLY_FILLED"):
                continue
            elif row["status"] == "FILLED" and row["side"] == "BUY":
                self._place_sell(row)
            elif row["status"] == "FILLED" and row["side"] == "SELL":
                self._record_filled_sell(row)
                if in_bounds:
                    self._place_buy(level, row["order_id"])
            else:
                raise TradingHalt("A grid lane ended unexpectedly; inspect local orders.")
        return events

    async def run(self, telegram_bot: TelegramBot) -> None:
        await asyncio.to_thread(self.prepare, persist=True)
        if self.database.get_state("halt_reason"):
            raise TradingHalt("Saved stop state requires manual review before a new run.")
        await telegram_bot.start()
        stop_loss_notified = False
        try:
            await telegram_bot.notify_startup(self.config.symbol)
            backoff = 1
            while True:
                if self.stop_controller.stop_requested.is_set():
                    break
                try:
                    if self.grid_needs_reset:
                        complete = await asyncio.to_thread(self.reset_grid)
                        backoff = 1
                        if not complete:
                            await asyncio.to_thread(
                                self.stop_controller.stop_requested.wait,
                                self.config.poll_seconds,
                            )
                        continue
                    if self.database.get_state("grid_reset_notification_pending"):
                        try:
                            await telegram_bot.notify_grid_reset()
                        except Exception:
                            LOGGER.warning("Grid reset notification could not be delivered.")
                        else:
                            self.database.clear_state("grid_reset_notification_pending")
                    events = await asyncio.to_thread(self.run_cycle)
                    for kind, values in events:
                        if kind == "filled":
                            await telegram_bot.notify_order_filled(*values)
                        elif kind == "stop_loss":
                            await telegram_bot.notify_stop_loss(*values)
                            stop_loss_notified = True
                    backoff = 1
                    delay = self.config.poll_seconds
                except (ccxt.RateLimitExceeded, ccxt.DDoSProtection, ccxt.NetworkError):
                    if self.stop_loss_triggered is not None:
                        await telegram_bot.notify_stop_loss(
                            self.config.symbol, str(self.stop_loss_triggered)
                        )
                        raise TradingHalt("Stop-loss liquidation could not be verified.")
                    delay = min(backoff, 60)
                    backoff = min(backoff * 2, 60)
                except Exception as error:
                    self.stop_controller.stop_requested.set()
                    if self.grid_needs_reset:
                        self.database.set_state("halt_reason", "grid_reset_failed")
                    if self.stop_loss_triggered is not None and not stop_loss_notified:
                        try:
                            await telegram_bot.notify_stop_loss(
                                self.config.symbol, str(self.stop_loss_triggered)
                            )
                        except Exception:
                            pass
                    try:
                        await telegram_bot.notify_critical_error(error)
                    except Exception:
                        pass
                    await asyncio.to_thread(self.stop_controller.request_stop)
                    raise
                await asyncio.to_thread(self.stop_controller.stop_requested.wait, delay)
        finally:
            if not self.stop_controller.stop_requested.is_set():
                await asyncio.to_thread(self.stop_controller.request_stop)
            await telegram_bot.stop()


def _credentials() -> Tuple[str, str]:
    load_dotenv(dotenv_path=BASE_DIR / ".env")
    key = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
    secret = os.getenv("BINANCE_TESTNET_API_SECRET", "").strip()
    if not key or not secret:
        raise ValueError("Set both Binance Spot Testnet keys in .env.")
    return key, secret


def _center_config_at_current_price(exchange: Any, database: GridDatabase) -> None:
    """Center bounds once per new run; preserve them on restart."""
    if database.get_state("grid_run") is not None:
        return
    path = config_path()
    raw = load_config(path)
    percent_value = raw["grid"].get("auto_center_percent")
    if percent_value is None:
        return
    percent = _decimal(percent_value, "auto_center_percent")
    if percent >= 100:
        raise ValueError("grid.auto_center_percent must be below 100.")
    ticker = exchange.fetch_ticker(raw["grid"]["symbol"])
    price = _order_decimal(ticker.get("last"))
    if not price.is_finite() or price <= 0:
        raise TradingHalt("Cannot center bounds without a valid Testnet price.")
    raw["grid"]["lower_price"] = float(price * (1 - percent / 100))
    raw["grid"]["upper_price"] = float(price * (1 + percent / 100))
    temporary_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(raw, file, indent=2)
            file.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    print(
        f"Centered bounds on Spot Testnet price {price}: "
        f"{raw['grid']['lower_price']} to {raw['grid']['upper_price']}"
    )


def _apply_active_grid_config(config: GridConfig, database: GridDatabase) -> GridConfig:
    active_config_text = database.get_state("active_grid_config")
    if not active_config_text:
        return config
    active = json.loads(active_config_text)
    return replace(
        config,
        lower_price=Decimal(active["lower"]),
        upper_price=Decimal(active["upper"]),
        spacing_percent=Decimal(active["spacing"]),
        stop_loss_price=Decimal(
            active.get("stop_loss_price", str(config.stop_loss_price))
        ),
    )


def main() -> int:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    parser = argparse.ArgumentParser(description="Binance Spot Testnet geometric grid bot")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Center and preview without orders")
    mode.add_argument("--execute", action="store_true", help="Trade on Spot Testnet")
    arguments = parser.parse_args()
    try:
        key, secret = _credentials()
        exchange = create_exchange(key, secret)
        database = GridDatabase()
        _center_config_at_current_price(exchange, database)
        config = _apply_active_grid_config(GridConfig.load(), database)
        bot = GridBot(config, exchange, database)
        if arguments.check:
            current, planned, upper_planned = bot.prepare(persist=False)
            print(f"Spot Testnet {config.symbol}: current={current}, anchor={bot.anchor}")
            print(f"Seed market buy: {bot._seed_quote()} {bot.market['quote']}")
            print(f"Geometric lower buy levels: {len(planned)}")
            for level, price, amount in planned:
                print(f"  {level}: buy {amount} at {price}")
            print(f"Geometric upper sell levels: {len(upper_planned)}")
            for level, price, amount in upper_planned:
                print(f"  {level}: sell {amount} at {price}")
            print(
                "Total planned quote: "
                f"{bot._seed_quote() + sum((p * a for _, p, a in planned), Decimal(0))}"
            )
            return 0
        token, owner_chat_id = load_telegram_credentials()
        telegram_bot = TelegramBot(token, owner_chat_id, bot.stop_controller, bot)
        asyncio.run(bot.run(telegram_bot))
        return 0
    except ccxt.NetworkError as error:
        print(f"Bot stopped: {error}", file=sys.stderr)
        return 1
    except (ValueError, TradingHalt, ccxt.BaseError) as error:
        print(f"Bot stopped: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Check open orders before restarting.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
