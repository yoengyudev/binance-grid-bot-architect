"""Experimental dollar-risk policy and read-only ledger projections.

Estimates are conservative planning assumptions, never guaranteed executions.
"""
from decimal import Decimal as D, ROUND_DOWN
import json
import os

POLICY_ID = 'campaign-dollar-risk-v1'
STATE_KEY = 'campaign_risk_budget'
EXIT_EVENT = 'CAMPAIGN_RISK_LIMIT'
FEE = D('.001')
SLIPPAGE = D('.0025')
EXIT_FRACTION = D('.80')


def configured_budget():
    flag = os.getenv('GRID_RISK_BUDGET_ENABLED', 'false').strip().lower()
    if flag not in ('true', 'false', '1', '0'):
        raise ValueError('GRID_RISK_BUDGET_ENABLED must be true/false or 1/0.')
    if flag in ('false', '0'):
        return None
    try:
        amount = D(os.environ['GRID_RISK_BUDGET_USDT'])
        if not amount.is_finite() or amount <= 0:
            raise ValueError()
    except (KeyError, ArithmeticError, ValueError) as error:
        raise ValueError('Set GRID_RISK_BUDGET_USDT to an explicit positive finite dollar amount.') from error
    return amount


def finite(*values):
    if any(not isinstance(x, D) or not x.is_finite() for x in values):
        raise ValueError('Risk inputs must be finite Decimals.')


def exit_value(price):
    finite(price)
    if price <= 0:
        raise ValueError('Risk price must be positive.')
    return price * (1-SLIPPAGE) * (1-FEE)


def unit_loss(price, stop):
    finite(price, stop)
    if price <= 0 or stop <= 0:
        raise ValueError('Risk prices must be positive.')
    return max(D(0), price*(1+FEE)**2-exit_value(stop))


def projection(budget, realized, held_cost, held_base, stop, mark, pending):
    finite(budget, realized, held_cost, held_base, stop, mark)
    if budget <= 0 or min(held_cost, held_base) < 0:
        raise ValueError('Invalid campaign risk budget or exposure.')
    if (held_base == 0) != (held_cost == 0):
        raise ValueError('Risk position cost and quantity disagree.')
    pending_risk = D(0)
    for price, quantity in pending:
        finite(price, quantity)
        if quantity < 0:
            raise ValueError('Negative pending BUY amount.')
        pending_risk += quantity * unit_loss(price, stop)
    held_risk = max(D(0), held_cost*(1+FEE)-held_base*exit_value(stop))
    realized_drawdown = max(D(0), -realized)  # Profits never expand the entry-risk budget.
    return dict(budget_usdt=budget, exit_trigger_usdt=budget*EXIT_FRACTION,
                realized_pnl=realized, held_cost=held_cost, held_base=held_base,
                held_stop_risk=held_risk, pending_stop_risk=pending_risk,
                modeled_risk=held_risk+pending_risk+realized_drawdown,
                headroom=max(D(0), budget-realized_drawdown-held_risk-pending_risk),
                estimated_exit_pnl=realized+held_base*exit_value(mark)-held_cost*(1+FEE))


def limit_quantity(requested, price, stop, headroom, round_amount):
    finite(requested, headroom)
    if requested <= 0 or headroom < 0:
        raise ValueError('Invalid requested risk quantity or headroom.')
    loss = unit_loss(price, stop)
    maximum = min(requested, headroom/loss) if loss > 0 else requested
    amount = round_amount(maximum)
    if not isinstance(amount, D) or not amount.is_finite() or not 0 <= amount <= maximum:
        raise ValueError('Exchange precision exceeded the risk-sized quantity.')
    return amount


def seed_quote(nominal, price, stop, headroom, seed_fraction):
    finite(nominal, headroom, seed_fraction)
    if nominal <= 0 or headroom < 0 or not 0 < seed_fraction < 1:
        raise ValueError('Invalid seed risk budget.')
    loss = unit_loss(price, stop)
    amount = min(nominal, headroom*seed_fraction*price/loss) if loss > 0 else nominal
    return amount.quantize(D('.00000001'), rounding=ROUND_DOWN)


def pending_buys(database):
    result = []
    for row in database.fetch_active_grids():
        if row['side'] == 'BUY':
            snapshot = json.loads(database.get_state('fill_snapshot:'+row['order_id']) or '{}')
            remaining = D(row['amount'])-D(snapshot.get('filled_base', '0'))
            if remaining < 0:
                raise ValueError('Pending BUY cumulative quantity is invalid.')
            result.append((D(row['price']), remaining))
    return result


def unbooked_sell_losses(database):
    """Do not temporarily free risk when partial/late SELL accounting lags.

    Only losses are charged until ordinary trade_history commits. Positive
    unbooked proceeds never grant additional buying permission.
    """
    booked = {r['sell_order_id'] for r in database.fetch_trade_history()}
    rows = {r['order_id']:r for r in database.fetch_all_orders()}
    carry = json.loads(database.get_state('carry_inventory') or '{}')
    loss = D(0)
    for row in rows.values():
        if row['side'] != 'SELL' or row['order_id'] in booked:
            continue
        snap = json.loads(database.get_state('fill_snapshot:'+row['order_id']) or '{}')
        filled = D(snap.get('filled_base', '0'))
        finite(filled)
        if filled < 0:
            raise ValueError('Negative unbooked SELL execution.')
        if filled == 0:
            continue
        parent = rows[row['parent_order_id']]
        if carry.get('order_id') == parent['order_id'] and not parent.get('client_order_id'):
            acquired, cost = D(parent['amount']), D(carry['cost'])
        else:
            buy = json.loads(database.get_state('fill_snapshot:'+parent['order_id']) or '{}')
            acquired = D(buy['filled_base'])-D(buy['base_fee'])
            cost = D(buy['filled_quote'])+D(buy['quote_fee'])
        consumed = filled+D(snap['base_fee'])
        proceeds = D(snap['filled_quote'])-D(snap['quote_fee'])
        finite(acquired, cost, consumed, proceeds)
        if acquired <= 0 or cost <= 0 or not 0 < consumed <= acquired or proceeds < 0:
            raise ValueError('Invalid unbooked SELL exposure.')
        basis = cost*consumed/acquired
        # Allow additional commission on unverified snapshots instead of using
        # an apparent zero fee to free the risk budget prematurely.
        loss += min(D(0), proceeds*(1-FEE)-basis*(1+FEE))
    return loss
