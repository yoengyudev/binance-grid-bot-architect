"""Experimental cost-based inventory taper; no exchange or database I/O.

Feature #2 only: this is not a loss budget, new stop, or capital-latch bypass.
"""
from dataclasses import dataclass
from decimal import Decimal as D
import os

POLICY_ID = 'inventory-taper-v1'
SOFT_FRACTION = D('.50')
CEILING_FRACTION = D('.75')


def configured_enabled():
    value = os.getenv('GRID_INVENTORY_SIZING_ENABLED', 'false').strip().lower()
    if value not in ('true', 'false', '1', '0'):
        raise ValueError('GRID_INVENTORY_SIZING_ENABLED must be true/false or 1/0.')
    return value in ('true', '1')


def inventory_budget(allocation, position_cost, pending_cost):
    values = (allocation, position_cost, pending_cost)
    if any(not isinstance(x, D) or not x.is_finite() for x in values):
        raise ValueError('Inventory costs must be finite Decimals.')
    if allocation <= 0 or min(position_cost, pending_cost) < 0:
        raise ValueError('Invalid inventory allocation or cost.')
    ceiling = allocation * CEILING_FRACTION
    factor = max(D(0), min(D(1), (ceiling-position_cost) /
                          (allocation * (CEILING_FRACTION-SOFT_FRACTION))))
    return dict(ceiling=ceiling, position_cost=position_cost, pending_cost=pending_cost,
                factor=factor, room=max(D(0), ceiling-position_cost-pending_cost))


@dataclass(frozen=True)
class BuySize:
    amount: D
    reason: str
    factor: D
    room: D


def size_buy(requested, price, allocation, position_cost, pending_cost, round_amount):
    if any(not isinstance(x, D) or not x.is_finite() or x <= 0
           for x in (requested, price)):
        raise ValueError('Inventory BUY amount and price must be positive Decimals.')
    budget = inventory_budget(allocation, position_cost, pending_cost)
    maximum = min(requested * budget['factor'], budget['room']/price)
    if maximum <= 0:
        return BuySize(D(0), 'INVENTORY_CEILING', budget['factor'], budget['room'])
    amount = round_amount(maximum)
    if (not isinstance(amount, D) or not amount.is_finite() or
            not 0 <= amount <= maximum or amount * price > budget['room']):
        raise ValueError('Exchange rounding exceeded the inventory BUY budget.')
    return BuySize(amount, 'ROUNDED_TO_ZERO' if amount == 0 else
                   'TRIMMED' if amount < requested else 'UNCHANGED',
                   budget['factor'], budget['room'])
