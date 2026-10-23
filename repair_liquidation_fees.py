"""Explicit, leased, idempotent correction of audited historical exit fees.

Dry-run by default. Only named, completed MARKET liquidation sales without a
grid-order row qualify. Purchase cost and original timestamps never change.
"""
import argparse
import json
import time
from decimal import Decimal as D
from uuid import uuid4
import ccxt

from database import GridDatabase
from execution_history import verified_execution_order
from exchange_handler import create_exchange, network_backoff, exchange_retry_delay
from models import TradingHalt
from portfolio import _fees_in_asset, _pnl_fee_in_quote, save_fill_snapshot
from settings import SPOT_FEE_RATE, TRADING_SETTINGS, RUNNER_LEASE_TTL_SECONDS


def read_with_retry(function, *args, **kwargs):
    for attempt in range(4):
        try:
            return function(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.RateLimitExceeded) as error:
            if attempt == 3:
                raise
            time.sleep(exchange_retry_delay(getattr(function, '__self__', None), error,
                                            network_backoff(attempt)))


def repair_trade(database, exchange, symbol, order_id, *, apply=False, before_commit=None):
    row = database.get_trade_by_sell_order_id(order_id)
    if not row or row['buy_order_id'] is not None or not row['sold_base']:
        raise TradingHalt('Not a consolidated liquidation sale.')
    # Exclude normal paired SELLs, including archived campaigns.
    with database._connection() as connection:
        found = connection.execute(
            'SELECT 1 FROM grid_orders WHERE order_id=? UNION ALL '
            'SELECT 1 FROM archived_grid_orders WHERE order_id=?',
            (order_id, order_id)).fetchone()
    if found:
        raise TradingHalt('Normal grid trades are outside this repair.')
    marker = 'liquidation_fee_correction:' + order_id
    previous = database.get_state(marker)
    if previous:
        saved = json.loads(previous)
        if row['profit'] != saved['profit'] or row['fee_basis'] != 'mixed':
            raise TradingHalt('Previously corrected accounting changed; manual review required.')
        return dict(order_id=order_id, already_corrected=True, profit=row['profit'])
    if row['fee_basis'] != 'estimated':
        raise TradingHalt('Only estimated liquidation fees may be repaired.')
    call = read_with_retry
    order = call(exchange.fetch_order, '', symbol, {'origClientOrderId': order_id})
    if (order.get('clientOrderId') != order_id or order.get('symbol') != symbol or
            order.get('side') != 'sell' or order.get('type') != 'market' or
            order.get('status') != 'closed'):
        raise TradingHalt('Exchange order is not the named completed MARKET SELL.')
    if not getattr(exchange, 'has', {}).get('fetchOrderTrades'):
        raise TradingHalt('Private execution history is required for a fee correction.')
    order = verified_execution_order(call, exchange, symbol, order)
    sold, proceeds = D(str(order['filled'])), D(str(order['cost']))
    tolerance = D('0.00000001')
    if sold != D(row['sold_base']) or abs(proceeds - sold * D(row['sell_price'])) > tolerance:
        raise TradingHalt('Saved quantity/proceeds disagree with private executions.')
    base, quote = symbol.split('/')
    if _fees_in_asset(order, base):
        raise TradingHalt('Base-asset liquidation commission needs a separate inventory review.')
    fee, actual = _pnl_fee_in_quote(order, base, quote, proceeds,
                                    _fees_in_asset(order, base), _fees_in_asset(order, quote))
    if not actual:
        raise TradingHalt('Commission cannot be valued from verified pair-asset fees.')
    basis = sold * D(row['buy_price'])
    estimated_profit = proceeds - basis - proceeds * SPOT_FEE_RATE
    if abs(estimated_profit - D(row['profit'])) > tolerance:
        raise TradingHalt('Saved profit does not match the known estimated-fee calculation.')
    # Preserve the original cost-basis precision; fee correction alone changes P&L.
    profit = D(row['profit']) + proceeds * SPOT_FEE_RATE - fee
    result = dict(order_id=order_id, previous_profit=row['profit'], profit=str(profit),
                  correction=str(profit-D(row['profit'])), exchange_fee=str(fee))
    if apply:
        if before_commit is not None:
            before_commit()
        with database.transaction():
            database.correct_liquidation_fee(order_id, row['profit'], profit)
            save_fill_snapshot(database, base, quote, order_id, order,
                               verified_fees=(_fees_in_asset(order, base), fee))
            database.set_state(marker, json.dumps(result, sort_keys=True))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--order-id', action='append', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not TRADING_SETTINGS.sandbox:
        raise TradingHalt('This attended repair is Testnet-only.')
    database = GridDatabase()
    token = uuid4().hex
    epoch = database.acquire_runner_lease(token, ttl_seconds=RUNNER_LEASE_TTL_SECONDS)
    if epoch is None:
        raise TradingHalt('Stop the service and wait for its runner lease before repair.')
    try:
        if (database.get_state('engine_status') != 'HALTED' or
                database.get_state('safety_mode') != 'LIQUIDATED' or
                json.loads(database.get_state('hard_stop_liquidation') or '{}').get('phase') != 'complete'):
            raise TradingHalt('Repair requires a completed, HALTED liquidation.')
        exchange = create_exchange()
        exchange.options['fetchMarkets'] = {'types': ['spot']}
        exchange.options['fetchCurrencies'] = False
        exchange.has['fetchCurrencies'] = False
        original = exchange.fetch
        def readonly(url, method='GET', headers=None, body=None):
            from urllib.parse import urlsplit
            if method.upper() != 'GET' or urlsplit(url).hostname not in (
                    'testnet.binance.vision', 'api1.testnet.binance.vision'):
                raise TradingHalt('Repair permits only Testnet GET requests.')
            return original(url, method, headers, body)
        exchange.fetch = readonly
        if read_with_retry(exchange.fetch_open_orders, 'BTC/USDT'):
            raise TradingHalt('Open orders require review before this repair.')
        results = []
        def check_lease():
            if not database.renew_runner_lease(token, epoch, ttl_seconds=RUNNER_LEASE_TTL_SECONDS):
                raise TradingHalt('Repair lease lost before commit.')
        for order_id in dict.fromkeys(args.order_id):
            check_lease()
            result = repair_trade(database, exchange, 'BTC/USDT', order_id,
                                  apply=args.apply, before_commit=check_lease)
            results.append(result)
        print(json.dumps(dict(applied=args.apply, results=results)))
    finally:
        database.release_runner_lease(token, epoch)


if __name__ == '__main__':
    main()
