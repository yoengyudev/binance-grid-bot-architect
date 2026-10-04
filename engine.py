"""Grid execution, risk state machine, and exchange reconciliation."""

import logging
import asyncio
import hashlib
import json
import math
import os
import secrets
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple
import ccxt
from fastapi import status
from database import GridDatabase
from exchange_handler import CircuitOpenError, check_exchange_circuit, config_path, exchange_retry_delay, exchange_write_attempts, load_config
from stop_controller import StopController
from notifier import TelegramNotifier, markdown_text, schedule_telegram_alert
from settings import (
    BREAKOUT_COOLDOWN_SECONDS,
    BREAKOUT_NOTICE_KEY,
    BREAKOUT_TIMER_KEY,
    BREAKOUT_WIDTH_KEY,
    ENGINE_FAULT_KEY,
    ENGINE_HALTED,
    ENGINE_IDLE,
    ENGINE_RUNNING,
    ENGINE_STATUS_KEY,
    ENGINE_STOP_CLEANUP_KEY,
    FILL_SNAPSHOT_PREFIX,
    FULL_BOOK_AUDIT_SECONDS,
    GHOST_ORDER_ALARM_KEY,
    LAST_EXCHANGE_AUDIT_TIME_KEY,
    LAST_MARKET_PRICE_KEY,
    LIQUIDATED,
    LIQUIDATING,
    LIQUIDATION_ALARM_KEY,
    LIQUIDATION_HALTED,
    LIQUIDATION_KEY,
    LIQUIDATION_NOTICE_KEY,
    MAX_LEVELS,
    MIN_GRID_ORDER_NOTIONAL,
    ORDER_CLIENT_PREFIX,
    ORDER_CLIENT_PREFIX_KEY,
    ORDER_CLEANUP_ALARM_KEY,
    PAUSED_DOWNSIDE,
    PAUSED_MANUAL,
    PAUSED_RESET,
    PAUSED_SIZING,
    RECONCILE_COVER_KEY,
    RESET_SPACING_PERCENT,
    RISK_OVERRIDE_DENIED,
    RUNNER_LEASE_TTL_SECONDS,
    SAFETY_MODE_KEY,
    SAFETY_PAUSE_NOTICE_KEY,
    SAFETY_RECOVERY_NOTICE_KEY,
    SAFETY_RESUME_NOTICE_KEY,
    SELL_AMOUNT_BUFFER,
    SIZING_PAUSE_NOTICE_KEY,
    SIZING_PAUSE_REASON_KEY,
    TRADING_SETTINGS,
    TRAILING_STOP_KEY,
)
from models import (
    GridConfig,
    GridSizingError,
    GridStopConflict,
    InsufficientGridCapital,
    LiquidationPending,
    OpenOrderEntry,
    RunnerLeaseLost,
    TradingHalt,
    UncertainOrderError,
    _decimal,
    _order_decimal,
)
from telemetry import (
    _exception_details,
    _journal_fault,
)
from portfolio import (
    save_fill_snapshot,
    _fees_in_asset,
    _pnl_fee_in_quote,
    _portfolio_wallet,
)

LOGGER = logging.getLogger(__name__)


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


def exact_geometric_levels(
    anchor: Decimal, bound: Decimal, count: int,
) -> List[Decimal]:
    """Fit an exact number of geometric levels from center to one bound."""
    if not 1 <= count <= MAX_LEVELS or anchor <= 0 or bound <= 0 or anchor == bound:
        raise ValueError("Exact grid geometry is invalid.")
    ratio = (bound / anchor) ** (Decimal(1) / Decimal(count))
    return [anchor * ratio ** index if index < count else bound
            for index in range(1, count + 1)]


def configured_grid_levels(
    anchor: Decimal, config: GridConfig,
) -> Tuple[List[Decimal], List[Decimal]]:
    if config.buy_grid_levels is None and config.sell_grid_levels is None:
        return (
            geometric_levels(anchor, config.lower_price, config.spacing_percent),
            geometric_upper_levels(anchor, config.upper_price, config.spacing_percent),
        )
    if config.buy_grid_levels is None or config.sell_grid_levels is None:
        raise TradingHalt("Saved exact grid level counts are incomplete.")
    return (
        exact_geometric_levels(anchor, config.lower_price, config.buy_grid_levels),
        exact_geometric_levels(anchor, config.upper_price, config.sell_grid_levels),
    )


class GridBot:
    def __init__(self, config: GridConfig, exchange: Any, database: GridDatabase,
                 *, runner_token: Optional[str] = None) -> None:
        self.config = config
        self.exchange = exchange
        self.database = database
        order_prefix = self.database.get_or_create_state(
            ORDER_CLIENT_PREFIX_KEY, ORDER_CLIENT_PREFIX + uuid.uuid4().hex[:8]
        )
        if (not order_prefix.startswith(ORDER_CLIENT_PREFIX) or
                len(order_prefix) != len(ORDER_CLIENT_PREFIX) + 8 or
                not order_prefix.isalnum()):
            raise TradingHalt("Saved order namespace is invalid; inspect SQLite state.")
        self.order_client_prefix = order_prefix
        self.exchange_lock = RLock()
        self.stop_controller = StopController(
            exchange, database, config.symbol, self.exchange_lock,
            exchange_guard=self.assert_runner_lease,
        )
        self.market: Dict[str, Any] = {}
        self.anchor: Optional[Decimal] = None
        self.levels: List[Decimal] = []
        self.upper_levels: List[Decimal] = []
        self.baseline_base: Optional[Decimal] = None
        self.high_water_mark: Optional[Decimal] = None
        self.stop_loss_distance: Optional[Decimal] = None
        self._post_only_rejected_in_cycle = False
        safety_mode = self.database.get_state(SAFETY_MODE_KEY)
        if safety_mode not in (None, PAUSED_DOWNSIDE, PAUSED_MANUAL, PAUSED_RESET,
                               PAUSED_SIZING,
                               LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED):
            raise TradingHalt("Unknown saved safety mode; inspect local state.")
        self.is_paused = safety_mode is not None
        self._cycle_lock = RLock()
        saved_width = self.database.get_state(BREAKOUT_WIDTH_KEY)
        self.breakout_width_percent = (
            _decimal(saved_width, BREAKOUT_WIDTH_KEY) if saved_width is not None
            else config.auto_center_percent or
            (config.upper_price - config.lower_price) * 100 /
            (config.upper_price + config.lower_price)
        )
        if self.breakout_width_percent >= 100:
            raise TradingHalt("Saved breakout grid width must be below 100 percent.")
        self._grid_lock = RLock()
        pending_text = self.database.get_state("grid_reset")
        pending = json.loads(pending_text) if pending_text else None
        self.pending_grid_bounds: Optional[Tuple[Decimal, Decimal]] = (
            (Decimal(pending["lower"]), Decimal(pending["upper"])) if pending else None
        )
        self.grid_needs_reset = pending is not None
        self.grid_activation_requested = False
        self.ready_mode = False
        self.reconciled_orders: Dict[str, Dict[str, Any]] = {}
        self._runner_token = runner_token or uuid.uuid4().hex
        self._runner_epoch: Optional[int] = None
        self._lease_required = False
        self._runner_lease_lost = False
        self._last_full_book_audit = 0.0
        self.last_exchange_audit_time: Optional[str] = self.database.get_state(
            LAST_EXCHANGE_AUDIT_TIME_KEY
        )

    def acquire_runner_lease(self) -> None:
        epoch = self.database.acquire_runner_lease(
            self._runner_token, ttl_seconds=RUNNER_LEASE_TTL_SECONDS
        )
        if epoch is None:
            raise RunnerLeaseLost("Another process holds the trading runner lease.")
        if self._runner_epoch is not None and epoch != self._runner_epoch:
            self._runner_lease_lost = True
            raise RunnerLeaseLost("The trading runner lease expired before renewal.")
        self._runner_epoch = epoch
        self._lease_required = True

    def renew_runner_lease(self) -> bool:
        if self._runner_epoch is None or self._runner_lease_lost:
            return False
        renewed = self.database.renew_runner_lease(
            self._runner_token, self._runner_epoch,
            ttl_seconds=RUNNER_LEASE_TTL_SECONDS,
        )
        if not renewed:
            self._runner_lease_lost = True
            self.stop_controller.stop_requested.set()
        return renewed

    def assert_runner_lease(self) -> None:
        if not self._lease_required:
            return  # Standalone read-only helpers and existing test harnesses.
        if (self._runner_lease_lost or self._runner_epoch is None or
                not self.database.runner_lease_valid(
                    self._runner_token, self._runner_epoch)):
            self._runner_lease_lost = True
            self.stop_controller.stop_requested.set()
            raise RunnerLeaseLost("Trading runner lease was lost; refusing exchange actions.")

    def release_runner_lease(self) -> None:
        if self._runner_epoch is not None:
            self.database.release_runner_lease(self._runner_token, self._runner_epoch)
            self._runner_epoch = None

    def _reconcile_state(self) -> Dict[str, Any]:
        """Verify the Spot order book and wallet before ordinary trading resumes.

        SQLite remains the order ledger. Memory holds only a verified snapshot;
        every normal trading cycle still fetches live order status by saved ID.
        """
        with self._cycle_lock:
            self.assert_runner_lease()
            if self.database.get_state(SAFETY_MODE_KEY) == LIQUIDATING:
                return {"resumable": False, "unhedged_btc": Decimal(0)}
            try:
                self._load_market_for_engine_control()
                account_live = self._full_account_open_orders()
                tracked = self.database.fetch_active_grids()
                self._audit_open_order_drift(account_live, tracked)
                live = [order for order in account_live if
                        order.get("symbol") in (None, self.config.symbol)]
                balances = self.wallet_balances()
                base = self.market["base"]
                free = balances[base]["free"]
                if free is None:
                    raise TradingHalt("Binance did not return a free BTC balance.")
                if tracked and not self.database.get_state("grid_run"):
                    raise TradingHalt("Open grid rows exist without a saved grid run.")
                by_client: Dict[str, Dict[str, Any]] = {}
                by_exchange: Dict[str, Dict[str, Any]] = {}
                for order in live:
                    if not isinstance(order, dict) or order.get("id") is None:
                        raise TradingHalt("Binance returned an invalid open order.")
                    exchange_id = str(order["id"])
                    client_id = self._client_order_id(order)
                    if exchange_id in by_exchange or (client_id and client_id in by_client):
                        raise TradingHalt("Binance returned duplicate open order IDs.")
                    by_exchange[exchange_id] = order
                    if client_id:
                        by_client[client_id] = order

                restored: Dict[str, Dict[str, Any]] = {}
                for row in tracked:
                    client_id = row.get("client_order_id")
                    exchange_id = row.get("exchange_order_id")
                    by_cid = by_client.get(str(client_id)) if client_id else None
                    by_eid = by_exchange.get(str(exchange_id)) if exchange_id else None
                    if by_cid is not None and by_eid is not None and by_cid is not by_eid:
                        raise TradingHalt("Saved client and exchange order IDs disagree.")
                    order = by_cid or by_eid
                    if order is None:
                        # A fill may have won the restart race. Verify its final
                        # state by exact saved ID before removing it from OPEN.
                        self._reconcile_order(row)
                        if self.database.get_order(row["order_id"])["status"] in (
                                "OPEN", "PARTIALLY_FILLED"):
                            raise TradingHalt("A tracked open order is absent from Binance.")
                        continue
                    if (client_id and self._client_order_id(order) != str(client_id)) or (
                            exchange_id and str(order["id"]) != str(exchange_id)):
                        raise TradingHalt("A saved order ID conflicts with Binance.")
                    exchange_type = str(order.get("type", "")).upper()
                    if exchange_type == "LIMIT_MAKER":
                        exchange_type = "LIMIT"
                    if (str(order.get("side", "")).upper() != row["side"] or
                            exchange_type != row["order_type"] or
                            str(order.get("status", "open")).lower() != "open"):
                        raise TradingHalt("A saved order side, type, or status conflicts with Binance.")
                    amount = _order_decimal(order.get("amount"))
                    price = _order_decimal(order.get("price"))
                    if (not amount.is_finite() or amount != Decimal(row["amount"]) or
                            row["order_type"] == "LIMIT" and
                            (not price.is_finite() or price != Decimal(row["price"]))):
                        raise TradingHalt("A saved order amount or price conflicts with Binance.")
                    if exchange_id is None:
                        self.database.set_exchange_order_id(row["order_id"],
                                                            str(order["id"]))
                    restored[row["order_id"]] = order

                locked = balances[base]["used"]
                if locked is not None:
                    bot_sell_remaining = sum((
                        _order_decimal(order.get("remaining"),
                                       str(order.get("amount") or "0"))
                        for order in restored.values()
                        if str(order.get("side", "")).lower() == "sell"
                    ), Decimal(0))
                    if bot_sell_remaining > locked:
                        raise TradingHalt(
                            "Binance locked BTC is below the saved SELL order total."
                        )
                self.reconciled_orders = restored
                # Only a sellable free balance is actionable. Binance can leave
                # precision dust after a filled SELL.
                unhedged = Decimal(0)
                if free > 0:
                    price = self._ticker_price()
                    if self._sellable_hard_stop_amount(free, price) > 0:
                        unhedged = free
                        LOGGER.critical(
                            "Unhedged %s BTC in Spot wallet; START needs "
                            "manual clearance or explicit auto_cover_inventory.", free,
                        )
                self._record_exchange_audit()
                return {"resumable": bool(restored), "unhedged_btc": unhedged,
                        "covered_btc": bot_sell_remaining if locked is not None else sum((
                            _order_decimal(order.get("remaining"),
                                           str(order.get("amount") or "0"))
                            for order in restored.values()
                            if str(order.get("side", "")).lower() == "sell"
                        ), Decimal(0))}
            except RunnerLeaseLost:
                raise
            except Exception as error:
                message = ("State reconciliation failed: " + str(error) +
                           " Manual intervention is required.")
                with self.database.transaction():
                    self.database.require_order_cleanup(
                        message, trigger_event="RECONCILIATION_FAILED",
                        error_details=_exception_details(error))
                    self.database.set_state(ENGINE_FAULT_KEY, message)
                self.reconciled_orders = {}
                LOGGER.critical(message, exc_info=True)
                raise TradingHalt(message) from error

    def request_initial_grid(self, center_text: str, width_text: str,
                             stop_distance_text: str, capital_text: str,
                             grid_levels: int) -> Tuple[Decimal, Decimal]:
        """Activate a fresh standby bot only after explicit authenticated settings."""
        center = _decimal(center_text, "center_price")
        width_percent = _decimal(width_text, "width_percentage")
        stop_distance = _decimal(stop_distance_text, "stop_loss_percentage")
        capital = _decimal(capital_text, "allocated_capital")
        if width_percent >= 100 or stop_distance >= 100:
            raise ValueError("Grid width and stop-loss distance must be below 100%.")
        if (isinstance(grid_levels, bool) or not isinstance(grid_levels, int) or
                not 2 <= grid_levels <= 2 * MAX_LEVELS):
            raise ValueError("Grid levels must be a whole number from 2 to 100.")
        if capital / grid_levels < Decimal("7"):
            raise ValueError("Order size must be at least 7 USDT.")
        lower = center * (1 - width_percent / 100)
        upper = center * (1 + width_percent / 100)
        stop = center * (1 - stop_distance / 100)
        if not 0 < stop < lower < center < upper:
            raise ValueError("Require hard stop < lower bound < center < upper bound.")
        candidate = replace(
            self.config, lower_price=lower, upper_price=upper,
            stop_loss_price=stop, investment_quote=capital,
            buy_grid_levels=(grid_levels + 1) // 2,
            sell_grid_levels=grid_levels // 2,
        )
        with self._cycle_lock:
            self.assert_runner_lease()
            if (self.grid_activation_requested or
                    self.database.get_state("grid_run") is not None or
                    self.database.get_state("grid_reset") is not None or
                    self.database.fetch_all_orders()):
                raise TradingHalt("A grid is already starting or active.")
            if self._live_open_orders():
                raise TradingHalt("Open BTC/USDT orders exist; resolve them before starting.")
            self._call(self.exchange.load_markets)
            self.market = self.exchange.market(candidate.symbol)
            if not self.market.get("spot") or self.market.get("active") is False:
                raise TradingHalt("Configured symbol is not an active Spot market.")
            current = self._ticker_price()
            if not self._within_grid_bounds(current, candidate, strict=True) or current <= stop:
                raise ValueError("Live price must be inside the new bounds and above the hard stop.")
            buys, sells = configured_grid_levels(center, candidate)
            self._validate_level_prices(center, buys, sells)
            ratio = max(center / buys[0], sells[0] / center)
            if not center / ratio < current < center * ratio:
                raise ValueError("Center must be close to the live market price.")
            self._validate_nominal_grid_sizes(candidate, center, buys, sells)
            lowers, uppers = self._validate_reset_grid(candidate, center, Decimal(0))
            baseline = self._free_balance(self.market["base"])
            run = {"anchor": str(center), "baseline_base": str(baseline),
                   "fingerprint": candidate.fingerprint()}
            active = {"lower": str(lower), "upper": str(upper),
                      "spacing": str(candidate.spacing_percent),
                      "investment_quote": str(capital),
                      "stop_loss_price": str(stop),
                      "buy_levels": candidate.buy_grid_levels,
                      "sell_levels": candidate.sell_grid_levels}
            trailing = {"high_water_mark": str(center),
                        "stop_loss_distance": str(center - stop)}
            request = {"phase": "placing", "source": "manual_initial",
                       "lower": str(lower), "upper": str(upper),
                       "spacing": str(candidate.spacing_percent),
                       "investment_quote": str(capital),
                       "buy_levels": candidate.buy_grid_levels,
                       "sell_levels": candidate.sell_grid_levels}
            self.database.complete_grid_reset(
                json.dumps(run, sort_keys=True),
                json.dumps(active, sort_keys=True),
                json.dumps(request, sort_keys=True),
                breakout_width_percent=str(width_percent),
                trailing_stop=json.dumps(trailing, sort_keys=True),
            )
            self.config = candidate
            self.anchor = center
            self.baseline_base = baseline
            self.levels, self.upper_levels = lowers, uppers
            self.breakout_width_percent = width_percent
            self.high_water_mark = center
            self.stop_loss_distance = center - stop
            self.pending_grid_bounds = (lower, upper)
            self.grid_needs_reset = True
            self.grid_activation_requested = True
            LOGGER.warning("Admin approved a new %s-level grid with %s USDT allocation.",
                           grid_levels, capital)
            return lower, upper

    def factory_reset(self) -> None:
        """Stop trading and clear history only after a fail-closed safety check."""
        with self._cycle_lock:
            self.assert_runner_lease()
            with self._grid_lock:
                mode = self.database.get_state(SAFETY_MODE_KEY)
                if mode in (LIQUIDATING, LIQUIDATION_HALTED):
                    raise TradingHalt("Cannot reset while liquidation is unresolved.")
                if self.grid_needs_reset or self.database.get_state("grid_reset"):
                    raise TradingHalt("Cannot reset while a grid rebuild is in progress.")
                if self._live_open_orders():
                    raise TradingHalt(
                        "Cannot reset: Active orders exist. Clear bot orders for reset "
                        "and resolve any other BTC/USDT orders first."
                    )
                if self.database.fetch_active_grids():
                    raise TradingHalt(
                        "Cannot reset: Tracked orders still need reconciliation."
                    )
                held = _portfolio_wallet(self.database)["btc_held"]
                if held is None:
                    raise TradingHalt(
                        "Cannot reset: Bot-owned BTC inventory cannot be verified."
                    )
                if not math.isfinite(held) or held != 0:
                    raise TradingHalt(
                        "Cannot reset: Bot-owned BTC remains. Resolve it before wiping history."
                    )
                with self.database.transaction():
                    self.database.factory_reset()
                    if self.ready_mode:
                        self.database.set_state(ENGINE_STATUS_KEY, ENGINE_IDLE)
                self.is_paused = True
                self.grid_needs_reset = False
                self.pending_grid_bounds = None

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

    def _full_account_open_orders(self) -> List[Dict[str, Any]]:
        orders = self._call(self.exchange.fetch_open_orders)
        if not isinstance(orders, list):
            raise TradingHalt("Binance returned an invalid account open-order list.")
        return orders

    def _audit_open_order_drift(
        self, live: List[Dict[str, Any]], tracked: List[Dict[str, Any]],
    ) -> None:
        """Cancel a bot-tagged open order absent from the active SQLite ledger."""
        ghosts: List[Dict[str, Any]] = []
        for order in live:
            if not isinstance(order, dict):
                raise TradingHalt("Binance returned an invalid open order.")
            client_id = self._client_order_id(order)
            if not client_id or not client_id.startswith(self.order_client_prefix):
                continue
            exchange_id = order.get("id")
            if any(
                row.get("client_order_id") == client_id and
                order.get("symbol") in (None, self.config.symbol) and
                (row.get("exchange_order_id") is None or
                 str(row["exchange_order_id"]) == str(exchange_id))
                for row in tracked
            ):
                continue
            ghosts.append(order)
        if not ghosts:
            return
        ids = ", ".join(self._client_order_id(order) or "unknown" for order in ghosts)
        message = f"Bot-owned Binance ghost orders absent from active SQLite: {ids}."
        self.database.set_state(GHOST_ORDER_ALARM_KEY, message)
        LOGGER.critical("%s Canceling them and halting for reconciliation.", message)
        unresolved = []
        for order in ghosts:
            exchange_id = order.get("id")
            if exchange_id is None:
                unresolved.append(self._client_order_id(order) or "unknown")
                continue
            try:
                self._call(self.exchange.cancel_order, str(exchange_id),
                           order.get("symbol") or self.config.symbol)
            except RunnerLeaseLost:
                raise
            except ccxt.OrderNotFound:
                pass  # A fill or another cancellation may have won the race.
            except Exception as error:
                LOGGER.error("Ghost order %s cancellation failed: %s",
                             exchange_id, type(error).__name__)
                unresolved.append(self._client_order_id(order) or str(exchange_id))
        try:
            remaining = self._full_account_open_orders()
            open_ids = {self._client_order_id(order) for order in remaining}
            unresolved.extend(
                self._client_order_id(order) or "unknown" for order in ghosts
                if self._client_order_id(order) in open_ids
            )
        except RunnerLeaseLost:
            raise
        except Exception as error:
            LOGGER.error("Ghost order verification failed: %s", type(error).__name__)
            unresolved.append("exchange verification")
        if unresolved:
            raise TradingHalt(message + " Cancellation unverified: " +
                              ", ".join(sorted(set(unresolved))))
        raise TradingHalt(message + " Cancellation verified; review fills before restart.")

    def _record_exchange_audit(self) -> None:
        """Only completed checks advance freshness; failed/skipped audits do not."""
        audited_at = datetime.now(timezone.utc).isoformat()
        self.database.set_state(LAST_EXCHANGE_AUDIT_TIME_KEY, audited_at)
        self.last_exchange_audit_time = audited_at

    def _audit_full_book_if_due(self) -> None:
        """Scan all account Spot open orders, not saved IDs or one symbol only."""
        if self.database.get_state(SAFETY_MODE_KEY) == LIQUIDATING:
            return  # The liquidation state machine owns its in-flight market order.
        now = time.monotonic()
        if self._last_full_book_audit and (
                now - self._last_full_book_audit < FULL_BOOK_AUDIT_SECONDS):
            return
        self.assert_runner_lease()
        live = self._full_account_open_orders()
        self._audit_open_order_drift(live, self.database.fetch_active_grids())
        self._record_exchange_audit()
        self._last_full_book_audit = now

    @staticmethod
    def _client_order_id(order: Dict[str, Any]) -> Optional[str]:
        info = order.get("info")
        client_id = order.get("clientOrderId") or (
            info.get("clientOrderId") if isinstance(info, dict) else None
        )
        return str(client_id) if client_id is not None else None

    def _is_bot_order(
        self, order: Any, tracked: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Recognize new tagged orders and exact IDs saved by older bot versions."""
        if not isinstance(order, dict):
            return False
        client_id = self._client_order_id(order)
        if client_id and client_id.startswith(self.order_client_prefix):
            return True
        exchange_id = order.get("id")
        for row in tracked if tracked is not None else self.database.fetch_active_grids():
            if (client_id and client_id == row.get("client_order_id")) or (
                exchange_id is not None and row.get("exchange_order_id") is not None
                and str(exchange_id) == str(row["exchange_order_id"])
            ):
                return True
        return False

    def _cancel_bot_orders(
        self, *, side: Optional[str] = None,
        selected_ids: Optional[set[str]] = None,
        marker_prefix: Optional[str] = None,
        limit_only: bool = False,
    ) -> Tuple[List[Tuple[str, Tuple[str, ...]]], bool]:
        """Cancel only this bot's live orders and verify tracked SQLite rows."""
        self.assert_runner_lease()
        with self.stop_controller._lock:
            tracked = self.database.fetch_active_grids()
            known_orders = self.database.fetch_all_orders()
            selected = [
                row for row in tracked
                if (side is None or row["side"].lower() == side)
                and (not limit_only or row["order_type"] == "LIMIT")
                and (selected_ids is None or row["order_id"] in selected_ids)
            ]

            def matches(order: Any) -> bool:
                if not self._is_bot_order(order, known_orders):
                    return False
                if side is not None and str(order.get("side") or "").lower() != side:
                    return False
                if limit_only and str(order.get("type") or "").lower() not in (
                    "limit", "limit_maker"
                ):
                    return False
                if selected_ids is None:
                    return True
                return any(
                    (order.get("id") is not None and row.get("exchange_order_id") is not None
                     and str(order["id"]) == str(row["exchange_order_id"])) or
                    (self._client_order_id(order) is not None and
                     self._client_order_id(order) == row.get("client_order_id"))
                    for row in selected
                )

            live = [order for order in self._live_open_orders() if matches(order)]
            for order in live:
                order_id = order.get("id")
                if order_id is None:
                    raise TradingHalt("Bot order has no exchange ID for targeted cancellation.")
                if marker_prefix:
                    for row in selected:
                        if ((row.get("exchange_order_id") is not None and
                             str(order_id) == str(row["exchange_order_id"])) or
                                (self._client_order_id(order) is not None and
                                 self._client_order_id(order) ==
                                 row.get("client_order_id"))):
                            self.database.set_state(
                                f"{marker_prefix}:{row['order_id']}", "1"
                            )
                try:
                    self._call(self.exchange.cancel_order, str(order_id), self.config.symbol)
                except ccxt.OrderNotFound:
                    # A fill can win the race with cancellation; reconcile below.
                    LOGGER.info("Bot order %s disappeared before cancellation.", order_id)

            remaining = any(matches(order) for order in self._live_open_orders())
            events: List[Tuple[str, Tuple[str, ...]]] = []
            for row in selected:
                order = self._fetch_order(row, persist=False)
                status = str(order.get("status") or "").lower()
                if status == "closed":
                    changed = self._persist_order_response(row, order, "FILLED")
                    if changed:
                        self._log_confirmed_fill(row, order)
                        events.append(("filled", (
                            row["order_id"], row["side"],
                            str(order.get("filled") or row["amount"]),
                            str(order.get("average") or order.get("price") or row["price"]),
                        )))
                elif status in ("canceled", "expired", "rejected"):
                    if marker_prefix and status == "canceled" and (
                        self.database.get_state(f"{marker_prefix}:{row['order_id']}") != "1"
                    ):
                        raise TradingHalt("Tracked order canceled outside the expected pause.")
                    self._persist_order_response(row, order, status.upper())
                elif status == "open":
                    self._persist_order_response(
                        row, order, "PARTIALLY_FILLED" if
                        _order_decimal(order.get("filled")) > 0 else None,
                    )
                    remaining = True
                else:
                    raise TradingHalt("Bot order cancellation returned an unknown status.")
            return events, not remaining

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
        with self._cycle_lock, self._grid_lock:
            self.assert_runner_lease()
            if self.database.get_state(SAFETY_MODE_KEY) in (
                    LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED):
                raise TradingHalt("Hard-stop liquidation has locked grid settings.")
            if self._compare_grid_prices(stop_loss, self.config.lower_price) >= 0:
                raise ValueError(
                    "❌ Rejected: Stop-loss must be lower than the current lower bound."
                )
            if stop_loss < self.config.stop_loss_price:
                raise ValueError("The trailing hard stop cannot be lowered.")
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
                "investment_quote": str(new_config.investment_quote),
                "stop_loss_price": str(stop_loss),
            }
            if new_config.buy_grid_levels is not None:
                active["buy_levels"] = new_config.buy_grid_levels
                active["sell_levels"] = new_config.sell_grid_levels
            if self.high_water_mark is None:
                self._restore_trailing_stop()
            if self.high_water_mark is None:
                raise TradingHalt("Trailing stop was not initialized.")
            distance = self.high_water_mark - stop_loss
            if distance <= 0:
                raise ValueError("Stop-loss must remain below the high water mark.")
            self.database.update_runtime_grid_settings(
                json.dumps(saved, sort_keys=True), json.dumps(active, sort_keys=True),
                trailing_stop=json.dumps({
                    "high_water_mark": str(self.high_water_mark),
                    "stop_loss_distance": str(distance),
                }, sort_keys=True),
            )
            self.config = new_config
            self.stop_loss_distance = distance
        return stop_loss

    def request_grid_reset(self, lower_text: str, upper_text: str) -> None:
        lower = _decimal(lower_text, "lower_price")
        upper = _decimal(upper_text, "upper_price")
        if not self.config.stop_loss_price < lower < upper:
            raise ValueError("Require stop-loss < lower < upper.")
        price = self._ticker_price()
        candidate = replace(self.config, lower_price=lower, upper_price=upper,
                            spacing_percent=RESET_SPACING_PERCENT)
        if not self._within_grid_bounds(price, candidate, strict=True):
            raise ValueError("Current price must be inside the new bounds.")
        configured_grid_levels(price, candidate)
        with self._grid_lock:
            if self.stop_controller.stop_requested.is_set():
                raise RuntimeError("Bot is stopping; grid was not changed.")
            if self.grid_needs_reset or self.database.get_state("grid_reset"):
                raise RuntimeError("A grid reset is already in progress.")
            request = {"phase": "canceling", "lower": str(lower),
                       "upper": str(upper), "spacing": str(RESET_SPACING_PERCENT)}
            with self.database.transaction():
                self.database.clear_state(BREAKOUT_TIMER_KEY)
                self.database.set_state("grid_reset", json.dumps(request))
            self.pending_grid_bounds = (lower, upper)
            self.grid_needs_reset = True

    def request_manual_recenter(self, center_text: str, width_text: str,
                                stop_distance_text: Optional[str] = None,
                                allocated_capital_text: Optional[str] = None,
                                grid_levels: Optional[int] = None,
                                ) -> Tuple[Decimal, Decimal]:
        """Queue a persisted reset with explicit bounds and optional pause trigger."""
        center = _decimal(center_text, "center_price")
        width_percent = _decimal(width_text, "width_percentage")
        if width_percent >= 100:
            raise ValueError("Width must be below 100%.")
        width = width_percent / 100
        lower, upper = center * (1 - width), center * (1 + width)
        if (allocated_capital_text is None) != (grid_levels is None):
            raise ValueError("Allocated capital and grid levels must be supplied together.")
        candidate = replace(self.config, lower_price=lower, upper_price=upper)
        if allocated_capital_text is not None:
            capital = _decimal(allocated_capital_text, "allocated_capital")
            if (isinstance(grid_levels, bool) or not isinstance(grid_levels, int) or
                    not 2 <= grid_levels <= 2 * MAX_LEVELS):
                raise ValueError("Grid levels must be a whole number from 2 to 100.")
            if capital / grid_levels < Decimal("7"):
                raise ValueError("Order size must be at least 7 USDT.")
            candidate = replace(
                candidate, investment_quote=capital,
                buy_grid_levels=(grid_levels + 1) // 2,
                sell_grid_levels=grid_levels // 2,
            )
        requested_stop = None
        if stop_distance_text is not None:
            stop_distance = _decimal(stop_distance_text, "stop_loss_percentage")
            if stop_distance >= 100:
                raise ValueError("Stop-loss distance must be below 100%.")
            requested_stop = center * (1 - stop_distance / 100)
            if requested_stop >= lower:
                raise ValueError("Pause trigger must be below the projected lower bound.")
        with self._cycle_lock:
            self.assert_runner_lease()
            with self._grid_lock:
                if self.stop_controller.stop_requested.is_set():
                    raise TradingHalt("Bot is stopping; grid was not changed.")
                if self.database.get_state(SAFETY_MODE_KEY) == LIQUIDATED:
                    if requested_stop is None:
                        raise ValueError("A new hard-stop price is required to reset liquidation.")
                    return self._restart_liquidated_run(
                        center, width_percent, lower, upper, requested_stop, candidate
                    )
                mode = self.database.get_state(SAFETY_MODE_KEY)
                releasing_reset = mode == PAUSED_RESET
                if releasing_reset:
                    self._assert_reset_reanchor_ready()
                    # Exchange verification must finish before the SQLite transaction.
                    live = self._full_account_open_orders()
                    if any(not isinstance(order, dict) for order in live):
                        raise TradingHalt("Binance returned an invalid open order.")
                    known = self.database.fetch_all_orders()
                    if any(self._is_bot_order(order, known) for order in live):
                        raise TradingHalt(
                            "Bot orders remain on Binance; retry order cleanup before re-anchoring."
                        )
                    self._record_exchange_audit()
                if self.is_paused and mode not in (PAUSED_SIZING, PAUSED_RESET):
                    raise TradingHalt("Release Safety Pause before re-anchoring the grid.")
                if self.grid_needs_reset or self.database.get_state("grid_reset"):
                    if self.database.get_state(ENGINE_STATUS_KEY) != ENGINE_IDLE:
                        raise TradingHalt("A grid reset is already in progress.")
                    if self.database.fetch_active_grids() or any(
                            self._is_bot_order(order) for order in self._live_open_orders()):
                        raise TradingHalt(
                            "Saved grid still has live bot orders; STOP and verify "
                            "cancellation before replacing it."
                        )
                if self.database.get_state("grid_run") is None:
                    raise TradingHalt("No active grid run is available.")
                if requested_stop is None and self.config.stop_loss_price >= lower:
                    raise ValueError("New lower bound must exceed the pause trigger.")
                candidate_buys, candidate_sells = configured_grid_levels(center, candidate)
                self._validate_level_prices(center, candidate_buys, candidate_sells)
                self._validate_nominal_grid_sizes(
                    candidate, center, candidate_buys, candidate_sells,
                    carry_amount=self._bot_base_exposure(),
                )
                price = self._ticker_price()
                self.advance_trailing_stop(price)
                if (requested_stop is not None and
                        requested_stop < self.config.stop_loss_price):
                    raise ValueError(RISK_OVERRIDE_DENIED)
                if price <= self.config.stop_loss_price:
                    raise TradingHalt("Market is at the pause trigger; recentering is unavailable.")
                planned_stop = requested_stop or self.config.stop_loss_price
                if not any(self._price(raw) > planned_stop for raw in candidate_buys):
                    raise ValueError("Hard stop leaves no safe BUY grid level.")
                ratio = max(center / candidate_buys[0], candidate_sells[0] / center)
                if not center / ratio < price < center * ratio:
                    raise ValueError("Center must be close to the live market price.")
                self._check_grid_buy_capital(
                    candidate, candidate_buys, planned_stop,
                    seed_required=self._bot_base_exposure() == 0,
                )
                request = {
                    "phase": "canceling", "source": "manual_recenter",
                    "center_price": str(center),
                    "width_percent": str(width_percent),
                    "lower": str(lower), "upper": str(upper),
                    "spacing": str(self.config.spacing_percent),
                    "investment_quote": str(candidate.investment_quote),
                }
                if candidate.buy_grid_levels is not None:
                    request["buy_levels"] = candidate.buy_grid_levels
                    request["sell_levels"] = candidate.sell_grid_levels
                if requested_stop is not None:
                    request["stop_loss_price"] = str(requested_stop)
                with self.database.transaction():
                    if releasing_reset:
                        # Fail closed if another writer changed the safety state during validation.
                        self.assert_runner_lease()
                        self._assert_reset_reanchor_ready()
                    self.database.set_state("grid_reset", json.dumps(request, sort_keys=True))
                    self.database.clear_state(RECONCILE_COVER_KEY)
                    self.database.clear_state(BREAKOUT_TIMER_KEY)
                    if releasing_reset:
                        self.database.clear_state(
                            SAFETY_MODE_KEY, trigger_event="RESET_REANCHOR_QUEUED"
                        )
                        self.database.clear_state(SAFETY_PAUSE_NOTICE_KEY)
                        self.database.clear_state(SAFETY_RECOVERY_NOTICE_KEY)
                        self.database.clear_state(SAFETY_RESUME_NOTICE_KEY)
                self.pending_grid_bounds = (lower, upper)
                self.grid_needs_reset = True
                if releasing_reset:
                    self.is_paused = False
                return lower, upper

    def _assert_reset_reanchor_ready(self) -> None:
        """Release only a clean reset lock; this never starts the engine."""
        if self.database.get_state(SAFETY_MODE_KEY) != PAUSED_RESET:
            raise TradingHalt("Safety mode changed; refresh before re-anchoring.")
        if self.database.get_state(ENGINE_STATUS_KEY) != ENGINE_IDLE:
            raise TradingHalt("STOP the engine before queuing a new Re-anchor Grid.")
        if (self.stop_controller.stop_requested.is_set() or
                self.database.get_state(ENGINE_STOP_CLEANUP_KEY) == "1" or
                self.database.fetch_active_grids()):
            raise TradingHalt("Order cleanup is incomplete; retry cleanup before re-anchoring.")
        if any(self.database.get_state(key) for key in (
                "halt_reason", ENGINE_FAULT_KEY, ORDER_CLEANUP_ALARM_KEY,
                GHOST_ORDER_ALARM_KEY, LIQUIDATION_KEY, LIQUIDATION_ALARM_KEY)):
            raise TradingHalt("An unresolved trading fault requires review before re-anchoring.")

    def _assert_liquidation_reset_ready(self) -> None:
        """A completed, fault-free exit may be replaced by a reviewed new grid."""
        if self.database.get_state(SAFETY_MODE_KEY) != LIQUIDATED:
            raise TradingHalt("Liquidation is unresolved; inspect exchange orders first.")
        if self.database.get_state(ENGINE_STATUS_KEY) == ENGINE_RUNNING:
            raise TradingHalt("STOP the engine before configuring a new grid after liquidation.")
        if (self.stop_controller.stop_requested.is_set() or
                self.database.get_state(ENGINE_STOP_CLEANUP_KEY) == "1" or
                self.database.fetch_active_grids()):
            raise TradingHalt("Order cleanup is incomplete; review it before resetting liquidation.")
        if (self.database.get_state("halt_reason") not in (None, "hard_stop_liquidated") or
                any(self.database.get_state(key) for key in (
                    ENGINE_FAULT_KEY, ORDER_CLEANUP_ALARM_KEY,
                    GHOST_ORDER_ALARM_KEY, LIQUIDATION_ALARM_KEY))):
            raise TradingHalt("An unresolved trading fault requires review before resetting liquidation.")
        try:
            state = json.loads(self.database.get_state(LIQUIDATION_KEY) or "null")
            residual = Decimal(state["residual_base"])
            complete = state["phase"] == "complete" and residual.is_finite() and residual == 0
        except (KeyError, TypeError, ValueError, InvalidOperation):
            complete = False
        if not complete:
            raise TradingHalt("Liquidation is unresolved; inspect exchange orders first.")

    def _restart_liquidated_run(self, center: Decimal, width_percent: Decimal,
                                lower: Decimal, upper: Decimal,
                                stop_loss: Decimal, candidate: GridConfig,
                                ) -> Tuple[Decimal, Decimal]:
        """Only an authenticated recenter may release a completed hard stop."""
        self._assert_liquidation_reset_ready()
        if any(self._is_bot_order(order) for order in self._live_open_orders()):
            raise TradingHalt("Bot orders remain; cannot reset a liquidated grid.")
        if self.database.fetch_active_grids():
            raise TradingHalt("Tracked orders remain active; cannot reset liquidation.")
        price = self._ticker_price()
        candidate_buys, candidate_sells = configured_grid_levels(center, candidate)
        if candidate.buy_grid_levels is not None:
            self._validate_level_prices(center, candidate_buys, candidate_sells)
        ratio = max(center / candidate_buys[0], candidate_sells[0] / center)
        if not center / ratio < price < center * ratio:
            raise ValueError("Center must be close to the live market price.")
        if not stop_loss < lower or not self._within_grid_bounds(price, candidate, strict=True):
            raise ValueError("New hard stop and bounds must contain the live price.")
        new_config = replace(candidate, stop_loss_price=stop_loss)
        lowers, uppers = self._validate_reset_grid(new_config, center, Decimal(0))
        baseline = self._free_balance(self.market["base"])
        run = {"anchor": str(center), "baseline_base": str(baseline),
               "fingerprint": new_config.fingerprint()}
        active = {"lower": str(lower), "upper": str(upper),
                  "spacing": str(new_config.spacing_percent),
                  "investment_quote": str(new_config.investment_quote),
                  "stop_loss_price": str(stop_loss)}
        if new_config.buy_grid_levels is not None:
            active["buy_levels"] = new_config.buy_grid_levels
            active["sell_levels"] = new_config.sell_grid_levels
        trailing = {"high_water_mark": str(center),
                    "stop_loss_distance": str(center - stop_loss)}
        request = {"phase": "placing", "source": "manual_liquidation_reset",
                   "center_price": str(center), "stop_loss_price": str(stop_loss),
                   "lower": str(lower), "upper": str(upper),
                   "spacing": str(new_config.spacing_percent),
                   "investment_quote": str(new_config.investment_quote)}
        if new_config.buy_grid_levels is not None:
            request["buy_levels"] = new_config.buy_grid_levels
            request["sell_levels"] = new_config.sell_grid_levels
        with self.database.transaction():
            self._assert_liquidation_reset_ready()
            self.database.complete_grid_reset(
                json.dumps(run, sort_keys=True), json.dumps(active, sort_keys=True),
                json.dumps(request, sort_keys=True),
                breakout_width_percent=str(width_percent),
                trailing_stop=json.dumps(trailing, sort_keys=True),
                clear_liquidation_state=True,
            )
            self.database.clear_state(LIQUIDATION_ALARM_KEY)
            self.database.clear_state(BREAKOUT_TIMER_KEY)
            if self.ready_mode:
                self.database.set_state(ENGINE_STATUS_KEY, ENGINE_IDLE,
                                        trigger_event="LIQUIDATION_GRID_SAVED")
        self.config = new_config
        self.anchor = center
        self.baseline_base = baseline
        self.levels, self.upper_levels = lowers, uppers
        self.breakout_width_percent = width_percent
        self.high_water_mark = center
        self.stop_loss_distance = center - stop_loss
        self.pending_grid_bounds = (lower, upper)
        self.grid_needs_reset = True
        self.is_paused = False
        LOGGER.warning("Admin reset released a completed hard-stop liquidation.")
        return lower, upper

    def _request_breakout_reset(self, price: Decimal) -> None:
        """Persist a reset intent before any order cancellation can begin."""
        width = self.breakout_width_percent / 100
        lower = price * (1 - width)
        upper = price * (1 + width)
        candidate = replace(self.config, lower_price=lower, upper_price=upper)
        if (not self._within_grid_bounds(price, candidate, strict=True) or
                self.config.stop_loss_price >= price):
            raise TradingHalt("Breakout bounds would violate the hard stop.")
        lowers, _ = configured_grid_levels(price, candidate)
        if candidate.buy_grid_levels is not None and any(
                self._price(raw) <= candidate.stop_loss_price for raw in lowers):
            raise TradingHalt(
                "Trailing stop would remove requested BUY levels from the exact grid."
            )
        if not any(self._price(raw) > self.config.stop_loss_price for raw in lowers):
            raise TradingHalt("Trailing stop leaves no safe BUY grid level.")
        with self._grid_lock:
            if self.stop_controller.stop_requested.is_set() or self.grid_needs_reset:
                return
            request = {
                "phase": "canceling", "source": "breakout",
                "previous_upper": str(self.config.upper_price),
                "width_percent": str(self.breakout_width_percent),
                "lower": str(lower), "upper": str(upper),
                "spacing": str(self.config.spacing_percent),
            }
            with self.database.transaction():
                self.database.set_state("grid_reset", json.dumps(request, sort_keys=True))
                self.database.clear_state(BREAKOUT_TIMER_KEY)
            self.pending_grid_bounds = (lower, upper)
            self.grid_needs_reset = True
            LOGGER.info("Four-hour breakout confirmed at %s; grid reset queued.", price)

    def _observe_upper_breakout(self, price: Decimal, *, now: Optional[float] = None) -> bool:
        """Require four hours of uninterrupted above-bound observations."""
        if (self.is_paused or self.grid_needs_reset or
                self._compare_grid_prices(price, self.config.upper_price) <= 0):
            self.database.clear_state(BREAKOUT_TIMER_KEY)
            return False
        now = time.time() if now is None else now
        if not math.isfinite(now) or now < 0:
            raise TradingHalt("System clock is invalid for breakout tracking.")
        text = self.database.get_state(BREAKOUT_TIMER_KEY)
        prior = None
        if text:
            try:
                prior = json.loads(text)
                started = float(prior["started_at"])
                last = float(prior["last_seen_at"])
                if (not math.isfinite(started) or not math.isfinite(last) or
                        started > last or last > now or
                        prior["grid_fingerprint"] != self.config.breakout_fingerprint() or
                        now - last > max(60, self.config.poll_seconds * 3)):
                    prior = None
            except (TypeError, ValueError, KeyError):
                prior = None
        started = float(prior["started_at"]) if prior else now
        if now - started >= BREAKOUT_COOLDOWN_SECONDS:
            self._request_breakout_reset(price)
            return self.grid_needs_reset
        self.database.set_state(BREAKOUT_TIMER_KEY, json.dumps({
            "started_at": started,
            "last_seen_at": now,
            "grid_fingerprint": self.config.breakout_fingerprint(),
        }, sort_keys=True))
        return False

    def _call(self, method: Any, *args: Any) -> Any:
        if self.database.in_transaction:
            raise RuntimeError("Exchange I/O must occur outside SQLite transactions.")
        self.assert_runner_lease()
        with self.exchange_lock:
            self.assert_runner_lease()
            check_exchange_circuit(self.exchange)
            return method(*args)

    def _ticker_price(self) -> Decimal:
        ticker = self._call(self.exchange.fetch_ticker, self.config.symbol)
        price = _order_decimal(ticker.get("last"))
        if not price.is_finite() or price <= 0:
            raise TradingHalt("Ticker has no valid last price.")
        return price

    def _restore_trailing_stop(self) -> bool:
        """Load saved risk state without preparing old orders or changing its floor."""
        trailing_text = self.database.get_state(TRAILING_STOP_KEY)
        if not trailing_text:
            return False
        try:
            trailing = json.loads(trailing_text)
            high_water_mark = _decimal(trailing["high_water_mark"], "high_water_mark")
            stop_loss_distance = _decimal(trailing["stop_loss_distance"], "stop_loss_distance")
        except (KeyError, TypeError, ValueError) as error:
            raise TradingHalt("Saved trailing stop state is invalid.") from error
        if (high_water_mark <= self.config.stop_loss_price or
                high_water_mark - stop_loss_distance > self.config.stop_loss_price):
            raise TradingHalt("Saved trailing stop conflicts with the active grid.")
        self.high_water_mark = high_water_mark
        self.stop_loss_distance = stop_loss_distance
        return True

    def advance_trailing_stop(self, price: Decimal) -> None:
        """Persist a new high and raised liquidation floor as one grid update."""
        with self._cycle_lock, self._grid_lock:
            self.assert_runner_lease()
            if self.high_water_mark is None or self.stop_loss_distance is None:
                self._restore_trailing_stop()
            if self.high_water_mark is None or self.stop_loss_distance is None:
                raise TradingHalt("Trailing stop was not initialized.")
            if price <= self.high_water_mark:
                return
            if self.database.get_state(SAFETY_MODE_KEY) in (
                    LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED):
                return
            saved_text = self.database.get_state("grid_run")
            if saved_text is None:
                raise TradingHalt("No active grid run is available for trailing.")
            saved = json.loads(saved_text)
            if saved["fingerprint"] != self.config.fingerprint():
                raise TradingHalt("Saved grid settings changed during trailing.")
            proposed_stop = price - self.stop_loss_distance
            new_stop = max(self.config.stop_loss_price, proposed_stop)
            new_config = replace(self.config, stop_loss_price=new_stop)
            saved["fingerprint"] = new_config.fingerprint()
            active = {
                "lower": str(new_config.lower_price),
                "upper": str(new_config.upper_price),
                "spacing": str(new_config.spacing_percent),
                "investment_quote": str(new_config.investment_quote),
                "stop_loss_price": str(new_stop),
            }
            if new_config.buy_grid_levels is not None:
                active["buy_levels"] = new_config.buy_grid_levels
                active["sell_levels"] = new_config.sell_grid_levels
            trailing = {
                "high_water_mark": str(price),
                "stop_loss_distance": str(self.stop_loss_distance),
            }
            self.database.update_runtime_grid_settings(
                json.dumps(saved, sort_keys=True),
                json.dumps(active, sort_keys=True),
                trailing_stop=json.dumps(trailing, sort_keys=True),
                allow_pending_reset=True,
            )
            if new_stop > self.config.stop_loss_price:
                LOGGER.info("Trailing hard stop raised to %s after new high %s.",
                            new_stop, price)
            self.config = new_config
            self.high_water_mark = price

    def prepare(
        self, *, persist: bool
    ) -> Tuple[Decimal, List[Tuple[int, Decimal, Decimal]], List[Tuple[int, Decimal, Decimal]]]:
        self._call(self.exchange.load_markets)
        self.market = self.exchange.market(self.config.symbol)
        if not self.market.get("spot") or self.market.get("active") is False:
            raise TradingHalt("Configured symbol is not an active Spot market.")
        current_price = self._ticker_price()
        if persist:
            self.database.set_state(LAST_MARKET_PRICE_KEY, str(current_price))

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
            if not self._within_grid_bounds(current_price, strict=True):
                raise ValueError(
                    f"Current price {current_price} must be inside "
                    f"{self.config.lower_price}–{self.config.upper_price}."
                )
            self.anchor = current_price
            self.baseline_base = self._free_balance(self.market["base"])

        if not self._within_grid_bounds(self.anchor, strict=True):
            raise TradingHalt("Saved anchor lies outside the configured grid bounds.")
        trailing_restored = self._restore_trailing_stop()
        if not trailing_restored:
            self.high_water_mark = self.anchor
            self.stop_loss_distance = self.anchor - self.config.stop_loss_price
            if self.stop_loss_distance <= 0:
                raise TradingHalt("Grid anchor must exceed the hard stop.")
        self.levels, self.upper_levels = configured_grid_levels(self.anchor, self.config)
        if self.config.buy_grid_levels is not None:
            self._validate_level_prices(self.anchor, self.levels, self.upper_levels)
        sizing_paused = self.database.get_state(SAFETY_MODE_KEY) == PAUSED_SIZING
        seed_quote = self._seed_quote()
        quote_per_level = self._lower_quote_per_level()
        seed_amount = self._amount(seed_quote / current_price)
        if not sizing_paused:
            self._check_order_size(current_price, seed_amount)
        planned: List[Tuple[int, Decimal, Decimal]] = []
        for level, raw_price in enumerate(self.levels, start=1):
            price = self._price(raw_price)
            if price <= self.config.stop_loss_price:
                continue
            amount = self._amount(quote_per_level / price)
            if not sizing_paused:
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
            if not sizing_paused:
                self._check_order_size(price, upper_amount)
            upper_planned.append((-index, price, upper_amount))

        if persist and not saved:
            self._check_grid_buy_capital(
                self.config, self.levels, self.config.stop_loss_price,
                seed_required=True,
            )
            state = {
                "anchor": str(self.anchor),
                "baseline_base": str(self.baseline_base),
                "fingerprint": self.config.fingerprint(),
            }
        if persist:
            with self.database.transaction():
                if not saved:
                    self.database.set_state("grid_run", json.dumps(state, sort_keys=True))
                if not trailing_restored:
                    self.database.set_state(TRAILING_STOP_KEY, json.dumps({
                        "high_water_mark": str(self.high_water_mark),
                        "stop_loss_distance": str(self.stop_loss_distance),
                    }, sort_keys=True))
                if self.database.get_state(BREAKOUT_WIDTH_KEY) is None:
                    self.database.set_state(BREAKOUT_WIDTH_KEY, str(self.breakout_width_percent))
        return current_price, planned, upper_planned

    def _seed_quote(self) -> Decimal:
        return self.config.investment_quote * self.config.initial_inventory_percent / 100

    def _lower_quote_per_level(self) -> Decimal:
        lower_budget = self.config.investment_quote - self._seed_quote()
        return lower_budget / Decimal(len(self.levels))

    def _required_buy_limit_quote(
        self, config: GridConfig, lowers: List[Decimal], stop_loss: Decimal,
    ) -> Decimal:
        """Sum the actual exchange-rounded price times amount for each BUY limit."""
        seed_quote = config.investment_quote * config.initial_inventory_percent / 100
        quote_per_level = (config.investment_quote - seed_quote) / len(lowers)
        required = Decimal(0)
        for raw in lowers:
            price = self._price(raw)
            if price <= stop_loss:
                continue
            amount = self._amount(quote_per_level / price)
            self._check_order_size(price, amount)
            cost = price * amount
            if cost > quote_per_level:
                raise TradingHalt("A rounded grid order exceeds its quote allocation.")
            required += cost
        return required

    def _check_grid_buy_capital(
        self, config: GridConfig, lowers: List[Decimal], stop_loss: Decimal,
        *, seed_required: bool,
    ) -> None:
        required = self._required_buy_limit_quote(config, lowers, stop_loss)
        if seed_required:
            required += config.investment_quote * config.initial_inventory_percent / 100
        available = self._free_balance(self.market["quote"])
        if available < required:
            raise InsufficientGridCapital(
                "Insufficient Capital: Grid requires "
                f"{format(required, 'f')} {self.market['quote']} for BUY limits, "
                f"but only {format(available, 'f')} {self.market['quote']} "
                "is available in the Spot wallet."
            )

    def _price(self, value: Decimal) -> Decimal:
        """Apply the loaded market's price precision (Binance PRICE_FILTER tickSize)."""
        return Decimal(self.exchange.price_to_precision(self.config.symbol, str(value)))

    def _compare_grid_prices(self, left: Decimal, right: Decimal) -> int:
        """Compare exchange ticks, avoiding float artifacts retained in saved bounds."""
        left_tick, right_tick = self._price(left), self._price(right)
        return (left_tick > right_tick) - (left_tick < right_tick)

    def _within_grid_bounds(self, price: Decimal, config: Optional[GridConfig] = None,
                            *, strict: bool = False) -> bool:
        config = config or self.config
        lower = self._compare_grid_prices(price, config.lower_price)
        upper = self._compare_grid_prices(price, config.upper_price)
        return lower > 0 and upper < 0 if strict else lower >= 0 and upper <= 0

    def _grid_order_price(self, target: Decimal, side: str) -> Decimal:
        """Round orders and both bounds identically before allowing submission."""
        price = self._price(target)
        if self._compare_grid_prices(price, self.config.lower_price) < 0:
            raise TradingHalt(f"Planned {side.lower()} is below the lower grid bound.")
        if self._compare_grid_prices(price, self.config.upper_price) > 0:
            raise TradingHalt(f"Planned {side.lower()} is above the upper grid bound.")
        return price

    def _amount(self, value: Decimal) -> Decimal:
        return Decimal(self.exchange.amount_to_precision(self.config.symbol, str(value)))

    def _check_order_size(self, price: Decimal, amount: Decimal) -> None:
        if price <= 0 or amount <= 0:
            raise GridSizingError("Order price or amount rounded to zero.")
        limits = self.market.get("limits") or {}
        amount_min = (limits.get("amount") or {}).get("min")
        cost_min = (limits.get("cost") or {}).get("min")
        if amount_min is not None and amount < _order_decimal(amount_min):
            raise GridSizingError("Order amount is below the market minimum.")
        notional = price * amount
        if cost_min is not None and notional < _order_decimal(cost_min):
            raise GridSizingError("Order notional is below the market minimum.")
        if notional < MIN_GRID_ORDER_NOTIONAL:
            raise GridSizingError(
                f"Rounded order is {notional:.4f} USDT at {price} x {amount} BTC; "
                f"each grid order must be at least {MIN_GRID_ORDER_NOTIONAL} USDT. "
                "Increase allocated capital or explicitly choose fewer grid levels."
            )

    def _validate_level_prices(
        self, center: Decimal, buys: List[Decimal], sells: List[Decimal],
    ) -> None:
        """Reject exact levels that collapse onto one exchange price tick."""
        buy_prices = [self._price(price) for price in buys]
        sell_prices = [self._price(price) for price in sells]
        center = self._price(center)
        if (len(set(buy_prices)) != len(buy_prices) or
                len(set(sell_prices)) != len(sell_prices) or
                any(price >= center for price in buy_prices) or
                any(price <= center for price in sell_prices)):
            raise ValueError("Requested grid levels collapse at exchange price precision.")

    def _validate_nominal_grid_sizes(
        self, config: GridConfig, center: Decimal,
        buys: List[Decimal], sells: List[Decimal],
        *, carry_amount: Decimal = Decimal(0),
    ) -> None:
        """Check rounded per-side orders before any existing order is canceled."""
        seed_quote = config.investment_quote * config.initial_inventory_percent / 100
        if carry_amount <= 0:
            self._check_order_size(center, self._amount(seed_quote / center))
        lower_quote = (config.investment_quote - seed_quote) / len(buys)
        for raw in buys:
            price = self._price(raw)
            buy_amount = self._amount(lower_quote / price)
            self._check_order_size(price, buy_amount)
            conservative_sell = self._amount(buy_amount * (1 - SELL_AMOUNT_BUFFER))
            sell_target = self._price(raw / (1 - config.spacing_percent / 100))
            self._check_order_size(sell_target, conservative_sell)
        upper_total = carry_amount if carry_amount > 0 else self._amount(seed_quote / center)
        upper_amount = self._amount(upper_total * (1 - SELL_AMOUNT_BUFFER) / len(sells))
        for raw in sells:
            price = self._price(raw)
            checked_amount = upper_amount
            try:
                self._check_order_size(price, checked_amount)
                checked_amount = self._amount(upper_amount * (1 - SELL_AMOUNT_BUFFER))
                self._check_order_size(price, checked_amount)
            except GridSizingError as error:
                if carry_amount <= 0:
                    raise
                raise GridSizingError(
                    f"Existing BTC ({carry_amount}) cannot support {len(sells)} SELL levels "
                    "after exchange rounding and fee reserves: "
                    f"{checked_amount} BTC at {price} is {checked_amount * price:.4f} USDT "
                    f"and fails order-size rules (bot minimum {MIN_GRID_ORDER_NOTIONAL} USDT, "
                    "plus exchange limits). Choose fewer total grid levels. "
                    "Increasing allocated USDT funds BUY limits; it does not top up "
                    "the existing BTC used for SELL limits during a re-anchor."
                ) from error

    def _fetch_order(self, row: Dict[str, Any], *, persist: bool = True) -> Dict[str, Any]:
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
        if persist:
            self._persist_order_response(row, order)
        return order

    def _persist_order_response(self, row: Dict[str, Any], order: Dict[str, Any],
                                status: Optional[str] = None) -> bool:
        """Commit confirmed identity, fill accounting, and order status together."""
        with self.database.transaction():
            if order.get("id") and not row.get("exchange_order_id"):
                self.database.set_exchange_order_id(row["order_id"], str(order["id"]))
            self._save_fill_snapshot(row["order_id"], order)
            if status is not None:
                changed = self.database.update_order_status(row["order_id"], status)
                if changed and status == "FILLED" and row["side"] == "BUY":
                    self.database.set_state(f"telegram_buy_pending:{row['order_id']}", "1")
                return changed
        return False

    def _save_fill_snapshot(
        self, order_id: str, order: Dict[str, Any],
        *, verified_fees: Optional[Tuple[Decimal, Decimal]] = None,
    ) -> None:
        save_fill_snapshot(
            self.database, self.market["base"], self.market["quote"],
            order_id, order, verified_fees=verified_fees,
        )

    def _buy_fee_in_asset(
        self, row: Dict[str, Any], buy: Dict[str, Any], asset: str,
    ) -> Decimal:
        """Prefer verified per-trade commission over an order with missing fees."""
        snapshot_text = self.database.get_state(FILL_SNAPSHOT_PREFIX + row["order_id"])
        if snapshot_text:
            snapshot = json.loads(snapshot_text)
            if (snapshot.get("fee_source") == "trades" and
                    Decimal(snapshot["filled_base"]) == _order_decimal(buy.get("filled"))):
                if asset == self.market["base"]:
                    return Decimal(snapshot["base_fee"])
                if asset == self.market["quote"]:
                    return Decimal(snapshot["quote_fee"])
        return _fees_in_asset(buy, asset)

    def _backfill_portfolio_snapshots(self) -> None:
        """Read old completed orders once so existing runs have a cost basis."""
        for row in self.database.fetch_all_orders():
            if (not row.get("client_order_id") or row["status"] == "OPEN" or
                    self.database.get_state(FILL_SNAPSHOT_PREFIX + row["order_id"])):
                continue
            try:
                self._fetch_order(row)
            except Exception as error:
                LOGGER.warning("Could not backfill fill for %s: %s",
                               row["order_id"], type(error).__name__)

    def _log_confirmed_fill(self, row: Dict[str, Any], order: Dict[str, Any]) -> None:
        amount = order.get("filled") or row["amount"]
        price = order.get("average") or order.get("price") or row["price"]
        LOGGER.info("Order FILLED: %s %s %s at %s; order %s. Accounting follows locally.",
                    row["side"], amount, self.config.symbol, price, row["order_id"])

    def _alert_accounted_buy(self, row: Dict[str, Any], net_base: Decimal) -> None:
        """Notify only new BUY fills after their durable cost/commission snapshot."""
        if self.database.in_transaction:
            return  # The caller's outer transaction has not committed yet.
        order_id = row["order_id"]
        pending_key = f"telegram_buy_pending:{order_id}"
        notice_key = f"telegram_buy_scheduled:{order_id}"
        if (not self.database.get_state(pending_key) or
                self.database.get_state(notice_key)):
            return
        snapshot_text = self.database.get_state(FILL_SNAPSHOT_PREFIX + order_id)
        if not snapshot_text:
            return
        try:
            snapshot = json.loads(snapshot_text)
            if not isinstance(snapshot, dict):
                raise ValueError("Fill accounting must be an object.")
            if (getattr(self.exchange, "has", {}).get("fetchOrderTrades") and
                    snapshot.get("fee_source") != "trades"):
                return  # Binance commission details have not been reconciled yet.
            filled = Decimal(snapshot["filled_base"])
            quote_cost = Decimal(snapshot["filled_quote"])
            quote_fee = Decimal(snapshot["quote_fee"])
            base_fee = Decimal(snapshot["base_fee"])
            accounted_base = filled - base_fee
        except (KeyError, TypeError, ValueError, InvalidOperation):
            LOGGER.warning("BUY alert awaits valid fill accounting for %s.", order_id)
            return
        if (not all(value.is_finite() for value in (filled, quote_cost, quote_fee, base_fee, net_base)) or
                filled <= 0 or quote_cost <= 0 or quote_fee < 0 or base_fee < 0
                or accounted_base <= 0 or net_base <= 0):
            return
        queued = schedule_telegram_alert(
            f"*BUY FILLED*\nPair: {markdown_text(self.config.symbol)}\nSide: BUY"
            f"\nPrice: {markdown_text(quote_cost / filled)}"
            f"\nAmount: {markdown_text(filled)}"
            f"\nNet inventory: {markdown_text(accounted_base)} {markdown_text(self.market['base'])}"
            f"\nCost basis: {markdown_text(quote_cost + quote_fee)} {markdown_text(self.market['quote'])}"
            f"\nRealized P&L: 0 {markdown_text(self.market['quote'])}"
            f"\n{markdown_text('BUY adds inventory; profit is realized on SELL.')}"
            f"\nOrder: {markdown_text(order_id)}"
        )
        if queued:
            with self.database.transaction():
                self.database.set_state(notice_key, "1")
                self.database.clear_state(pending_key)

    def _alert_realized_sell(self, order_id: str, price: Decimal,
                              amount: Decimal, profit: Decimal, *, filled: bool = True) -> None:
        if self.database.in_transaction:
            return  # Never publish accounting that an outer transaction can roll back.
        trade = self.database.get_trade_by_sell_order_id(order_id)
        if trade is None:
            return
        price = Decimal(trade["sell_price"])
        profit = Decimal(trade["profit"])
        if trade["sold_base"] is not None:
            amount = Decimal(trade["sold_base"])
        notice_key = f"telegram_pnl_scheduled:{order_id}"
        if self.database.get_state(notice_key):
            return
        event = "SELL FILLED" if filled else "SELL PARTIAL EXECUTION"
        queued = schedule_telegram_alert(
            f"*{event} — Realized P&L*\nPair: {markdown_text(self.config.symbol)}\nSide: SELL"
            f"\nPrice: {markdown_text(price)}\nAmount: {markdown_text(amount)}"
            f"\nRealized P&L: {markdown_text(profit)} {markdown_text(self.market['quote'])}"
            f"\nOrder: {markdown_text(order_id)}"
        )
        if queued:
            self.database.set_state(notice_key, "1")

    def _reconcile_order(self, row: Dict[str, Any]) -> Optional[Tuple[str, Tuple[str, ...]]]:
        order = self._fetch_order(row, persist=False)
        status = order.get("status")
        if status == "open":
            if _order_decimal(order.get("filled")) > 0 and row["status"] == "OPEN":
                self._persist_order_response(row, order, "PARTIALLY_FILLED")
            else:
                self._persist_order_response(row, order)
            return None
        if status == "closed":
            changed = self._persist_order_response(row, order, "FILLED")
            if changed:
                self._log_confirmed_fill(row, order)
                amount = str(order.get("filled") or row["amount"])
                price = str(order.get("average") or order.get("price") or row["price"])
                return ("filled", (row["order_id"], row["side"], amount, price))
            return None
        if status in ("canceled", "expired", "rejected"):
            self._persist_order_response(row, order, status.upper())
            if (status == "canceled" and row["side"] == "BUY" and
                    self.database.get_state(f"safety_pause_buy:{row['order_id']}") == "1"):
                return None
            raise TradingHalt(f"Tracked order {row['order_id']} ended as {status}.")
        raise TradingHalt("Exchange returned an unknown order status.")

    def _submit_order(
        self, level: int, side: str, price: Decimal, amount: Decimal,
        *, parent_order_id: Optional[str] = None, order_type: str = "LIMIT",
        quote_cost: Optional[Decimal] = None,
    ) -> Optional[str]:
        if self.database.in_transaction:
            raise RuntimeError("Order intents must commit before exchange submission.")
        client_id = self.order_client_prefix + uuid.uuid4().hex[:20]
        with self.exchange_lock:
            self.assert_runner_lease()
            check_exchange_circuit(self.exchange)
            liquidation = order_type == "MARKET" and side == "SELL" and level == -1
            if self.stop_controller.stop_requested.is_set() and not liquidation:
                raise TradingHalt("Stop was requested before order submission.")
            self.database.insert_order(
                client_id, level, side, price, amount,
                client_order_id=client_id, parent_order_id=parent_order_id,
                order_type=order_type,
            )
            previous_write_attempts = exchange_write_attempts(self.exchange)
            try:
                self.assert_runner_lease()
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
            except CircuitOpenError as error:
                if exchange_write_attempts(self.exchange) != previous_write_attempts:
                    raise UncertainOrderError(
                        f"Order {client_id} may have been accepted before a blocked retry; "
                        "inspect it before restarting."
                    ) from error
                # A concurrent HTTP call may trip the breaker after the intent
                # was saved. A local rejection guarantees no order was sent.
                self.database.discard_unsubmitted_order(client_id)
                raise
            except ccxt.OrderImmediatelyFillable as error:
                if (order_type != "LIMIT" or
                        "Order would immediately match and take." not in str(error)):
                    raise UncertainOrderError(
                        f"Order {client_id} may have been accepted; inspect it before restarting."
                    ) from error
                self.database.discard_rejected_post_only_order(client_id)
                self._post_only_rejected_in_cycle = True
                LOGGER.warning(
                    "Spot rejected post-only %s %s at %s (grid level %s); "
                    "skipping this order until the next cycle.",
                    side, amount, price, level,
                )
                return None
            except ccxt.InsufficientFunds as error:
                if order_type != "LIMIT":
                    raise UncertainOrderError(
                        f"Order {client_id} may have been accepted; inspect it before restarting."
                    ) from error
                # A definite balance rejection did not create an exchange
                # order. Keep the lane ready for a fresh balance check next cycle.
                self.database.discard_rejected_post_only_order(client_id)
                self._post_only_rejected_in_cycle = True
                LOGGER.warning(
                    "Spot rejected %s %s at %s for insufficient free %s "
                    "(grid level %s); retrying after balance reconciliation.",
                    side, amount, price,
                    self.market["base"] if side == "SELL" else self.market["quote"],
                    level,
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
            "Spot submitted %s %s %s %s at %s (grid level %s).",
            order_type, side, amount, self.config.symbol, price, level,
        )
        return client_id

    def _free_balance(self, asset: str) -> Decimal:
        balances = self._call(self.exchange.fetch_balance, {"type": "spot"})
        try:
            free = _order_decimal((balances.get(asset) or {}).get("free"))
        except (AttributeError, InvalidOperation, TypeError) as error:
            raise TradingHalt(f"Exchange returned an invalid free {asset} balance.") from error
        if not free.is_finite() or free < 0:
            raise TradingHalt(f"Exchange returned an invalid free {asset} balance.")
        return free

    def _free_bot_base(self) -> Decimal:
        if self.baseline_base is None:
            raise TradingHalt("Starting base balance has not been recorded.")
        return max(
            Decimal(0), self._free_balance(self.market["base"]) - self.baseline_base
        )

    def _net_buy_fill_base(self, row: Dict[str, Any], buy: Dict[str, Any]) -> Optional[Decimal]:
        """Use the BUY's executed BTC less its actual BTC-denominated commission."""
        filled = _order_decimal(buy.get("filled"))
        if not filled.is_finite() or filled <= 0:
            raise TradingHalt("Filled buy has no valid executed BTC amount.")
        base = self.market["base"]
        snapshot_text = self.database.get_state(FILL_SNAPSHOT_PREFIX + row["order_id"])
        snapshot = json.loads(snapshot_text) if snapshot_text else {}
        carry_text = self.database.get_state("carry_inventory")
        carry = json.loads(carry_text) if carry_text else {}
        if (carry.get("order_id") == row["order_id"] and
                not row.get("client_order_id")):
            # This row already represents net BTC carried from earlier fills.
            # Its carry-* ID is local and must never be sent to Binance as orderId.
            base_fee = Decimal(0)
        elif (snapshot.get("fee_source") == "trades" and
                Decimal(snapshot["filled_base"]) == filled):
            base_fee = Decimal(snapshot["base_fee"])
        elif getattr(self.exchange, "has", {}).get("fetchOrderTrades"):
            exchange_id = row.get("exchange_order_id") or buy.get("id")
            if not exchange_id:
                raise TradingHalt("Filled buy has no exchange ID for commission lookup.")
            trades = self._call(
                self.exchange.fetch_order_trades, str(exchange_id), self.config.symbol
            )
            if not isinstance(trades, list):
                raise TradingHalt("Exchange returned invalid BUY execution details.")
            executed = base_fee = quote_fee = Decimal(0)
            for trade in trades:
                if not isinstance(trade, dict):
                    raise TradingHalt("Exchange returned an invalid BUY execution.")
                trade_order = trade.get("order")
                if trade_order is not None and str(trade_order) != str(exchange_id):
                    raise TradingHalt("BUY execution belongs to another order.")
                info = trade.get("info") or {}
                if not isinstance(info, dict):
                    raise TradingHalt("BUY execution has invalid exchange details.")
                # Binance's raw strings retain the full commission and fill
                # precision even when CCXT's unified fields are floats.
                quantity = _order_decimal(info.get("qty", trade.get("amount")))
                if not quantity.is_finite() or quantity <= 0:
                    raise TradingHalt("BUY execution has an invalid amount.")
                if "commission" in info and "commissionAsset" in info:
                    commission = (_order_decimal(info["commission"])
                                  if info["commissionAsset"] == base else Decimal(0))
                    quote_commission = (_order_decimal(info["commission"])
                                        if info["commissionAsset"] == self.market["quote"]
                                        else Decimal(0))
                elif trade.get("fee") or trade.get("fees"):
                    commission = _fees_in_asset(trade, base)
                    quote_commission = _fees_in_asset(trade, self.market["quote"])
                else:
                    LOGGER.info("BUY commission is not available yet for %s.", row["order_id"])
                    return None
                if (not commission.is_finite() or commission < 0 or
                        not quote_commission.is_finite() or quote_commission < 0):
                    raise TradingHalt("BUY execution has an invalid commission.")
                executed += quantity
                base_fee += commission
                quote_fee += quote_commission
            if executed == 0 or (executed < filled and
                                  self._amount(executed) < self._amount(filled)):
                LOGGER.info("Waiting for all BUY executions for %s.", row["order_id"])
                return None
            if executed > filled and self._amount(executed) > self._amount(filled):
                raise TradingHalt("BUY executions exceed the confirmed filled amount.")
            self._save_fill_snapshot(
                row["order_id"], buy, verified_fees=(base_fee, quote_fee)
            )
        else:
            # Older test doubles and exchanges without trade detail retain the
            # conservative reserve; Binance Spot exposes per-order trades.
            base_fee = max(_fees_in_asset(buy, base), filled * SELL_AMOUNT_BUFFER)
        net_base = filled - base_fee
        if not net_base.is_finite() or net_base <= 0 or net_base > filled:
            raise TradingHalt("BUY commission leaves no valid BTC for a SELL.")
        self._alert_accounted_buy(row, net_base)
        return net_base

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
        amount = self._seed_upper_sell_amount(seed_row)
        if amount is None:
            return
        target = (self.config.upper_price if self.database.get_state(RECONCILE_COVER_KEY)
                  else self.upper_levels[-level - 1])
        price = self._grid_order_price(target, "SELL")
        self._check_order_size(price, amount)
        if self._free_bot_base() < amount:
            return  # Account balance can lag a newly filled market buy.
        if not self.stop_controller.stop_requested.is_set():
            self._submit_order(
                level, "SELL", price, amount,
                parent_order_id=seed_row["order_id"],
            )

    def _seed_upper_sell_amount(self, seed_row: Dict[str, Any]) -> Optional[Decimal]:
        seed = self._fetch_order(seed_row)
        net_base = self._net_buy_fill_base(seed_row, seed)
        if net_base is None:
            return None
        allocated_base = net_base / Decimal(len(self.upper_levels))
        return self._amount(allocated_base)

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
        price = self._grid_order_price(price, "BUY")
        if price <= self.config.stop_loss_price:
            return  # Never create a BUY at or beneath the liquidation floor.
        self._check_order_size(price, amount)
        available_quote = self._free_balance(self.market["quote"])
        if available_quote < price * amount:
            LOGGER.info(
                "Waiting for free %s before BUY at level %s: need %s, free %s.",
                self.market["quote"], level, price * amount, available_quote,
            )
            return
        if not self.stop_controller.stop_requested.is_set():
            self._submit_order(level, "BUY", price, amount, parent_order_id=parent_order_id)

    def _place_sell(self, row: Dict[str, Any]) -> None:
        buy = self._fetch_order(row)
        net_base = self._net_buy_fill_base(row, buy)
        if net_base is None:
            return  # Wait for Binance's per-trade commissions to become available.
        amount = self._amount(net_base)
        if amount > net_base:
            raise TradingHalt("Exchange precision rounded a SELL above net received BTC.")
        if row["level"] > 0:
            # Exact-level grids derive their own geometric spacing; the legacy
            # configured percentage is not the distance to the next SELL.
            target = (self.anchor if row["level"] == 1
                      else self.levels[row["level"] - 2])
        else:
            target = self.upper_levels[-row["level"] - 1]
        if self.database.get_state(RECONCILE_COVER_KEY):
            target = self.config.upper_price
        price = self._grid_order_price(target, "SELL")
        free_bot_base = self._free_bot_base()
        if amount > free_bot_base:
            LOGGER.info(
                "Waiting for free bot BTC before SELL at level %s: need %s, free %s.",
                row["level"], amount, free_bot_base,
            )
            return  # Recheck after the account balance catches up with the fill.
        self._check_order_size(price, amount)
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
        buy = self._fetch_order(parent, persist=False)
        sell = self._fetch_order(row, persist=False)
        with self.database.transaction():
            self._persist_order_response(parent, buy)
            self._persist_order_response(row, sell, "FILLED")
            buy_filled = _order_decimal(buy.get("filled"))
            sell_filled = _order_decimal(sell.get("filled"))
            if buy_filled <= 0 or sell_filled <= 0:
                raise TradingHalt("Filled trade has no executed amount.")
            buy_price = _order_decimal(buy.get("average") or buy.get("price"))
            sell_price = _order_decimal(sell.get("average") or sell.get("price"))
            net_base = buy_filled - self._buy_fee_in_asset(parent, buy, self.market["base"])
            sell_base_fee = _fees_in_asset(sell, self.market["base"])
            consumed_base = sell_filled + sell_base_fee
            if net_base <= 0 or consumed_base > net_base:
                raise TradingHalt("Sell amount exceeds the tracked buy's net base amount.")
            allocation = consumed_base / net_base
            buy_cost = _order_decimal(buy.get("cost"), str(buy_price * buy_filled))
            sell_cost = _order_decimal(sell.get("cost"), str(sell_price * sell_filled))
            buy_base_fee = self._buy_fee_in_asset(parent, buy, self.market["base"])
            buy_quote_fee = self._buy_fee_in_asset(parent, buy, self.market["quote"])
            carry_text = self.database.get_state("carry_inventory")
            carry = json.loads(carry_text) if carry_text else {}
            if carry.get("order_id") == parent_id and not parent.get("client_order_id"):
                buy_fee, buy_fee_actual = Decimal(0), True  # Rolled-up cost basis.
            else:
                buy_fee, buy_fee_actual = _pnl_fee_in_quote(
                    buy, self.market["base"], self.market["quote"],
                    allocation * buy_cost, buy_base_fee, allocation * buy_quote_fee,
                )
            sell_fee, sell_fee_actual = _pnl_fee_in_quote(
                sell, self.market["base"], self.market["quote"],
                sell_cost, sell_base_fee, _fees_in_asset(sell, self.market["quote"]),
            )
            profit = (
                sell_cost - sell_fee - allocation * buy_cost - buy_fee
            )
            self.database.record_trade(
                buy_price, sell_price, profit, sell_order_id=row["order_id"],
                buy_order_id=parent_id, sold_base=sell_filled,
                fee_basis=("exchange" if buy_fee_actual and sell_fee_actual else
                           "estimated" if not buy_fee_actual and not sell_fee_actual else "mixed"),
            )
        self._alert_realized_sell(row["order_id"], sell_price, sell_filled, profit)

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
                    filled - self._buy_fee_in_asset(row, order, self.market["base"]),
                    filled * (1 - SELL_AMOUNT_BUFFER),
                )
            else:
                net_base -= filled
        return max(Decimal(0), net_base)

    def _carry_inventory(self, *, require_available: bool = True) -> Tuple[Decimal, Decimal]:
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
            sold_by_buy[parent] = (sold_by_buy.get(parent, Decimal(0)) + sold +
                                   _fees_in_asset(sell, self.market["base"]))
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
            net = filled - self._buy_fee_in_asset(row, buy, self.market["base"])
            # Use the accounting already available here; notification delivery
            # must not add commission HTTP lookups to emergency liquidation.
            self._alert_accounted_buy(row, net)
            remaining = net - sold_by_buy.get(row["order_id"], Decimal(0))
            if remaining < 0:
                raise TradingHalt("Sold BTC exceeds a tracked buy lot.")
            if net <= 0 or remaining == 0:
                continue
            spent = _order_decimal(buy.get("cost"), str(filled *
                                   _order_decimal(buy.get("average") or buy.get("price"))))
            spent += self._buy_fee_in_asset(row, buy, self.market["quote"])
            amount += remaining
            cost += spent * remaining / net
        if amount > 0 and cost <= 0:
            raise TradingHalt("Carried BTC has no verifiable cost basis.")
        if require_available and amount > self._free_bot_base():
            raise TradingHalt("Tracked BTC is not fully available after cancellation.")
        return amount, cost

    def _validate_reset_grid(self, new_config: GridConfig, price: Decimal,
                             carry_amount: Decimal) -> Tuple[List[Decimal], List[Decimal]]:
        lowers, uppers = configured_grid_levels(price, new_config)
        if new_config.buy_grid_levels is not None:
            self._validate_level_prices(price, lowers, uppers)
            lowest_buy = min(self._price(raw) for raw in lowers)
            if lowest_buy <= new_config.stop_loss_price:
                raise GridStopConflict(lowest_buy, new_config.stop_loss_price)
        if not any(self._price(raw) > new_config.stop_loss_price for raw in lowers):
            raise TradingHalt("Trailing stop leaves no safe BUY grid level.")
        seed_quote = new_config.investment_quote * new_config.initial_inventory_percent / 100
        lower_quote = (new_config.investment_quote - seed_quote) / len(lowers)
        for raw in lowers:
            level_price = self._price(raw)
            if level_price <= new_config.stop_loss_price:
                continue
            self._check_order_size(level_price, self._amount(lower_quote / level_price))
        upper_total = carry_amount if carry_amount > 0 else self._amount(seed_quote / price)
        upper_amount = self._amount(upper_total * (1 - SELL_AMOUNT_BUFFER) / len(uppers))
        for raw in uppers:
            self._check_order_size(self._price(raw), upper_amount)
        self._check_grid_buy_capital(
            new_config, lowers, new_config.stop_loss_price,
            seed_required=carry_amount == 0,
        )
        if carry_amount > 0:
            required_base = upper_amount * len(uppers)
            available_base = self._free_bot_base()
            if available_base < required_base:
                raise TradingHalt(
                    f"Insufficient bot BTC for SELL limits: requires {required_base}, "
                    f"but only {available_base} is free after cancellation."
                )
        return lowers, uppers

    def reset_grid(self) -> bool:
        with self._cycle_lock:
            self.assert_runner_lease()
            return self._reset_grid_locked()

    def _reset_grid_locked(self) -> bool:
        """Process the persisted reset before an ordinary trading cycle."""
        if self.database.get_state(ENGINE_STATUS_KEY) == ENGINE_IDLE:
            return False
        request_text = self.database.get_state("grid_reset")
        if request_text is None:
            self.pending_grid_bounds = None
            self.grid_needs_reset = False
            return False
        request = json.loads(request_text)
        if self.stop_controller.stop_requested.is_set():
            raise TradingHalt("Stop requested during grid reset.")
        if request["phase"] == "canceling":
            if (request.get("source") == "manual_recenter" and
                    "stop_loss_price" in request and
                    Decimal(request["stop_loss_price"]) < self.config.stop_loss_price):
                self.database.clear_state("grid_reset")
                self.pending_grid_bounds = None
                self.grid_needs_reset = False
                LOGGER.warning("Queued manual recenter withdrawn: trailing floor rose.")
                return False
            if request.get("source") == "breakout":
                if self._compare_grid_prices(
                        self._ticker_price(), Decimal(request["previous_upper"])) <= 0:
                    self.database.clear_state("grid_reset")
                    self.pending_grid_bounds = None
                    self.grid_needs_reset = False
                    LOGGER.info("Breakout faded before cancellation; grid reset withdrawn.")
                    return False
            if request.get("source") == "manual_recenter":
                center = Decimal(request["center_price"])
                candidate = replace(
                    self.config,
                    lower_price=Decimal(request["lower"]),
                    upper_price=Decimal(request["upper"]),
                    buy_grid_levels=request.get("buy_levels", self.config.buy_grid_levels),
                    sell_grid_levels=request.get("sell_levels", self.config.sell_grid_levels),
                )
                buys, sells = configured_grid_levels(center, candidate)
                ratio = max(center / buys[0], sells[0] / center)
                if not center / ratio < self._ticker_price() < center * ratio:
                    self.database.clear_state("grid_reset")
                    self.pending_grid_bounds = None
                    self.grid_needs_reset = False
                    LOGGER.info("Manual recenter withdrawn because market moved away.")
                    return False
            _, complete = self._cancel_bot_orders()
            if not complete or self.database.fetch_active_grids():
                raise TradingHalt("Old grid cancellation could not be verified.")
            carry_amount, carry_cost = self._carry_inventory()
            price = self._ticker_price()
            self.advance_trailing_stop(price)
            if price < self.config.stop_loss_price:
                self._liquidate_locked(price)
                return False
            if request.get("source") == "breakout":
                if self._compare_grid_prices(price, Decimal(request["previous_upper"])) <= 0:
                    if not self._within_grid_bounds(price, strict=True):
                        raise TradingHalt(
                            "Breakout faded outside old bounds after cancellation; "
                            "inspect orders before restart."
                        )
                    request["source"] = "breakout_faded"
                    request["lower"] = str(self.config.lower_price)
                    request["upper"] = str(self.config.upper_price)
                    LOGGER.warning("Breakout faded during cancellation; rebuilding old bounds.")
                else:
                    width = Decimal(request["width_percent"]) / 100
                    request["lower"] = str(price * (1 - width))
                    request["upper"] = str(price * (1 + width))
            anchor = price
            if request.get("source") == "manual_recenter":
                center = Decimal(request["center_price"])
                candidate = replace(
                    self.config,
                    lower_price=Decimal(request["lower"]),
                    upper_price=Decimal(request["upper"]),
                    buy_grid_levels=request.get("buy_levels", self.config.buy_grid_levels),
                    sell_grid_levels=request.get("sell_levels", self.config.sell_grid_levels),
                )
                buys, sells = configured_grid_levels(center, candidate)
                ratio = max(center / buys[0], sells[0] / center)
                if center / ratio < price < center * ratio:
                    anchor = center
                elif self._within_grid_bounds(price, strict=True):
                    request["source"] = "manual_stale"
                    request["lower"] = str(self.config.lower_price)
                    request["upper"] = str(self.config.upper_price)
                    request["investment_quote"] = str(self.config.investment_quote)
                    if self.config.buy_grid_levels is None:
                        request.pop("buy_levels", None)
                        request.pop("sell_levels", None)
                    else:
                        request["buy_levels"] = self.config.buy_grid_levels
                        request["sell_levels"] = self.config.sell_grid_levels
                    LOGGER.warning("Manual recenter drifted during cancellation; rebuilding old bounds.")
                else:
                    raise TradingHalt(
                        "Market left both grids during cancellation; inspect orders before restart."
                    )
            stop_loss = self.config.stop_loss_price
            if request.get("source") == "manual_recenter" and "stop_loss_price" in request:
                stop_loss = Decimal(request["stop_loss_price"])
                if stop_loss < self.config.stop_loss_price:
                    raise TradingHalt(RISK_OVERRIDE_DENIED)
            new_config = replace(self.config, lower_price=Decimal(request["lower"]),
                                 upper_price=Decimal(request["upper"]),
                                 spacing_percent=Decimal(request["spacing"]),
                                 investment_quote=Decimal(request.get(
                                     "investment_quote", str(self.config.investment_quote))),
                                 buy_grid_levels=request.get(
                                     "buy_levels", self.config.buy_grid_levels),
                                 sell_grid_levels=request.get(
                                     "sell_levels", self.config.sell_grid_levels),
                                 stop_loss_price=stop_loss)
            if new_config.stop_loss_price >= anchor:
                raise TradingHalt("New grid anchor is at or below the hard stop.")
            if not self._within_grid_bounds(price, new_config, strict=True):
                raise TradingHalt("Price left the requested bounds during reset.")
            try:
                lowers, uppers = self._validate_reset_grid(new_config, anchor, carry_amount)
            except GridSizingError as error:
                self._pause_for_order_sizing(error)
                self.database.clear_state("grid_reset")
                self.pending_grid_bounds = None
                self.grid_needs_reset = False
                return False
            baseline = self._free_balance(self.market["base"]) - carry_amount
            if baseline < 0:
                raise TradingHalt("Carried BTC exceeds the account balance.")
            state = {"anchor": str(anchor), "baseline_base": str(baseline),
                     "fingerprint": new_config.fingerprint()}
            active = {"lower": request["lower"], "upper": request["upper"],
                      "spacing": request["spacing"],
                      "investment_quote": str(new_config.investment_quote),
                      "stop_loss_price": str(new_config.stop_loss_price)}
            if new_config.buy_grid_levels is not None:
                active["buy_levels"] = new_config.buy_grid_levels
                active["sell_levels"] = new_config.sell_grid_levels
            trailing = None
            if request.get("source") == "manual_recenter":
                trailing = {
                    "high_water_mark": str(anchor),
                    "stop_loss_distance": str(anchor - new_config.stop_loss_price),
                }
            request["phase"] = "placing"
            carry_id = "carry-" + uuid.uuid4().hex if carry_amount > 0 else None
            release_sizing_pause = (request.get("source") == "manual_recenter" and
                                    self.database.get_state(SAFETY_MODE_KEY) == PAUSED_SIZING)
            with self.database.transaction():
                self.database.complete_grid_reset(
                    json.dumps(state, sort_keys=True), json.dumps(active),
                    json.dumps(request), carry_order_id=carry_id,
                    carry_price=str(carry_cost / carry_amount) if carry_id else None,
                    carry_amount=str(carry_amount) if carry_id else None,
                    carry_cost=str(carry_cost) if carry_id else None,
                    breakout_width_percent=(request["width_percent"]
                                            if request.get("source") == "manual_recenter"
                                            else None),
                    trailing_stop=(json.dumps(trailing, sort_keys=True)
                                   if trailing is not None else None),
                )
                if release_sizing_pause:
                    self.database.clear_state(SAFETY_MODE_KEY)
                    self.database.clear_state(SIZING_PAUSE_REASON_KEY)
                    self.database.clear_state(SIZING_PAUSE_NOTICE_KEY)
            if release_sizing_pause:
                self.is_paused = False
            with self._grid_lock:
                self.config = new_config
                self.anchor = anchor
                self.baseline_base = baseline
                self.levels, self.upper_levels = lowers, uppers
                if request.get("source") == "manual_recenter":
                    self.breakout_width_percent = Decimal(request["width_percent"])
                    self.high_water_mark = anchor
                    self.stop_loss_distance = anchor - new_config.stop_loss_price
        elif request["phase"] != "placing":
            raise TradingHalt("Unknown grid reset phase.")
        if request.get("source") == "manual_recenter" and self.database.get_state(SAFETY_MODE_KEY) == PAUSED_SIZING:
            with self.database.transaction():
                self.database.clear_state(SAFETY_MODE_KEY)
                self.database.clear_state(SIZING_PAUSE_REASON_KEY)
                self.database.clear_state(SIZING_PAUSE_NOTICE_KEY)
            self.is_paused = False
        for _ in range(3):
            self.run_cycle()
            if self.database.get_state(SAFETY_MODE_KEY) == PAUSED_SIZING:
                self.database.clear_state("grid_reset")
                self.pending_grid_bounds = None
                self.grid_needs_reset = False
                return False
            if self._post_only_rejected_in_cycle:
                return False
            latest = self.database.fetch_latest_orders_by_level()
            safe_buys = {
                level for level, raw in enumerate(self.levels, start=1)
                if self._price(raw) > self.config.stop_loss_price
            }
            expected = safe_buys | set(
                range(-1, -len(self.upper_levels) - 1, -1)) | {0}
            if expected <= latest.keys():
                notification_key = (
                    BREAKOUT_NOTICE_KEY if request.get("source") == "breakout"
                    else "grid_reset_notification_pending"
                )
                notification_value = "1" if notification_key == BREAKOUT_NOTICE_KEY else json.dumps({
                    "event": ("saved_pending" if request.get("started_from_idle")
                              else request.get("source", "grid_reset")),
                    "lower": request.get("lower"),
                    "upper": request.get("upper"),
                }, sort_keys=True)
                self.database.finish_grid_reset(notification_key, notification_value)
                self.pending_grid_bounds = None
                self.grid_needs_reset = False
                return True
        raise TradingHalt("Replacement grid did not finish placing orders.")

    def _save_liquidation(self, state: Dict[str, Any]) -> None:
        self.database.set_state(LIQUIDATION_KEY, json.dumps(state, sort_keys=True))

    def _liquidation_order(self, client_id: str) -> Optional[Dict[str, Any]]:
        try:
            order = self._call(self.exchange.fetch_order, "", self.config.symbol,
                               {"origClientOrderId": client_id})
        except ccxt.OrderNotFound:
            return None
        if not isinstance(order, dict) or not order.get("id"):
            raise TradingHalt("Hard-stop order lookup returned no exchange order ID.")
        return order

    def _submit_hard_stop_sell(self, state: Dict[str, Any]) -> bool:
        """Never resubmit an ambiguous market request with a reusable client ID."""
        if self.database.in_transaction:
            raise RuntimeError("Liquidation intents must commit before exchange submission.")
        client_id = state["client_order_id"]
        if state["attempts"]:
            found = self._liquidation_order(client_id)
            if found is not None:
                state["exchange_order_id"] = str(found["id"])
                state["phase"] = "confirming"
                self._save_liquidation(state)
                return False
            if not state.get("retry_allowed"):
                raise LiquidationPending(
                    "Market SELL outcome is unknown; reconciling the client order "
                    "before any further sale."
                )
            if self._available_base_for_liquidation() != Decimal(
                    state["pre_submit_base_free"]):
                raise LiquidationPending(
                    "BTC balance changed after a rejected MARKET SELL; reconciling."
                )
        remaining = Decimal(state["held_base"]) - Decimal(state.get("sold_base", "0"))
        amount, available = self._liquidation_sell_amount(state, remaining)
        if amount <= 0:
            self._complete_hard_stop(
                state, Decimal(state.get("sold_base", "0")),
                Decimal(state.get("proceeds_quote", "0")),
            )
            return True
        state["sell_amount"] = str(amount)
        state["pre_submit_base_free"] = str(available)
        state["attempts"] += 1
        state["retry_allowed"] = False  # Only a definite rejection permits resubmission.
        self._save_liquidation(state)  # Durable intent before the network request.
        previous_write_attempts = exchange_write_attempts(self.exchange)
        try:
            response = self._call(
                self.exchange.create_market_sell_order,
                self.config.symbol, float(Decimal(state["sell_amount"])),
                {"newClientOrderId": client_id},
            )
        except CircuitOpenError:
            state["retry_allowed"] = (
                exchange_write_attempts(self.exchange) == previous_write_attempts
            )
            self._save_liquidation(state)
            raise
        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
            state["retry_allowed"] = True
            self._save_liquidation(state)
            raise
        except (ccxt.InsufficientFunds, ccxt.InvalidOrder):
            # Binance rejected the request before execution. Recheck balance
            # and filters before attempting a newly sized MARKET order.
            state["attempts"] = 0
            self._save_liquidation(state)
            raise
        if not isinstance(response, dict) or not response.get("id"):
            raise ccxt.ExchangeNotAvailable(
                "Market SELL returned no exchange ID; verifying its client ID."
            )
        state["exchange_order_id"] = str(response["id"])
        state["phase"] = "confirming"
        self._save_liquidation(state)
        return False

    def _complete_hard_stop(self, state: Dict[str, Any], sold: Decimal,
                            proceeds: Decimal = Decimal(0)) -> None:
        residual = Decimal(state["held_base"]) - sold
        if residual < 0:
            raise TradingHalt("Hard-stop SELL exceeded bot-tracked BTC.")
        if residual != 0:
            raise LiquidationPending(f"Hard-stop still holds {residual} bot BTC.")
        if self.baseline_base is None:
            raise TradingHalt("Pre-grid BTC baseline is unavailable.")
        if self._available_base_for_liquidation() > self.baseline_base:
            raise LiquidationPending("Exchange BTC balance remains above the pre-grid baseline.")
        if any(self._is_bot_order(order) for order in self._live_open_orders()):
            raise LiquidationPending("Bot orders remain open after MARKET liquidation.")
        state.update(phase="complete", sold_base=str(sold),
                     residual_base="0", dust_base="0", proceeds_quote=str(proceeds),
                     completed_at=datetime.now(timezone.utc).isoformat())
        with self.database.transaction():
            self._save_liquidation(state)
            self.database.set_state(SAFETY_MODE_KEY, LIQUIDATED)
            self.database.set_state(ENGINE_STATUS_KEY, ENGINE_HALTED,
                                    trigger_event="LIQUIDATION_COMPLETE")
            self.database.set_state("halt_reason", "hard_stop_liquidated")
            self.database.set_state(LIQUIDATION_NOTICE_KEY, "1")
            self.database.clear_state(LIQUIDATION_ALARM_KEY)
            for key in ("grid_reset", BREAKOUT_TIMER_KEY, SAFETY_PAUSE_NOTICE_KEY,
                        SAFETY_RECOVERY_NOTICE_KEY, SAFETY_RESUME_NOTICE_KEY):
                self.database.clear_state(key)
        self.grid_needs_reset = False
        self.pending_grid_bounds = None
        self.is_paused = True
        LOGGER.critical(
            "Hard stop sold %s %s; verified bot inventory is flat. "
            "Engine is HALTED until admin reset.", sold, self.market["base"],
        )

    def _sellable_hard_stop_amount(self, held: Decimal, price: Decimal) -> Decimal:
        """Apply Binance MARKET lot and notional filters before a SELL."""
        if held < 0:
            raise TradingHalt("Bot BTC exposure cannot be negative.")
        if held == 0:
            return Decimal(0)

        def minimum(value: Any) -> Decimal:
            try:
                parsed = _order_decimal(value)
            except (InvalidOperation, TypeError, ValueError) as error:
                raise TradingHalt("Exchange limit minimum is invalid.") from error
            if not parsed.is_finite() or parsed < 0:
                raise TradingHalt("Exchange limit minimum is invalid.")
            return parsed

        try:
            amount = self._amount(held)
        except ccxt.InvalidOrder:
            # Binance rejects amounts below its precision step before we can
            # apply the market's explicit lot and notional filters.
            if held * price < Decimal("10"):
                return Decimal(0)
            raise
        if amount <= 0:
            return Decimal(0)
        limits = self.market.get("limits") or {}
        amount_min = minimum((limits.get("amount") or {}).get("min"))
        cost_min = minimum((limits.get("cost") or {}).get("min"))
        amount_max = None
        steps: List[Decimal] = []
        info = self.market.get("info") or {}
        filters = (info.get("filters") or []) if isinstance(info, dict) else []
        if any(isinstance(rule, dict) and rule.get("filterType") in
               ("MIN_NOTIONAL", "NOTIONAL") for rule in filters):
            cost_min = Decimal(0)  # The raw market flags govern these filters.
        for rule in filters:
            if not isinstance(rule, dict):
                continue
            kind = rule.get("filterType")
            if kind in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                amount_min = max(amount_min, minimum(rule.get("minQty")))
                maximum = minimum(rule.get("maxQty"))
                if maximum > 0:
                    amount_max = min(amount_max, maximum) if amount_max else maximum
                step = minimum(rule.get("stepSize"))
                if step > 0:
                    steps.append(step)
            elif kind == "MIN_NOTIONAL" and rule.get("applyToMarket", True):
                cost_min = max(cost_min, minimum(rule.get("minNotional")))
            elif kind == "NOTIONAL" and rule.get("applyMinToMarket", True):
                cost_min = max(cost_min, minimum(rule.get("minNotional")))
            if kind == "NOTIONAL" and rule.get("applyMaxToMarket", False):
                maximum_cost = minimum(rule.get("maxNotional"))
                if maximum_cost > 0:
                    maximum_amount = maximum_cost / price
                    amount_max = (min(amount_max, maximum_amount)
                                  if amount_max else maximum_amount)
        if amount_max is not None:
            amount = min(amount, amount_max)
        if steps:
            scale = Decimal(10) ** max(-step.as_tuple().exponent for step in steps)
            common_step = Decimal(math.lcm(*(int(step * scale) for step in steps))) / scale
            amount = self._amount((amount // common_step) * common_step)
        if amount < amount_min or price * amount < cost_min:
            return Decimal(0)
        return amount

    def _available_base_for_liquidation(self) -> Decimal:
        """Read an explicit, fresh Spot free balance; missing data is not zero."""
        balances = self._call(self.exchange.fetch_balance, {"type": "spot"})
        asset = balances.get(self.market["base"]) if isinstance(balances, dict) else None
        free = asset.get("free") if isinstance(asset, dict) else None
        try:
            available = Decimal(str(free))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise TradingHalt("Exchange BTC free balance is unavailable.") from error
        if not available.is_finite() or available < 0:
            raise TradingHalt("Exchange BTC free balance is invalid.")
        return available

    def _liquidation_sell_amount(
        self, state: Dict[str, Any], tracked: Decimal,
    ) -> Tuple[Decimal, Decimal]:
        """Cap each sale at actual free BTC; never hide a material shortfall as dust."""
        if tracked < 0:
            raise TradingHalt("Liquidation ledger has negative remaining BTC.")
        available = self._available_base_for_liquidation()
        try:
            price = self._ticker_price()
        except ccxt.NetworkError:
            # Public market data must not gate a MARKET risk exit.
            price = Decimal(state.get("trigger_price", str(self.config.stop_loss_price)))
            LOGGER.warning("Using the hard-stop trigger price for MARKET order sizing.")
        safe_amount = min(tracked, available)
        amount = self._sellable_hard_stop_amount(safe_amount, price)
        if amount <= 0 and tracked > 0:
            if self._sellable_hard_stop_amount(tracked, price) > 0:
                state["unavailable_base"] = str(tracked - safe_amount)
                self._save_liquidation(state)
                raise LiquidationPending(
                    "Bot inventory exceeds sellable free BTC; liquidation needs review."
                )
            state["residual_base"] = str(tracked)
            self._save_liquidation(state)
            raise LiquidationPending(
                "Bot BTC is below Binance MARKET order filters; liquidation is unresolved."
            )
        return amount, available

    def _liquidation_step(self, state: Dict[str, Any]) -> bool:
        """Advance one durable phase; never submit a second sell before reconciliation."""
        if state["phase"] == "canceling":
            _, complete = self._cancel_bot_orders()
            if not complete or self.database.fetch_active_grids():
                return False
            held, cost = self._carry_inventory(require_available=False)
            state.update(held_base=str(held), cost_basis_quote=str(cost),
                         sold_base="0", proceeds_quote="0")
            amount, available = self._liquidation_sell_amount(state, held)
            state["sell_amount"] = str(amount)
            if amount <= 0:
                self._complete_hard_stop(state, Decimal(0))
                return True
            state.update(phase="submitting", client_order_id=self.order_client_prefix + uuid.uuid4().hex[:20],
                         pre_submit_base_free=str(available),
                         attempts=0, retry_allowed=False)
            self._save_liquidation(state)
        if state["phase"] == "submitting":
            if self._submit_hard_stop_sell(state):
                return True
        if state["phase"] == "confirming":
            if state.get("confirmed_order_id") == state["client_order_id"]:
                total_sold = Decimal(state["confirmed_sold_base"])
                total_proceeds = Decimal(state["confirmed_proceeds_quote"])
            else:
                order = self._liquidation_order(state["client_order_id"])
                if order is None:
                    raise LiquidationPending("MARKET SELL is not yet visible on Binance.")
                if order.get("status") == "open":
                    if str(order.get("type", "")).lower() == "limit":
                        # Upgrade a GTC liquidation persisted by the old code.
                        try:
                            self._call(self.exchange.cancel_order, str(order["id"]),
                                       self.config.symbol)
                        except ccxt.OrderNotFound:
                            pass
                        order = self._liquidation_order(state["client_order_id"])
                    if order is None or order.get("status") == "open":
                        raise LiquidationPending(
                            "Hard-stop SELL has not reached a terminal exchange status."
                        )
                if order.get("status") not in ("closed", "canceled", "expired"):
                    raise TradingHalt("Hard-stop SELL has an unknown exchange status.")
                if order.get("filled") is None:
                    raise TradingHalt("Hard-stop SELL has no confirmed filled quantity.")
                sold = _order_decimal(order.get("filled"))
                total_sold = Decimal(state.get("sold_base", "0")) + sold
                if total_sold > Decimal(state["held_base"]):
                    raise TradingHalt("Hard-stop SELL exceeded bot-tracked BTC.")
                proceeds = _order_decimal(order.get("cost"),
                                          str(sold * _order_decimal(order.get("average"))))
                if sold > 0 and proceeds > 0:
                    basis = (Decimal(state["cost_basis_quote"]) * sold /
                             Decimal(state["held_base"]))
                    sell_fee, sell_fee_actual = _pnl_fee_in_quote(
                        order, self.market["base"], self.market["quote"],
                        proceeds, _fees_in_asset(order, self.market["base"]),
                        _fees_in_asset(order, self.market["quote"]),
                    )
                    profit = proceeds - sell_fee - basis
                total_proceeds = Decimal(state.get("proceeds_quote", "0")) + proceeds
                state.update(confirmed_order_id=state["client_order_id"],
                             confirmed_sold_base=str(total_sold),
                             confirmed_proceeds_quote=str(total_proceeds))
                with self.database.transaction():
                    if sold > 0 and proceeds > 0:
                        self.database.record_trade(
                            basis / sold, proceeds / sold, profit,
                            sell_order_id=state["client_order_id"],
                            sold_base=sold,
                            fee_basis="mixed" if sell_fee_actual else "estimated",
                        )
                    self._save_liquidation(state)
                if sold > 0 and proceeds > 0:
                    self._alert_realized_sell(
                        state["client_order_id"], proceeds / sold, sold, profit,
                        filled=order.get("status") == "closed",
                    )
            residual = Decimal(state["held_base"]) - total_sold
            amount, available = self._liquidation_sell_amount(state, residual)
            if amount <= 0:
                self._complete_hard_stop(state, total_sold, total_proceeds)
                return True
            state.update(phase="submitting", sold_base=str(total_sold),
                         proceeds_quote=str(total_proceeds), sell_amount=str(amount),
                         client_order_id=self.order_client_prefix + uuid.uuid4().hex[:20],
                         pre_submit_base_free=str(available),
                         attempts=0, retry_allowed=False)
            for key in ("confirmed_order_id", "confirmed_sold_base",
                        "confirmed_proceeds_quote"):
                state.pop(key, None)
            self._save_liquidation(state)
            return False
        raise TradingHalt("Unknown hard-stop liquidation phase.")

    def _liquidate_locked(self, price: Decimal, *, trigger_event: str = "HARD_STOP") -> None:
        """Cancel, verify, sell only bot inventory, and persist a terminal state."""
        state_text = self.database.get_state(LIQUIDATION_KEY)
        state = json.loads(state_text) if state_text else {
            "phase": "canceling", "trigger_price": str(price), "trigger_event": trigger_event,
        }
        if state_text is None:
            with self.database.transaction():
                self._save_liquidation(state)
                self.database.set_state(SAFETY_MODE_KEY, LIQUIDATING,
                                        trigger_event=trigger_event if trigger_event != "HARD_STOP" else None)
                if trigger_event == "MANUAL_PANIC_LIQUIDATION":
                    # The API worker owns the exit. Stop ordinary placement even
                    # when panic was requested from IDLE or a safety pause.
                    self.database.set_state(ENGINE_STATUS_KEY, ENGINE_HALTED,
                                            trigger_event=trigger_event)
            headline = ("MANUAL PANIC — MARKET LIQUIDATION" if trigger_event == "MANUAL_PANIC_LIQUIDATION"
                        else "HARD STOP TRIGGERED — MARKET LIQUIDATION")
            schedule_telegram_alert(
                f"🚨 *{headline}*"
                f"\nPair: {markdown_text(self.config.symbol)}"
                + ("\nRequested by admin" if trigger_event == "MANUAL_PANIC_LIQUIDATION"
                   else f"\nTrigger price: {markdown_text(price)}")
            )
            self.is_paused = True
            label = "Manual panic requested" if trigger_event == "MANUAL_PANIC_LIQUIDATION" else "Hard stop triggered"
            LOGGER.critical("%s at %s %s; liquidating bot inventory.",
                            label, price, self.config.symbol)
        if state["phase"] == "complete":
            self._complete_hard_stop(state, Decimal(state["sold_base"]),
                                     Decimal(state.get("proceeds_quote", "0")))
            return
        if state["phase"] == "failed":
            with self.database.transaction():
                self.database.set_state(SAFETY_MODE_KEY, LIQUIDATION_HALTED)
                self.database.set_state(ENGINE_STATUS_KEY, ENGINE_HALTED)
                self.database.set_state("halt_reason", "hard_stop_failed")
                self.database.set_state(LIQUIDATION_NOTICE_KEY, "1")
            self.is_paused = True
            return
        permanent_errors = (ccxt.AuthenticationError, ccxt.PermissionDenied,
                            ccxt.BadRequest)
        retry_delay = 1
        while True:
            try:
                if self._liquidation_step(state):
                    return
                if state["phase"] == "submitting":
                    retry_delay = 1
                    continue  # A terminal partial fill needs the next MARKET sale now.
            except permanent_errors as error:
                self._fail_liquidation(state, error)
                return
            except (ccxt.NetworkError, ccxt.ExchangeError) as error:
                if isinstance(error, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
                    retry_delay = exchange_retry_delay(self.exchange, error, max(retry_delay, 60))
                _journal_fault(self.database, error, "LIQUIDATION_RETRY")
                LOGGER.critical("Hard-stop liquidation exchange failure (%s); retrying in %ss.",
                                type(error).__name__, retry_delay)
                self.database.set_state(LIQUIDATION_ALARM_KEY,
                                        f"Liquidation exchange failure: {type(error).__name__}: {error}")
            except LiquidationPending as error:
                self.database.set_state(LIQUIDATION_ALARM_KEY, str(error))
                LOGGER.error("Hard-stop liquidation remains unresolved: %s", error)
            except (TradingHalt, ValueError, KeyError, TypeError) as error:
                self._fail_liquidation(state, error)
                return
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)

    def _fail_liquidation(self, state: Dict[str, Any], error: Exception) -> None:
        if (state.get("client_order_id") and
                state.get("confirmed_order_id") == state["client_order_id"]):
            state["sold_base"] = state["confirmed_sold_base"]
            state["proceeds_quote"] = state["confirmed_proceeds_quote"]
        if "held_base" in state and "sold_base" in state:
            state["residual_base"] = str(
                Decimal(state["held_base"]) - Decimal(state["sold_base"])
            )
        state.update(phase="failed", failure_type=type(error).__name__,
                     failure_reason=str(error),
                     failed_at=datetime.now(timezone.utc).isoformat())
        with self.database.transaction():
            self._save_liquidation(state)
            self.database.set_state(SAFETY_MODE_KEY, LIQUIDATION_HALTED,
                                    trigger_event="LIQUIDATION_FAILED",
                                    error_details=_exception_details(error))
            self.database.set_state(ENGINE_STATUS_KEY, ENGINE_HALTED,
                                    trigger_event="LIQUIDATION_FAILED",
                                    error_details=_exception_details(error))
            self.database.set_state("halt_reason", "hard_stop_failed")
            self.database.set_state(LIQUIDATION_NOTICE_KEY, "1")
            self.database.set_state(LIQUIDATION_ALARM_KEY, str(error))
        self.is_paused = True
        LOGGER.critical("Hard-stop liquidation halted: %s", type(error).__name__,
                        exc_info=True)

    def liquidate(self, price: Decimal) -> None:
        with self._cycle_lock:
            self.assert_runner_lease()
            self._liquidate_locked(price)

    def force_liquidate(self) -> Dict[str, Any]:
        """An explicit admin exit; never change the stop or sell baseline BTC."""
        with self._cycle_lock:
            self.assert_runner_lease()
            state_text = self.database.get_state(LIQUIDATION_KEY)
            state = json.loads(state_text) if state_text else None
            if state and state["phase"] == "failed":
                raise TradingHalt("Liquidation is unresolved. Review the saved alarm before another exit.")
            if not state or state["phase"] != "complete":
                saved_run = self.database.get_state("grid_run")
                if not saved_run:
                    raise TradingHalt("No saved bot inventory exists to liquidate.")
                try:
                    baseline = Decimal(json.loads(saved_run)["baseline_base"])
                    if not baseline.is_finite() or baseline < 0:
                        raise ValueError("Invalid baseline")
                except (KeyError, TypeError, ValueError, InvalidOperation) as error:
                    raise TradingHalt("Saved BTC baseline is unavailable; cannot safely liquidate.") from error
                self.baseline_base = baseline
                self._load_market_for_engine_control()
                # A public ticker failure must not gate the emergency exit.
                # The existing protocol fetches fresh prices for exchange sizing.
                price = self.config.stop_loss_price
                try:
                    observed = Decimal(self.database.get_state(LAST_MARKET_PRICE_KEY) or str(price))
                    if observed.is_finite() and observed > 0:
                        price = observed
                except (TypeError, ValueError, InvalidOperation):
                    pass
                self._liquidate_locked(price, trigger_event="MANUAL_PANIC_LIQUIDATION")
                state = json.loads(self.database.get_state(LIQUIDATION_KEY))
            return {
                "engine_status": self.database.get_state(ENGINE_STATUS_KEY),
                "mode": self.database.get_state(SAFETY_MODE_KEY),
                "phase": state["phase"], "completed": state["phase"] == "complete",
                "sold_base": state.get("sold_base", "0"),
                "residual_base": state.get("residual_base"),
            }

    def set_manual_pause(self, active: bool) -> Optional[str]:
        """Persist a manual pause and use the existing targeted BUY cancellation."""
        with self._cycle_lock:
            self.assert_runner_lease()
            if self.stop_controller.stop_requested.is_set() or self.grid_needs_reset:
                raise TradingHalt("Bot is stopping or resetting its grid.")
            mode = self.database.get_state(SAFETY_MODE_KEY)
            if mode in (LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED):
                raise TradingHalt("Hard-stop liquidation has locked trading controls.")
            if mode == PAUSED_RESET:
                raise TradingHalt("Orders are cleared for reset; start a new run after reset.")
            if mode == PAUSED_SIZING:
                raise TradingHalt("Order sizing pause requires a valid grid re-anchor.")
            if active:
                with self.database.transaction():
                    if mode != PAUSED_MANUAL:
                        self.database.set_state(SAFETY_MODE_KEY, PAUSED_MANUAL)
                        self.database.clear_state(SAFETY_RECOVERY_NOTICE_KEY)
                        self.database.clear_state(SAFETY_RESUME_NOTICE_KEY)
                        if mode is None:
                            self.database.set_state(SAFETY_PAUSE_NOTICE_KEY, "1")
                    self.database.clear_state(BREAKOUT_TIMER_KEY)
                self.is_paused = True
                _, complete = self._cancel_buy_orders_for_pause()
                if not complete:
                    raise TradingHalt("Some BUY orders remain open; cancellation will retry.")
                return PAUSED_MANUAL

            if mode != PAUSED_MANUAL:
                return mode
            price = self._ticker_price()
            if self._compare_grid_prices(price, self.config.lower_price) <= 0:
                # The automatic downside guard still owns the pause.
                self.database.set_state(SAFETY_MODE_KEY, PAUSED_DOWNSIDE)
                self.is_paused = True
                return PAUSED_DOWNSIDE
            with self.database.transaction():
                self.database.clear_state(SAFETY_MODE_KEY)
                self.database.clear_state(SAFETY_PAUSE_NOTICE_KEY)
            self.is_paused = False
            return None

    def prepare_factory_reset(self) -> Dict[str, Any]:
        """Persist a no-placement state before canceling both bot order sides."""
        with self._cycle_lock:
            self.assert_runner_lease()
            if self.stop_controller.stop_requested.is_set() or self.grid_needs_reset:
                raise TradingHalt("Bot is stopping or resetting its grid.")
            mode = self.database.get_state(SAFETY_MODE_KEY)
            if mode in (LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED):
                raise TradingHalt("Hard-stop liquidation has locked trading controls.")
            with self.database.transaction():
                self.database.set_state(SAFETY_MODE_KEY, PAUSED_RESET)
                self.database.clear_state(BREAKOUT_TIMER_KEY)
                self.database.clear_state(SAFETY_RECOVERY_NOTICE_KEY)
                self.database.clear_state(SAFETY_RESUME_NOTICE_KEY)
            self.is_paused = True
            _, complete = self._cancel_bot_orders()
            if not complete or self.database.fetch_active_grids():
                raise TradingHalt("Bot orders still need cancellation or reconciliation; retry.")
            remaining = len(self._live_open_orders())
            held = _portfolio_wallet(self.database)["btc_held"]
            LOGGER.warning("Admin cleared bot BUY and SELL orders for factory reset.")
            return {
                "safety_pause": "Active", "mode": PAUSED_RESET,
                "remaining_exchange_orders": remaining,
                "bot_btc_held": held,
                "factory_reset_ready": remaining == 0 and held == 0,
            }

    def _load_market_for_engine_control(self) -> None:
        if self.market:
            return
        self._call(self.exchange.load_markets)
        self.market = self.exchange.market(self.config.symbol)
        if not self.market.get("spot") or self.market.get("active") is False:
            raise TradingHalt("Configured symbol is not an active Spot market.")

    def reconcile_idle_orders(self) -> None:
        """Retry cleanup until the bot-owned Binance book and ledger are flat."""
        with self._cycle_lock:
            self.assert_runner_lease()
            if (self.database.get_state(ENGINE_STATUS_KEY) != ENGINE_IDLE or
                    self.database.get_state(ENGINE_STOP_CLEANUP_KEY) != "1"):
                return
            if self.database.get_state(SAFETY_MODE_KEY) in (
                    LIQUIDATING, LIQUIDATION_HALTED):
                return  # The separate liquidation state must be reviewed.
            self._load_market_for_engine_control()
            _, complete = self._cancel_bot_orders()
            known_orders = self.database.fetch_all_orders()
            exchange_orders = self._full_account_open_orders()
            self._audit_open_order_drift(
                exchange_orders, self.database.fetch_active_grids()
            )
            if (not complete or self.database.fetch_active_grids() or
                    any(self._is_bot_order(order, known_orders)
                        for order in exchange_orders)):
                raise TradingHalt("Idle bot orders still need reconciliation.")
            self.database.complete_order_cleanup()

    def halt_after_fault(self, reason: Any) -> bool:
        """Stop placement, persist the alarm, and verify exchange cleanup."""
        with self._cycle_lock:
            _journal_fault(self.database, reason)
            self.assert_runner_lease()
            result = self.stop_controller.request_stop()
            if result.unresolved:
                LOGGER.error("Fault stop left %s tracked orders unresolved.",
                             result.unresolved)
            try:
                self.reconcile_idle_orders()
            except Exception as error:
                _journal_fault(self.database, error, "FAULT_CLEANUP_FAILED")
                LOGGER.exception("Fault stop could not verify Binance order cleanup.")
                return False
            return True

    def stop_engine(self) -> Dict[str, Any]:
        """Persist IDLE before canceling and reconciling every bot-owned grid order."""
        with self._cycle_lock:
            self.assert_runner_lease()
            mode = self.database.get_state(SAFETY_MODE_KEY)
            if mode in (
                    LIQUIDATING, LIQUIDATION_HALTED):
                raise TradingHalt("Unresolved hard-stop liquidation cannot be interrupted.")
            with self.database.transaction():
                self.database.require_order_cleanup(
                    "Engine STOP requested; Binance order cleanup is unverified.", trigger_event="STOP")
                if self.grid_needs_reset:
                    pending_text = self.database.get_state("grid_reset")
                    if pending_text:
                        pending = json.loads(pending_text)
                        pending["phase"] = "canceling"
                        self.database.set_state("grid_reset", json.dumps(pending, sort_keys=True))
            self.reconcile_idle_orders()
            with self.database.transaction():
                if mode == LIQUIDATED:
                    self.database.set_state(ENGINE_STATUS_KEY, ENGINE_HALTED)
                self.database.clear_state(RECONCILE_COVER_KEY)
            LOGGER.warning("Admin stopped the trading engine and cleared bot grid orders.")
            return {"engine_status": ENGINE_HALTED if mode == LIQUIDATED else ENGINE_IDLE,
                    "bot_orders_cleared": True,
                    "remaining_exchange_orders": len(self._live_open_orders())}

    def start_engine(self, *, auto_cover_inventory: bool = False,
                     pending_grid_token: Optional[str] = None) -> Dict[str, Any]:
        """Reconcile first; resume matched orders or queue a guarded rebuild."""
        with self._cycle_lock:
            self.assert_runner_lease()
            if self.database.get_state(ENGINE_STOP_CLEANUP_KEY) == "1":
                raise TradingHalt("A prior stop still needs Binance order cleanup.")
            state = self._reconcile_state()
            if self.stop_controller.stop_requested.is_set():
                raise TradingHalt("The trading process is stopping or faulted.")
            if self.database.get_state(ENGINE_STATUS_KEY) == ENGINE_RUNNING:
                if state["unhedged_btc"]:
                    self.stop_engine()
                    raise TradingHalt("Free BTC lacks SELL protection; engine is idle.")
                return {"engine_status": ENGINE_RUNNING}
            if not self.database.get_state("grid_run"):
                raise TradingHalt("Configure a grid before starting the engine.")
            mode = self.database.get_state(SAFETY_MODE_KEY)
            if mode == LIQUIDATED:
                self._assert_liquidation_reset_ready()
                raise TradingHalt(
                    "Hard-stop liquidation is complete. Save a new grid in Settings, "
                    "then review and START the saved grid."
                )
            if self.database.get_state("halt_reason"):
                raise TradingHalt("A saved trading fault requires review before starting.")
            if mode in (PAUSED_DOWNSIDE, PAUSED_MANUAL, PAUSED_RESET,
                        PAUSED_SIZING, LIQUIDATING, LIQUIDATED,
                        LIQUIDATION_HALTED):
                raise TradingHalt(f"Cannot start while safety mode is {mode}.")
            pending_text = self.database.get_state("grid_reset")
            if self.grid_needs_reset and not pending_text:
                raise TradingHalt("A pending grid is missing from SQLite; manual review required.")
            if pending_grid_token and not pending_text:
                raise TradingHalt("The pending grid changed; review it again before START.")
            if pending_text:
                expected = hashlib.sha256(pending_text.encode("utf-8")).hexdigest()
                if not pending_grid_token or not secrets.compare_digest(
                        pending_grid_token, expected):
                    raise TradingHalt(
                        "Review the saved pending grid before START. Refresh the dashboard "
                        "and confirm its exact settings."
                    )
            unhedged = state["unhedged_btc"]
            if unhedged:
                if not auto_cover_inventory:
                    raise TradingHalt(
                        f"Unhedged {unhedged} BTC: clear it manually or restart "
                        "with auto_cover_inventory=true to place SELL protection first."
                    )
                carried, _ = self._carry_inventory(require_available=False)
                if carried < unhedged * (1 - SELL_AMOUNT_BUFFER):
                    raise TradingHalt(
                        "BTC has no complete bot cost-basis history; clear it manually."
                    )
            current, _, _ = self.prepare(persist=False)
            if not self._within_grid_bounds(current, strict=True):
                raise TradingHalt("Market price is outside the saved grid; re-anchor first.")
            if current <= self.config.stop_loss_price:
                raise TradingHalt("Market price is at or below the hard stop.")
            request = None
            if not state["resumable"] and not self.grid_needs_reset:
                carry_amount, _ = self._carry_inventory()
                self._validate_reset_grid(self.config, current, carry_amount)
                request = {
                    "phase": "canceling", "source": "engine_resume",
                    "lower": str(self.config.lower_price),
                    "upper": str(self.config.upper_price),
                    "spacing": str(self.config.spacing_percent),
                    "investment_quote": str(self.config.investment_quote),
                }
                if self.config.buy_grid_levels is not None:
                    request["buy_levels"] = self.config.buy_grid_levels
                    request["sell_levels"] = self.config.sell_grid_levels
            with self.database.transaction():
                # Recheck the reviewed request while holding SQLite's write lock.
                if self.database.get_state("grid_reset") != pending_text:
                    raise TradingHalt("The pending grid changed; review it again before START.")
                if self.database.get_state(ENGINE_STOP_CLEANUP_KEY) == "1":
                    raise TradingHalt("A prior stop still needs Binance order cleanup.")
                if self.database.get_state(SAFETY_MODE_KEY) != mode:
                    raise TradingHalt("Safety mode changed during START; review it again.")
                if request is not None:
                    self.database.set_state("grid_reset", json.dumps(request, sort_keys=True))
                if pending_text:
                    pending = json.loads(pending_text)
                    pending["started_from_idle"] = True
                    self.database.set_state("grid_reset", json.dumps(pending, sort_keys=True))
                if unhedged:
                    needed = state["covered_btc"] + unhedged * (1 - SELL_AMOUNT_BUFFER)
                    self.database.set_state(RECONCILE_COVER_KEY, str(needed))
                self.database.clear_state(ENGINE_FAULT_KEY)
                self.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING, trigger_event="START")
            if request is not None:
                self.pending_grid_bounds = (self.config.lower_price,
                                            self.config.upper_price)
                self.grid_needs_reset = True
            LOGGER.warning("Admin started the trading engine with a saved grid.")
            return {"engine_status": ENGINE_RUNNING}

    def _cancel_buy_orders_for_pause(
        self,
    ) -> Tuple[List[Tuple[str, Tuple[str, ...]]], bool]:
        """Cancel only bot-owned BUY orders while leaving every SELL active."""
        return self._cancel_bot_orders(
            side="buy", marker_prefix="safety_pause_buy", limit_only=True
        )

    def _missing_paused_buys(self) -> bool:
        return any(
            row["side"] == "BUY" and row["status"] == "CANCELED" and
            self.database.get_state(f"safety_pause_buy:{row['order_id']}") == "1"
            for row in self.database.fetch_latest_orders_by_level().values()
        )

    def _cancel_buys_below_stop(self) -> None:
        """Remove tracked BUY limits that a raised hard stop has overtaken."""
        affected = [
            row for row in self.database.fetch_active_grids()
            if row["side"] == "BUY" and row["order_type"] == "LIMIT"
            and Decimal(row["price"]) <= self.config.stop_loss_price
        ]
        if not affected:
            return
        ids = {row["order_id"] for row in affected}
        with self.database.transaction():
            for row in affected:
                self.database.set_state(f"trailing_stop_buy:{row['order_id']}", "1")
        _, complete = self._cancel_bot_orders(
            side="buy", selected_ids=ids, marker_prefix="trailing_stop_buy"
        )
        if not complete or any(row["order_id"] in ids
                               for row in self.database.fetch_active_grids()):
            raise TradingHalt("BUY orders below the trailing stop need reconciliation.")

    def run_cycle(self) -> List[Tuple[str, Tuple[str, ...]]]:
        with self._cycle_lock:
            self.assert_runner_lease()
            if self.database.get_state(ENGINE_STATUS_KEY) == ENGINE_IDLE:
                return []
            try:
                self._audit_full_book_if_due()
                return self._run_cycle_locked()
            except GridSizingError as error:
                self._pause_for_order_sizing(error)
                return []

    def _pause_for_order_sizing(self, error: GridSizingError) -> None:
        """Keep the risk monitor alive while preventing an undersized grid."""
        with self.database.transaction():
            self.database.set_state(SAFETY_MODE_KEY, PAUSED_SIZING)
            self.database.set_state(SIZING_PAUSE_REASON_KEY, str(error))
            self.database.set_state(SIZING_PAUSE_NOTICE_KEY, "1")
            self.database.clear_state(BREAKOUT_TIMER_KEY)
        self.is_paused = True
        LOGGER.critical("Grid order sizing pause: %s", error)
        _, complete = self._cancel_buy_orders_for_pause()
        if not complete:
            LOGGER.error("Sizing pause BUY cancellations will retry next cycle.")

    def _run_cycle_locked(self) -> List[Tuple[str, Tuple[str, ...]]]:
        if not self.levels:
            raise RuntimeError("Call prepare() before run_cycle().")
        self._post_only_rejected_in_cycle = False
        mode = self.database.get_state(SAFETY_MODE_KEY)
        if mode == LIQUIDATING:
            self._liquidate_locked(self.config.stop_loss_price)
            return []
        current_price = self._ticker_price()
        self.database.set_state(LAST_MARKET_PRICE_KEY, str(current_price))
        if mode in (LIQUIDATED, LIQUIDATION_HALTED):
            return []
        if mode != LIQUIDATING:
            self.advance_trailing_stop(current_price)
        if mode == LIQUIDATING or current_price < self.config.stop_loss_price:
            self._liquidate_locked(current_price)
            return []
        if mode == PAUSED_RESET:
            events, complete = self._cancel_bot_orders()
            if not complete or self.database.fetch_active_grids():
                LOGGER.warning("Bot order cleanup for factory reset will retry next cycle.")
            return events
        self._cancel_buys_below_stop()
        if mode != PAUSED_SIZING and self._observe_upper_breakout(current_price):
            return []
        events: List[Tuple[str, Tuple[str, ...]]] = []
        if self.is_paused:
            pause_events, cancellations_complete = self._cancel_buy_orders_for_pause()
            events.extend(pause_events)
            if (cancellations_complete and
                    self._compare_grid_prices(current_price, self.config.lower_price) > 0
                    and self.database.get_state(SAFETY_MODE_KEY) == PAUSED_DOWNSIDE):
                with self.database.transaction():
                    self.database.clear_state(SAFETY_MODE_KEY)
                    self.database.clear_state(SAFETY_PAUSE_NOTICE_KEY)
                    self.database.set_state(SAFETY_RECOVERY_NOTICE_KEY, "1")
                    self.database.set_state(SAFETY_RESUME_NOTICE_KEY, "1")
                self.is_paused = False
                LOGGER.info("Safety pause lifted after recovery to %s %s.",
                            current_price, self.config.symbol)
        for row in self.database.fetch_active_grids():
            if self.stop_controller.stop_requested.is_set():
                return events
            event = self._reconcile_order(row)
            if event:
                events.append(event)

        if self.database.get_state(SAFETY_MODE_KEY) == PAUSED_SIZING:
            return events

        latest = self.database.fetch_latest_orders_by_level()
        seed_row = latest.get(0)
        cover_text = self.database.get_state(RECONCILE_COVER_KEY)
        if cover_text:
            required = Decimal(cover_text)
            covered = sum((
                _order_decimal(order.get("remaining"), str(order.get("amount") or "0"))
                for order in self._live_open_orders()
                if str(order.get("side", "")).lower() == "sell" and
                self._is_bot_order(order)
            ), Decimal(0))
            free_base = self._free_balance(self.market["base"])
            if (covered >= required or
                    self._sellable_hard_stop_amount(free_base, current_price) == 0):
                self.database.clear_state(RECONCILE_COVER_KEY)
                cover_text = None
        if seed_row is None:
            if cover_text:
                raise TradingHalt("Inventory cover has no saved seed lot; manual review required.")
            if not self.is_paused:
                self._check_grid_buy_capital(
                    self.config, self.levels, self.config.stop_loss_price,
                    seed_required=True,
                )
                self._place_seed_buy(current_price)
            return events
        if seed_row["status"] not in ("FILLED",):
            seed_row = self.database.get_order(seed_row["order_id"])
        if seed_row["status"] != "FILLED":
            return events

        latest = self.database.fetch_latest_orders_by_level()
        in_bounds = (
            not self.is_paused and not cover_text and
            self._within_grid_bounds(current_price)
        )
        if in_bounds and all(
            level not in latest for level in range(1, len(self.levels) + 1)
        ):
            # The seed may have filled in an earlier cycle. Recheck the wallet
            # before the first LIMIT order, since other account activity can
            # consume quote between the seed and this batch.
            self._check_grid_buy_capital(
                self.config, self.levels, self.config.stop_loss_price,
                seed_required=False,
            )
        if all(level not in latest for level in
               range(-1, -len(self.upper_levels) - 1, -1)):
            sell_amount = self._seed_upper_sell_amount(seed_row)
            if sell_amount is None:
                return events
            required_base = sell_amount * len(self.upper_levels)
            for raw in self.upper_levels:
                self._check_order_size(self._price(raw), sell_amount)
            if self._free_bot_base() < required_base:
                LOGGER.warning(
                    "Insufficient free bot BTC for initial SELL limits; "
                    "waiting for balance reconciliation."
                )
                return events
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
            elif (row["status"] == "CANCELED" and row["side"] == "BUY" and
                  (self.database.get_state(
                      f"safety_pause_buy:{row['order_id']}") == "1" or
                   self.database.get_state(
                      f"trailing_stop_buy:{row['order_id']}") == "1")):
                canceled_buy = self._fetch_order(row)
                if _order_decimal(canceled_buy.get("filled")) > 0:
                    self._place_sell(row)
                elif in_bounds and Decimal(row["price"]) < current_price:
                    self._place_buy(level, row.get("parent_order_id"))
            else:
                raise TradingHalt("A grid lane ended unexpectedly; inspect local orders.")
        return events

    async def _notify_safety_state(self, notifier: TelegramNotifier) -> None:
        if self.database.get_state(SIZING_PAUSE_NOTICE_KEY):
            try:
                await notifier.notify_sizing_pause(
                    self.database.get_state(SIZING_PAUSE_REASON_KEY) or "Order below size floor."
                )
            except Exception as error:
                LOGGER.warning("Grid sizing Telegram update failed: %s",
                               type(error).__name__)
            else:
                self.database.clear_state(SIZING_PAUSE_NOTICE_KEY)
        if self.database.get_state(SAFETY_PAUSE_NOTICE_KEY):
            try:
                await notifier.notify_safety_pause()
            except Exception as error:
                LOGGER.warning("Safety pause Telegram update failed: %s",
                               type(error).__name__)
            else:
                self.database.clear_state(SAFETY_PAUSE_NOTICE_KEY)
        missing_buys = self._missing_paused_buys() if not self.is_paused else False
        if (not self.is_paused and missing_buys and
                self.database.get_state(SAFETY_RECOVERY_NOTICE_KEY)):
            try:
                await notifier.notify_safety_recovery()
            except Exception as error:
                LOGGER.warning("Safety recovery Telegram update failed: %s",
                               type(error).__name__)
            else:
                self.database.clear_state(SAFETY_RECOVERY_NOTICE_KEY)
        if (not self.is_paused and
                self.database.get_state(SAFETY_RESUME_NOTICE_KEY) and
                not missing_buys):
            try:
                await notifier.notify_safety_resume()
            except Exception as error:
                LOGGER.warning("Safety resume Telegram update failed: %s",
                               type(error).__name__)
            else:
                with self.database.transaction():
                    self.database.clear_state(SAFETY_RECOVERY_NOTICE_KEY)
                    self.database.clear_state(SAFETY_RESUME_NOTICE_KEY)

    async def _notify_liquidation(self, notifier: TelegramNotifier) -> None:
        if not self.database.get_state(LIQUIDATION_NOTICE_KEY):
            return
        mode = self.database.get_state(SAFETY_MODE_KEY)
        try:
            await notifier.notify_hard_stop(mode == LIQUIDATED)
        except Exception as error:
            LOGGER.warning("Hard-stop Telegram alert failed: %s", type(error).__name__)
        else:
            self.database.clear_state(LIQUIDATION_NOTICE_KEY)

    async def run(self, notifier: TelegramNotifier) -> None:
        self.assert_runner_lease()
        if self.ready_mode and self.database.get_state(ENGINE_STATUS_KEY) != ENGINE_RUNNING:
            return
        initial_request_text = self.database.get_state("grid_reset")
        if initial_request_text and not self.database.fetch_all_orders():
            initial_request = json.loads(initial_request_text)
            if initial_request.get("source") == "manual_initial":
                current = await asyncio.to_thread(self._ticker_price)
                if not self._within_grid_bounds(current, strict=True):
                    raise TradingHalt("Price left the approved grid bounds before startup.")
                saved_run = self.database.get_state("grid_run")
                if not saved_run:
                    raise TradingHalt("Approved grid has no saved run.")
                approved_center = Decimal(json.loads(saved_run)["anchor"])
                buys, sells = configured_grid_levels(approved_center, self.config)
                ratio = max(approved_center / buys[0], sells[0] / approved_center)
                if not approved_center / ratio < current < approved_center * ratio:
                    raise TradingHalt("Price moved too far from the approved grid center.")
        await asyncio.to_thread(self.prepare, persist=True)
        if (self.database.get_state("halt_reason") and
                self.database.get_state(SAFETY_MODE_KEY) not in
                (LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED)):
            raise TradingHalt("Saved stop state requires manual review before a new run.")
        await asyncio.to_thread(self._backfill_portfolio_snapshots)
        try:
            if self.database.get_state(SAFETY_MODE_KEY) not in (
                    LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED):
                try:
                    await notifier.notify_startup(self.config.symbol)
                except Exception as error:
                    LOGGER.warning("Startup alert failed: %s", type(error).__name__)
            backoff = 1
            while True:
                self.assert_runner_lease()
                if self.stop_controller.stop_requested.is_set():
                    break
                if (self.ready_mode and
                        self.database.get_state(ENGINE_STATUS_KEY) != ENGINE_RUNNING):
                    return
                try:
                    await asyncio.to_thread(self._audit_full_book_if_due)
                    mode = self.database.get_state(SAFETY_MODE_KEY)
                    if mode in (LIQUIDATED, LIQUIDATION_HALTED):
                        await self._notify_liquidation(notifier)
                        await asyncio.to_thread(
                            self.stop_controller.stop_requested.wait,
                            self.config.poll_seconds,
                        )
                        continue
                    if mode == LIQUIDATING:
                        await asyncio.to_thread(self.liquidate,
                                                self.config.stop_loss_price)
                        await self._notify_liquidation(notifier)
                        await asyncio.to_thread(
                            self.stop_controller.stop_requested.wait,
                            self.config.poll_seconds,
                        )
                        continue
                    if self.grid_needs_reset:
                        price = await asyncio.to_thread(self._ticker_price)
                        await asyncio.to_thread(self.advance_trailing_stop, price)
                        if price < self.config.stop_loss_price:
                            await asyncio.to_thread(self.liquidate, price)
                            await self._notify_liquidation(notifier)
                            continue
                        complete = await asyncio.to_thread(self.reset_grid)
                        await self._notify_safety_state(notifier)
                        backoff = 1
                        if not complete:
                            await asyncio.to_thread(
                                self.stop_controller.stop_requested.wait,
                                self.config.poll_seconds,
                            )
                        continue
                    notification_text = self.database.get_state(
                        "grid_reset_notification_pending"
                    )
                    if notification_text:
                        try:
                            try:
                                notification = json.loads(notification_text)
                            except (TypeError, ValueError):
                                notification = {}
                            if not isinstance(notification, dict):
                                notification = {}
                            await notifier.notify_grid_reset(
                                notification.get("event"),
                                notification.get("lower"),
                                notification.get("upper"),
                            )
                        except Exception:
                            LOGGER.warning("Grid reset notification could not be delivered.")
                        else:
                            self.database.clear_state("grid_reset_notification_pending")
                    if self.database.get_state(BREAKOUT_NOTICE_KEY):
                        try:
                            await notifier.notify_breakout_shift()
                        except Exception as error:
                            LOGGER.warning("Breakout Telegram update failed: %s",
                                           type(error).__name__)
                        else:
                            self.database.clear_state(BREAKOUT_NOTICE_KEY)
                    events = await asyncio.to_thread(self.run_cycle)
                    await self._notify_liquidation(notifier)
                    # Raw fills are local logs; trade alerts wait for committed
                    # BUY cost/commission or SELL realized-P&L accounting.
                    await self._notify_safety_state(notifier)
                    backoff = 1
                    delay = 0 if self.grid_needs_reset else self.config.poll_seconds
                except (ccxt.RateLimitExceeded, ccxt.DDoSProtection) as error:
                    _journal_fault(self.database, error, "API_CIRCUIT_OPEN" if
                                   isinstance(error, CircuitOpenError) else "API_RATE_LIMIT")
                    delay = exchange_retry_delay(self.exchange, error, min(backoff, 60))
                    backoff = min(backoff * 2, 60)
                except ccxt.NetworkError:
                    if self.is_paused:
                        await self._notify_safety_state(notifier)
                    delay = min(backoff, 60)
                    backoff = min(backoff * 2, 60)
                except RunnerLeaseLost:
                    raise
                except Exception as error:
                    if self.grid_needs_reset:
                        self.database.set_state("halt_reason", "grid_reset_failed")
                    await asyncio.to_thread(self.halt_after_fault, error)
                    if self.is_paused:
                        await self._notify_safety_state(notifier)
                    try:
                        await notifier.notify_critical_error(error)
                    except Exception:
                        pass
                    raise
                await asyncio.to_thread(self.stop_controller.stop_requested.wait, delay)
        finally:
            if (not self.stop_controller.stop_requested.is_set() and not self.is_paused
                    and not (self.ready_mode and self.database.get_state(ENGINE_STATUS_KEY)
                             == ENGINE_IDLE)):
                await asyncio.to_thread(self.halt_after_fault, "ProcessStopping")


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
        raise TradingHalt("Cannot center bounds without a valid Spot price.")
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
        f"Centered bounds on Spot {TRADING_SETTINGS.environment.value} price {price}: "
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
        investment_quote=Decimal(active.get("investment_quote", str(config.investment_quote))),
        buy_grid_levels=active.get("buy_levels", config.buy_grid_levels),
        sell_grid_levels=active.get("sell_levels", config.sell_grid_levels),
        stop_loss_price=Decimal(
            active.get("stop_loss_price", str(config.stop_loss_price))
        ),
    )


async def _reconcile_service_boot(bot: GridBot, *, ready: bool) -> None:
    """Verify the exchange before either service mode can run a trading loop."""
    if ready:
        bot.assert_runner_lease()
        if bot.database.get_state(SAFETY_MODE_KEY) == LIQUIDATING:
            bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        else:
            bot.database.require_order_cleanup(
                "Ready-mode restart; Binance order cleanup is unverified."
            )
        try:
            await asyncio.to_thread(bot._reconcile_state)
        except RunnerLeaseLost:
            raise
        except TradingHalt:
            # Keep the dashboard available so its engine_fault explains why
            # START is locked. Reconciliation has persisted cleanup-required.
            pass
        if bot.database.get_state(ENGINE_STOP_CLEANUP_KEY) == "1":
            try:
                await asyncio.to_thread(bot.reconcile_idle_orders)
            except Exception as error:
                _journal_fault(bot.database, error, "STARTUP_CLEANUP_FAILED")
                LOGGER.exception("Startup order cleanup remains unresolved.")
        liquidation_text = bot.database.get_state(LIQUIDATION_KEY)
        if (bot.database.get_state(ENGINE_STOP_CLEANUP_KEY) is None and
                bot.database.get_state(SAFETY_MODE_KEY) == LIQUIDATED and
                liquidation_text and
                json.loads(liquidation_text).get("phase") == "complete"):
            bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_HALTED)
    else:
        # Direct execution also needs a full exchange/ledger boot check.
        try:
            await asyncio.to_thread(bot._reconcile_state)
        except RunnerLeaseLost:
            raise
        except TradingHalt:
            try:
                await asyncio.to_thread(bot.reconcile_idle_orders)
            except Exception as error:
                _journal_fault(bot.database, error, "STARTUP_CLEANUP_FAILED")
                LOGGER.exception("Direct-mode boot cleanup remains unresolved.")
            raise
        bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING, trigger_event="START")


async def _ensure_fault_cleanup(bot: GridBot, error: Exception) -> None:
    """Cover failures before and inside run(), including direct execution."""
    _journal_fault(bot.database, error, "RUNNER_LEASE_LOST" if
                   isinstance(error, RunnerLeaseLost) else "ENGINE_FAULT")
    if not getattr(error, "_telegram_alert_scheduled", False):
        await TelegramNotifier().notify_critical_error(error)
    if isinstance(error, RunnerLeaseLost):
        return  # A newer owner is responsible; the fenced process must not cancel.
    if bot.stop_controller.stop_requested.is_set():
        if bot.database.get_state(ENGINE_STOP_CLEANUP_KEY) == "1":
            try:
                await asyncio.to_thread(bot.reconcile_idle_orders)
            except Exception as cleanup_error:
                _journal_fault(bot.database, cleanup_error, "FAULT_CLEANUP_FAILED")
                LOGGER.exception("Fault cleanup remains unresolved.")
        elif bot.database.get_state(ENGINE_STATUS_KEY) != ENGINE_IDLE:
            await asyncio.to_thread(bot.halt_after_fault, error)
    else:
        await asyncio.to_thread(bot.halt_after_fault, error)
