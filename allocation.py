"""Cost-basis plus outstanding BUY commitment cap; never values reserved BTC."""
import json
from decimal import Decimal

from models import GridSizingError, TradingHalt
from portfolio import _portfolio_wallet


def tracked_position_cost(database):
    wallet = _portfolio_wallet(database)
    if wallet['btc_held'] is None:
        raise TradingHalt('Cannot verify bot cost basis for the total allocation cap.')
    return (Decimal(str(wallet['btc_held'])) * Decimal(str(wallet['average_cost']))
            if wallet['btc_held'] > 0 else Decimal(0))


def pending_buy_commitment(database):
    total = Decimal(0)
    for row in database.fetch_active_grids():
        if row['side'] != 'BUY':
            continue
        snapshot = json.loads(database.get_state('fill_snapshot:' + row['order_id']) or '{}')
        remaining = Decimal(row['amount']) - Decimal(snapshot.get('filled_base', '0'))
        if not remaining.is_finite() or remaining < 0:
            raise TradingHalt('Cannot verify remaining BUY commitment.')
        total += remaining * Decimal(row['price'])
    return total


def require_total_cap(config, position_cost, buy_commitment):
    total = position_cost + buy_commitment
    if (not position_cost.is_finite() or not buy_commitment.is_finite() or
            position_cost < 0 or buy_commitment < 0):
        raise TradingHalt('Total allocation cost is invalid.')
    # Wallet projections cross the API float boundary; tolerate sub-micro noise.
    if total - config.investment_quote > Decimal('0.000001'):
        raise GridSizingError(
            f'Total allocation cap exceeded: bot BTC cost {position_cost:.2f} + '
            f'BUY commitments {buy_commitment:.2f} = {total:.2f} USDT, above '
            f'{config.investment_quote:.2f}. Increase the total allocation or '
            'reduce bot inventory before configuring this grid. The cap uses '
            'cost basis and BUY notionals, excluding future fees and price appreciation.')
