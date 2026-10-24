"""Explicit execution-cost assumptions and read-only commission validation."""
from dataclasses import dataclass, asdict
from decimal import Decimal as D
import json
import os

POLICY_ID = 'net-edge-validation-v1'
STATE_KEY = 'fee_validation_policy'
INPUTS = {
    'buy_fee': 'GRID_BUY_FEE_PERCENT',
    'sell_fee': 'GRID_SELL_FEE_PERCENT',
    'entry_slippage': 'GRID_ENTRY_SLIPPAGE_PERCENT',
    'exit_slippage': 'GRID_EXIT_SLIPPAGE_PERCENT',
    'minimum_net_return': 'GRID_MIN_NET_RETURN_PERCENT',
}


def positive(*values, zero=False):
    if any(not isinstance(x, D) or not x.is_finite() or (x < 0 if zero else x <= 0) for x in values):
        raise ValueError('Fee model inputs must be finite Decimals with valid signs.')


@dataclass(frozen=True)
class FeePolicy:
    buy_fee: D
    sell_fee: D
    entry_slippage: D
    exit_slippage: D
    minimum_net_return: D

    def __post_init__(self):
        positive(*asdict(self).values(), zero=True)
        if any(x >= 1 for x in asdict(self).values()) or self.minimum_net_return <= 0:
            raise ValueError('Fee/slippage rates must be below 100%; minimum net return must be positive.')

    def serialize(self):
        return dict(policy_id=POLICY_ID, **{key: str(value.normalize()) for key,value in asdict(self).items()})

    def required_gross_return(self):
        return ((1 + self.minimum_net_return) * (1 + self.entry_slippage) /
                ((1-self.buy_fee)*(1-self.sell_fee)*(1-self.exit_slippage)) - 1)

    def net_return(self, buy, sell):
        positive(buy, sell)
        return sell*(1-self.exit_slippage)*(1-self.sell_fee)*(1-self.buy_fee)/(buy*(1+self.entry_slippage))-1

    def project(self, buy, sell, gross_quantity, round_amount, *, base_reserve=D(0)):
        positive(buy, sell, gross_quantity)
        positive(base_reserve, zero=True)
        if base_reserve >= 1:
            raise ValueError('Invalid base reserve.')
        acquired = gross_quantity*(1-self.buy_fee)
        offered = gross_quantity*(1-max(self.buy_fee, base_reserve))
        sold = round_amount(offered)
        if not isinstance(sold, D) or not sold.is_finite() or not 0 <= sold <= offered:
            raise ValueError('SELL precision exceeds modeled usable BTC.')
        if sold == 0:
            raise ValueError('No tradable modeled SELL quantity.')
        # Unsold rounding dust keeps its proportional cost; it is neither a
        # realized loss nor free capital. The base BUY fee enters acquired once.
        allocated_cost = gross_quantity*buy*(1+self.entry_slippage)*sold/acquired
        proceeds = sold*sell*(1-self.exit_slippage)*(1-self.sell_fee)
        # Quantity cancels algebraically. Use one unit formula so Decimal
        # rounding of a dust allocation cannot flip an entry-edge comparison.
        edge = self.net_return(buy, sell)
        return dict(buy_price=buy, sell_target=sell, gross_buy_quantity=gross_quantity,
                    gross_return=sell/buy-1, net_return=edge,
                    net_profit_quote=proceeds-allocated_cost, allocated_cost=allocated_cost,
                    modeled_sell_quantity=sold, retained_model_base=acquired-sold,
                    allowed=edge >= self.minimum_net_return)

    def existing_exit(self, cost, acquired, quantity, sell):
        """Actual acquisition cost already includes entry fees/slippage once."""
        positive(cost, acquired, quantity, sell)
        if quantity > acquired:
            raise ValueError('Exit exceeds the acquired lot.')
        basis = cost*quantity/acquired
        proceeds = quantity*sell*(1-self.exit_slippage)*(1-self.sell_fee)
        return proceeds/basis-1


def configured_policy():
    flag = os.getenv('GRID_FEE_VALIDATION_ENABLED', 'false').strip().lower()
    if flag not in ('true', 'false', '1', '0'):
        raise ValueError('GRID_FEE_VALIDATION_ENABLED must be true/false or 1/0.')
    if flag in ('false', '0'):
        return None
    try:
        rates = {key: D(os.environ[name])/100 for key,name in INPUTS.items()}
        return FeePolicy(**rates)
    except (KeyError, ValueError, ArithmeticError) as error:
        raise ValueError('Fee validation requires explicit finite fee, slippage and positive net-return percentages.') from error


def audit(database, policy, symbol, *, require_verified=False):
    """Read snapshots only. Unknown third-asset costs are never treated as zero."""
    base, quote = symbol.split('/')
    result = dict(allowed=True, reason_code='COST_MODEL_VALID', verified_orders=0,
                  estimated_orders=0, observed_orders=0, held_exits_checked=0,
                  held_exits_below_model=0, worst_held_exit_net_return=None)
    try:
        rows = {row['order_id']: row for row in database.fetch_all_orders()}
        carry = json.loads(database.get_state('carry_inventory') or '{}')
        lots = {}
        for identity, row in rows.items():
            if identity == carry.get('order_id') and not row.get('client_order_id'):
                acquired, cost = D(row['amount']), D(carry['cost'])
                positive(acquired, cost)
                lots[identity] = (acquired, cost)
                continue
            text = database.get_state('fill_snapshot:' + identity)
            if text is None:
                if row['status'] == 'OPEN':
                    continue
                raise ValueError('Missing commission snapshot.')
            snapshot = json.loads(text)
            filled, cost, base_fee, quote_fee = (D(snapshot[key]) for key in
                ('filled_base', 'filled_quote', 'base_fee', 'quote_fee'))
            positive(filled, cost, base_fee, quote_fee, zero=True)
            fees = snapshot.get('fees', [])
            reported = {base:D(0), quote:D(0)}
            for fee in fees:
                amount = D(fee['cost']);positive(amount, zero=True)
                if amount > 0 and fee.get('currency') not in (base, quote):
                    return dict(result, allowed=False, reason_code='UNVALUED_COMMISSION_ASSET')
                if fee.get('currency') in reported:
                    reported[fee['currency']] += amount
            if fees and (reported[base] != base_fee or reported[quote] != quote_fee):
                raise ValueError('Commission totals disagree with the execution snapshot.')
            if filled == 0:
                if base_fee or quote_fee:
                    raise ValueError('Commission without an execution.')
                # Some providers retain the order's reserved quote notional
                # in cost even before an execution; it is not a paid fee.
                continue
            positive(cost)
            if base_fee >= filled:
                raise ValueError('Invalid base commission.')
            result['observed_orders'] += 1
            verified = snapshot.get('fee_source') == 'trades'
            if require_verified and not verified:
                return dict(result, allowed=False, reason_code='UNVERIFIED_COMMISSION')
            rate = base_fee/filled + quote_fee/cost
            ceiling = policy.buy_fee if row['side'] == 'BUY' else policy.sell_fee
            if rate > ceiling:
                return dict(result, allowed=False, reason_code='COMMISSION_EXCEEDS_MODEL')
            result['verified_orders' if verified else 'estimated_orders'] += 1
            if row['side'] == 'BUY':
                acquired, total_cost = filled-base_fee, cost+quote_fee
                if not verified:
                    # Use actual known cost OR the configured allowance, never
                    # add the full planning entry fee on top of actual fees.
                    total_cost = max(total_cost, acquired*cost/filled/(1-policy.buy_fee))
                lots[identity] = (acquired, total_cost)
        for row in rows.values():
            if row['side'] != 'SELL' or row['status'] not in ('OPEN', 'PARTIALLY_FILLED'):
                continue
            lot = lots.get(row['parent_order_id'])
            if lot is None:
                raise ValueError('Exit acquisition cost is unavailable.')
            edge = policy.existing_exit(lot[1], lot[0], D(row['amount']), D(row['price']))
            result['held_exits_checked'] += 1
            result['held_exits_below_model'] += edge < policy.minimum_net_return
            worst = result['worst_held_exit_net_return']
            result['worst_held_exit_net_return'] = min(worst, edge) if worst is not None else edge
        return result
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return dict(result, allowed=False, reason_code='COMMISSION_DATA_UNAVAILABLE')
