"""Confirmed profitable SELL episode fences; no network or order submission."""
from decimal import Decimal as D
import json
import os

from portfolio import _portfolio_wallet

POLICY_ID = 'confirmed-sell-recycling-v1'
STATE_KEY = 'capital_recycling'
FEE_RESERVE = D('.001')


def configured_enabled():
    flag = os.getenv('GRID_CAPITAL_RECYCLING_ENABLED', 'false').strip().lower()
    if flag not in ('true', 'false', '1', '0'):
        raise ValueError('GRID_CAPITAL_RECYCLING_ENABLED must be true/false or 1/0.')
    return flag in ('true', '1')


def finite_nonnegative(*values):
    if any(not isinstance(x, D) or not x.is_finite() or x < 0 for x in values):
        raise ValueError('Recycling amounts must be finite nonnegative Decimals.')


def exposure(database):
    wallet = _portfolio_wallet(database, decimal_values=True)
    if wallet['btc_held'] is None:
        raise ValueError('Unknown recycling inventory.')
    quantity = D(str(wallet['btc_held']))
    held = quantity * D(str(wallet['average_cost'])) if quantity else D(0)
    pending = D(0)
    for row in database.fetch_active_grids():
        if row['side'] != 'BUY':
            continue
        price, amount = D(row['price']), D(row['amount'])
        snap = json.loads(database.get_state('fill_snapshot:' + row['order_id']) or '{}')
        filled = D(snap.get('filled_base', '0'))
        finite_nonnegative(price, amount, filled)
        if price <= 0 or amount <= 0 or filled > amount:
            raise ValueError('Recycling BUY execution exceeds its reservation.')
        pending += (amount - filled) * price
    finite_nonnegative(quantity, held, pending)
    return held, pending


def fits(allocation, held, pending, notional):
    finite_nonnegative(allocation, held, pending, notional)
    if allocation <= 0 or notional <= 0:
        return False
    return held + pending + notional * (1 + FEE_RESERVE) <= allocation


def sell_fence(database):
    history = database.fetch_trade_history()
    booked = {row['sell_order_id'] for row in history}
    excluded = []
    for row in database.fetch_all_orders():
        if row['side'] != 'SELL' or row['order_id'] in booked:
            continue
        snap = json.loads(database.get_state('fill_snapshot:' + row['order_id']) or '{}')
        filled = D(snap.get('filled_base', '0'))
        finite_nonnegative(filled)
        if row['status'] != 'OPEN' or filled > 0:
            excluded.append(row['order_id'])
    return max((r['id'] for r in history), default=0), sorted(excluded)


def settled_final_order(database, episode):
    identity = episode['final_order_id']
    if identity is None:
        return True
    row = database.get_order(identity)
    if (row is None or row['side'] != 'BUY' or row['order_type'] != 'LIMIT' or
            row['client_order_id'] != identity or not row.get('exchange_order_id') or
            row['status'] not in ('FILLED', 'CANCELED')):
        return False
    snap = json.loads(database.get_state('fill_snapshot:' + identity) or 'null')
    if not isinstance(snap, dict):
        return False
    filled, amount = D(snap['filled_base']), D(row['amount'])
    finite_nonnegative(filled, amount)
    return filled <= amount and (row['status'] != 'FILLED' or filled == amount)


def eligible_sell(database, episode, client_prefix, *, require_verified_fees=False):
    """A terminal ordinary SELL newly booked AND not executed at the fence."""
    for trade in database.fetch_trade_history():
        identity = trade['sell_order_id']
        if (trade['id'] <= episode['booked_through'] or identity in episode['excluded_sells'] or
                not identity or not identity.startswith(client_prefix)):
            continue
        row = database.get_order(identity)
        if (row is None or row['side'] != 'SELL' or row['order_type'] != 'LIMIT' or
                row['status'] != 'FILLED' or row['client_order_id'] != identity or
                not row.get('exchange_order_id') or trade['buy_order_id'] != row['parent_order_id'] or
                trade['fee_basis'] not in ('exchange', 'mixed', 'estimated')):
            continue
        profit, sold = D(trade['profit']), D(trade['sold_base'] or '0')
        if not profit.is_finite() or not sold.is_finite() or profit <= 0 or sold <= 0:
            continue
        parent = database.get_order(row['parent_order_id'])
        snap = json.loads(database.get_state('fill_snapshot:' + identity) or '{}')
        if require_verified_fees and snap.get('fee_source') != 'trades':
            continue
        filled = D(snap.get('filled_base', '0'))
        proceeds = D(snap.get('filled_quote', '0'))
        finite_nonnegative(filled, proceeds)
        if (parent is not None and parent['side'] == 'BUY' and proceeds > 0 and
                filled == sold and filled == D(row['amount'])):
            return trade
    return None
