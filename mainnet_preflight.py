"""Mainnet deployment checks. Offline by default; --exchange uses GET/read calls.

Never creates/cancels orders, initializes a database or starts an engine.
Run while the Mainnet service is stopped. Output contains no API credentials.
"""
import argparse
from contextlib import closing
from decimal import Decimal as D
import json
import sqlite3
import time

import ccxt
from database import resolve_database_path
from exchange_handler import create_exchange,create_macro_exchange,load_config,config_path
from models import GridConfig
from production_baseline import BASELINE_ID,MIN_GRID_RETURN,compressed_bounds,closed_hourly_atr
from strategy_policy import closed_daily_sma
from trading_environment import get_trading_settings


def commission_limits(reply):
    """Conservative undiscounted maker/taker + buyer/seller + tax/special costs."""
    discount = reply.get('discount',{})
    if discount.get('enabledForAccount') and discount.get('enabledForSymbol'):
        raise ValueError('Disable BNB commission payment to match the tested base/quote fee model.')
    groups = []
    for name in ('standardCommission','specialCommission','taxCommission'):
        group = {key:D(str(reply[name][key])) for key in ('maker','taker','buyer','seller')}
        if not all(v.is_finite() and 0<=v<1 for v in group.values()):
            raise ValueError('Commission response is invalid; review rates manually.')
        groups.append(group)
    buy = max(sum((g[role]+g['buyer'] for g in groups),D(0)) for role in ('maker','taker'))
    sell = max(sum((g[role]+g['seller'] for g in groups),D(0)) for role in ('maker','taker'))
    if max(buy,sell)>D('.001'):
        raise ValueError('Actual commission exceeds the tested 0.1% per-side ceiling.')
    net = (1+MIN_GRID_RETURN)*(1-buy)*(1-sell)-1
    if net<D('.0019'):
        raise ValueError('The locked 0.4% floor has insufficient net fee margin.')
    return dict(buy_fee_ceiling=str(buy),sell_fee_ceiling=str(sell),minimum_net_cycle_return=str(net))


def database_check(path,*,fresh):
    if not path.is_file():
        raise ValueError('Initialize the fresh Mainnet ledger explicitly before preflight.')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('Database integrity check failed.')
        row = db.execute('SELECT environment FROM trading_environment WHERE id=1').fetchone()
        if row!=('MAINNET',):
            raise ValueError('Mainnet ledger environment marker is missing/wrong.')
        if fresh:
            if db.execute('SELECT COUNT(*) FROM grid_orders').fetchone()[0]:
                raise ValueError('Fresh deployment must not reuse an order ledger.')
            if db.execute('SELECT COUNT(*) FROM bot_state').fetchone()[0]:
                raise ValueError('Fresh deployment ledger already has runtime state; review instead of clearing it.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exchange',action='store_true',help='Opt in to read-only public/private network checks')
    parser.add_argument('--fresh',action='store_true',help='Require an untouched, explicitly initialized Mainnet ledger')
    args = parser.parse_args()
    report = dict(order_actions=False,engine_started=False)
    try:
        selected = get_trading_settings()
        if selected.sandbox:
            raise ValueError('Mainnet preflight requires explicit MAINNET credentials/environment.')
        config = GridConfig.load()
        if config.strategy_baseline!=BASELINE_ID:
            raise ValueError('Locked baseline marker is missing.')
        database = resolve_database_path()
        database_check(database,fresh=args.fresh)
        report.update(environment='MAINNET',baseline=BASELINE_ID,config_path=str(config_path().resolve()),
                      database_path=str(database),offline_checks='PASS',exchange_checks='NOT_RUN')
        if args.exchange:
            exchange = create_exchange()
            before = int(time.time()*1000)
            server = exchange.fetch_time()
            after = int(time.time()*1000)
            if abs(server-(before+after)//2)>1000:
                raise ValueError('Clock offset exceeds 1000 ms; fix time synchronization.')
            exchange.load_markets()
            market = exchange.market(config.symbol)
            if not market.get('spot') or market.get('active') is False:
                raise ValueError('BTC/USDT is not active Spot trading.')
            filters = {f['filterType']:f for f in market['info']['filters']}
            if not {'PRICE_FILTER','LOT_SIZE'}<=filters.keys() or not {'NOTIONAL','MIN_NOTIONAL'}&filters.keys():
                raise ValueError('Required live price/lot/notional filters were not returned.')
            balance = exchange.fetch_balance()
            if D(str(balance['USDT']['free']))<config.investment_quote:
                raise ValueError('Free USDT is below the explicitly configured allocation cap.')
            if args.fresh and D(str(balance['BTC']['total']))!=0:
                raise ValueError('Fresh rollout requires a dedicated account with zero existing BTC.')
            if exchange.fetch_open_orders(config.symbol):
                raise ValueError('Resolve existing BTC/USDT orders before fresh deployment.')
            fees = commission_limits(exchange.private_get_account_commission({'symbol':market['id']}))
            center = D(str(exchange.fetch_ticker(config.symbol)['last']))
            atr,_,available = closed_hourly_atr(exchange.fetch_ohlcv(config.symbol,'1h',None,16),int(time.time()*1000))
            tick = D(filters['PRICE_FILTER']['tickSize'])
            lower,upper = compressed_bounds(center,atr,tick=tick,
                round_price=lambda p:D(exchange.price_to_precision(config.symbol,str(p))))
            # Gate checks are read-only. A blocked macro gate is a WAIT, never
            # permission to substitute RSI or disable the original SMA rules.
            macro = create_macro_exchange()
            sma,_ = closed_daily_sma(macro.fetch_ohlcv(config.symbol,'1d',None,201),int(time.time()*1000))
            from macro_trend import fetch_macro_trend
            yahoo = fetch_macro_trend()
            report.update(exchange_checks='PASS',fees=fees,closed_atr_available_at_ms=available,
                proposed_center=str(center),proposed_lower=str(lower),proposed_upper=str(upper),
                binance_sma_gate_open=center>sma,yahoo_sma_gate_open=yahoo.uptrend,
                live_filters=filters)
            if not center>sma or not yahoo.uptrend:
                raise ValueError('WAIT: SMA200 entry gate is closed; remain IDLE.')
        report['status']='PASS'
        print(json.dumps(report,indent=2,default=str))
        return 0
    except (ValueError,KeyError,TypeError,ArithmeticError,sqlite3.Error) as error:
        report.update(status='BLOCKED',reason=str(error))
    except (ccxt.BaseError,OSError) as error:
        report.update(status='BLOCKED',reason=f'Read-only connection check failed: {type(error).__name__}')
    print(json.dumps(report,indent=2,default=str))
    return 2


if __name__=='__main__':
    raise SystemExit(main())
