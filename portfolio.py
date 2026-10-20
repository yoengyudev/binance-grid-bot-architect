"""Ledger-derived wallet, cost basis, and fee calculations."""

import logging
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple
from database import GridDatabase
from settings import (
    FILL_SNAPSHOT_PREFIX,
    LAST_MARKET_PRICE_KEY,
    LIQUIDATION_KEY,
    MANUAL_ACCOUNT_EXIT_REASON,
    SPOT_FEE_RATE,
)
from models import (
    TradingHalt,
    _order_decimal,
)

LOGGER = logging.getLogger(__name__)


def _unavailable_wallet() -> Dict[str, Optional[float]]:
    return {"btc_held": None, "average_cost": None, "unrealized_pnl": None}


def _realized_pnl_payload(database: Optional[GridDatabase]) -> Dict[str, Optional[float]]:
    if database is None:
        return {"today": None, "seven_day": None, "total": None}
    totals = database.realized_pnl_summary()
    return {key: float(totals[key]) for key in ("today", "seven_day", "total")}


def _portfolio_wallet(database: GridDatabase, *,
                      include_verifying_inventory: bool = False) -> Dict[str, Optional[float]]:
    """Value the current run's remaining BTC lots from persisted fill snapshots."""
    try:
        if database.get_state("halt_reason") == MANUAL_ACCOUNT_EXIT_REASON:
            # An administrator sold the entire Spot BTC balance outside the grid.
            # Preserve historical fills while clearing current exposure.
            return {"btc_held": 0.0, "average_cost": None, "unrealized_pnl": 0.0}
        liquidation = database.get_state(LIQUIDATION_KEY)
        state = json.loads(liquidation) if liquidation else None
        if liquidation and not isinstance(state, dict):
            return _unavailable_wallet()
        # Only the risk verifier opts in, after refreshing every tracked order.
        # UI readers keep reporting unknown inventory until verification finishes.
        if state is not None and not (include_verifying_inventory and state.get('phase') == 'verifying'):
            if state.get("phase") == "dust_acknowledged":
                # The residual has its own persistent reserve ledger and warning.
                if not any(row["id"] == state.get("dust_reserve_id") and
                           row["status"] == "UNMONITORED" and
                           Decimal(row["quantity"]) == Decimal(state["residual_base"])
                           for row in database.fetch_dust_reserves()):
                    return _unavailable_wallet()
                return {"btc_held": 0.0, "average_cost": None, "unrealized_pnl": 0.0}
            if state.get("phase") == "complete":
                residual = float(state.get("residual_base", "0"))
                return {"btc_held": residual, "average_cost": None,
                        "unrealized_pnl": 0.0 if residual == 0 else None}
            if state.get("phase") == "failed" and state.get("failure_type") == "LiquidationDust":
                residual = Decimal(state["residual_base"])
                held = Decimal(state["held_base"])
                cost = Decimal(state["cost_basis_quote"])
                if (not all(value.is_finite() for value in (residual, held, cost))
                        or not 0 < residual <= held or cost <= 0):
                    return _unavailable_wallet()
                average = cost / held
                price_text = database.get_state(LAST_MARKET_PRICE_KEY)
                price = Decimal(price_text) if price_text else None
                pnl = (residual * (price - average)
                       if price is not None and price.is_finite() and price > 0 else None)
                return {"btc_held": float(residual), "average_cost": float(average),
                        "unrealized_pnl": float(pnl) if pnl is not None else None}
            return _unavailable_wallet()
        carry_text = database.get_state("carry_inventory")
        carry = json.loads(carry_text) if carry_text else {}
        rows = database.fetch_all_orders()
        buys: Dict[str, Tuple[Decimal, Decimal]] = {}
        sold_by_buy: Dict[str, Decimal] = {}
        for row in rows:
            order_id = row["order_id"]
            if order_id == carry.get("order_id"):
                filled = Decimal(row["amount"])
                quote_cost = Decimal(carry["cost"])
                base_fee = quote_fee = Decimal(0)
            else:
                snapshot_text = database.get_state(FILL_SNAPSHOT_PREFIX + order_id)
                if snapshot_text:
                    snapshot = json.loads(snapshot_text)
                    filled = Decimal(snapshot["filled_base"])
                    quote_cost = Decimal(snapshot["filled_quote"])
                    base_fee = Decimal(snapshot["base_fee"])
                    quote_fee = Decimal(snapshot["quote_fee"])
                elif row["status"] == "OPEN":
                    filled = quote_cost = base_fee = quote_fee = Decimal(0)
                else:
                    # A historical fill cannot be valued from its limit price.
                    return _unavailable_wallet()
            if any(not value.is_finite() or value < 0 for value in
                   (filled, quote_cost, base_fee, quote_fee)):
                return _unavailable_wallet()
            if row["side"] == "BUY":
                acquired = filled - base_fee
                spent = quote_cost + quote_fee
                if acquired < 0 or (acquired > 0 and spent <= 0):
                    return _unavailable_wallet()
                buys[order_id] = (acquired, spent)
            elif filled > 0:
                parent_id = row["parent_order_id"]
                if not parent_id:
                    return _unavailable_wallet()
                sold_by_buy[parent_id] = (
                    sold_by_buy.get(parent_id, Decimal(0)) + filled + base_fee
                )

        held = remaining_cost = Decimal(0)
        for order_id, (acquired, spent) in buys.items():
            sold = sold_by_buy.pop(order_id, Decimal(0))
            if sold > acquired:
                return _unavailable_wallet()
            remaining = acquired - sold
            if remaining > 0:
                held += remaining
                remaining_cost += spent * remaining / acquired
        if sold_by_buy:
            return _unavailable_wallet()
        if held == 0:
            return {"btc_held": 0.0, "average_cost": None, "unrealized_pnl": 0.0}

        price_text = database.get_state(LAST_MARKET_PRICE_KEY)
        price = Decimal(price_text) if price_text else None
        if price is not None and (not price.is_finite() or price <= 0):
            price = None
        return {
            "btc_held": float(held),
            "average_cost": float(remaining_cost / held),
            "unrealized_pnl": float(held * price - remaining_cost) if price else None,
        }
    except (KeyError, TypeError, ValueError, InvalidOperation, ZeroDivisionError):
        LOGGER.warning("Portfolio exposure is unavailable from saved fill data.")
        return _unavailable_wallet()


def _fees_in_asset(order: Dict[str, Any], asset: str) -> Decimal:
    fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
    return sum(
        (_order_decimal(fee.get("cost")) for fee in fees if fee.get("currency") == asset),
        Decimal(0),
    )


def _pnl_fee_in_quote(
    order: Dict[str, Any], base: str, quote: str, notional: Decimal,
    base_fee: Decimal, quote_fee: Decimal,
) -> Tuple[Decimal, bool]:
    """Use exchange fees when valued in the traded pair; otherwise estimate 0.1%."""
    fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
    if fees and all(fee.get("currency") in (base, quote) for fee in fees):
        # Base fees are already reflected in the net BTC cost-basis allocation.
        return quote_fee, True
    if base_fee or quote_fee:
        return quote_fee, True
    return notional * SPOT_FEE_RATE, False


def save_fill_snapshot(
    database: GridDatabase, base: str, quote: str, order_id: str, order: Dict[str, Any],
    *, verified_fees: Optional[Tuple[Decimal, Decimal]] = None,
) -> None:
    """Persist exchange-confirmed quantities and cost basis without network I/O."""
    try:
        if order.get("filled") is None:
            return
        filled = Decimal(str(order["filled"]))
        if not filled.is_finite() or filled < 0:
            return
        quote_cost = Decimal(str(order.get("cost") or 0))
        if filled > 0 and quote_cost <= 0:
            execution_price = order.get("average") or order.get("price")
            if execution_price is None:
                return
            quote_cost = filled * Decimal(str(execution_price))
        key = FILL_SNAPSHOT_PREFIX + order_id
        previous_text = database.get_state(key)
        previous = json.loads(previous_text) if previous_text else {}
        if filled < Decimal(previous.get("filled_base", "0")):
            raise TradingHalt(f"Cumulative execution regressed for {order_id}; ledger retained.")
        if verified_fees is not None:
            base_fee, quote_fee = verified_fees
            fee_source = "trades"
            fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        elif (previous.get("fee_source") == "trades" and
              Decimal(previous["filled_base"]) == filled):
            base_fee = Decimal(previous["base_fee"])
            quote_fee = Decimal(previous["quote_fee"])
            fee_source = "trades"
            fees = previous.get("fees", [])
        else:
            base_fee = _fees_in_asset(order, base)
            quote_fee = _fees_in_asset(order, quote)
            fee_source = "order"
            fees = []
        snapshot = json.dumps({
            "filled_base": str(filled),
            "filled_quote": str(quote_cost),
            "base_fee": str(base_fee),
            "quote_fee": str(quote_fee),
            "fee_source": fee_source,
            # Keep explicit zero and third-asset commissions distinguishable
            # from missing fee data when the raw order endpoint omits fees.
            "fees": [{'currency': fee.get('currency'), 'cost': str(fee.get('cost'))}
                     for fee in fees],
        }, sort_keys=True)
        if previous_text != snapshot:
            database.set_state(key, snapshot)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        LOGGER.warning("Fill snapshot unavailable for order %s.", order_id)
