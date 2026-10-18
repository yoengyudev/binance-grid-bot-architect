"""Local exposure presentation; never changes sizing or adopts wallet inventory."""
import json
from decimal import Decimal, InvalidOperation


def exposure_summary(bot, wallet):
    unavailable = dict(position_cost_quote=None, carried_base=None, carried_cost_quote=None,
                       open_buy_commitment_quote=None, potential_cost_quote=None,
                       configured_allocation_quote=float(bot.config.investment_quote),
                       new_buy_budget_quote=None, exceeds_allocation=None,
                       allocation_is_total_cap=bot.database.get_state('total_allocation_cap') == '1')
    try:
        def number(value):
            result = Decimal(str(value))
            if not result.is_finite() or result < 0:
                raise ValueError('Unknown exposure')
            return result
        rows = bot.database.fetch_all_orders()
        carry = json.loads(bot.database.get_state('carry_inventory') or '{}')
        carried = carried_cost = committed = Decimal(0)
        carry_id = carry.get('order_id')
        for row in rows:
            snapshot = json.loads(bot.database.get_state('fill_snapshot:' + row['order_id']) or '{}')
            filled = number(snapshot.get('filled_base', 0))
            if row['order_id'] == carry_id:
                carried = number(row['amount'])
                carried_cost = number(carry['cost'])
            if row['side'] == 'BUY' and row['status'] in ('OPEN', 'PARTIALLY_FILLED'):
                remaining = number(row['amount']) - filled
                committed += number(remaining) * number(row['price'])
        if carried > 0:
            sold = Decimal(0)
            for row in rows:
                if row['side'] == 'SELL' and row['parent_order_id'] == carry_id:
                    snapshot = json.loads(bot.database.get_state('fill_snapshot:' + row['order_id']) or '{}')
                    sold += number(snapshot.get('filled_base', 0)) + number(snapshot.get('base_fee', 0))
            remaining = number(carried - sold)
            carried_cost *= remaining / carried
            carried = remaining
        held = number(wallet['btc_held'])
        position_cost = held * number(wallet['average_cost']) if held > 0 else Decimal(0)
        allocation = number(bot.config.investment_quote)
        buy_budget = allocation * (1 - number(bot.config.initial_inventory_percent) / 100)
        potential = position_cost + committed
        return dict(position_cost_quote=float(position_cost), carried_base=float(carried),
                    carried_cost_quote=float(carried_cost), open_buy_commitment_quote=float(committed),
                    potential_cost_quote=float(potential), configured_allocation_quote=float(allocation),
                    new_buy_budget_quote=float(buy_budget), exceeds_allocation=potential > allocation,
                    allocation_is_total_cap=bot.database.get_state('total_allocation_cap') == '1')
    except (KeyError, ValueError, TypeError, InvalidOperation, ZeroDivisionError):
        return unavailable
