#!/usr/bin/env python3
"""Standalone daily BTC-USD illustration: pure dip grid vs SMA200 BUY filter.

Default test: 2021-01-01 inclusive through 2023-01-01 exclusive, $10,000,
$500 all-in tickets, 5% drop rungs, 5% per-lot SELL targets, 0.1% fees/side.
Downloads 200 earlier days solely to warm up SMA200. No app/env/DB imports.

Signals use completed daily closes and execute at the NEXT day's open. Each
5% rung down from a rolling closing-price reference creates one BUY signal;
crossed rungs advance the reference even when filtered or unfunded. Recovering
closes raise that reference. Hybrid requires signal close > its SMA200 AND
next execution open > the last available SMA200. Existing lots always retain
SELL targets; a daily High touching entry * 1.05 fills at that target price.
BUYs occur only at the open, so the later daily High cannot precede placement.

Drawdown uses daily CLOSING marked equity, not intraday extrema. Full SELL
fills on touch, USD=USDT, no spread/slippage, no hard stops/leverage, and no
forced final sale are declared simplifications. This does not replay the live
engine or establish a statistical edge.
"""
import argparse
from dataclasses import asdict, dataclass
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

import pandas as pd
import yfinance as yf


@dataclass(frozen=True)
class Config:
    capital: D = D('10000')
    ticket: D = D('500')
    drop: D = D('0.05')
    recovery: D = D('0.05')
    fee: D = D('0.001')

    def __post_init__(self):
        if not all(value.is_finite() for value in asdict(self).values()):
            raise ValueError('Parameters must be finite.')
        if not 0 < self.ticket <= self.capital or not 0 < self.drop < 1:
            raise ValueError('Require 0 < ticket <= capital and 0 < drop < 1.')
        if not 0 < self.recovery < 1 or not 0 <= self.fee < 1:
            raise ValueError('Require 0 < recovery < 1 and 0 <= fee < 1.')


def utc_day(value):
    stamp = pd.Timestamp(value)
    stamp = stamp.tz_localize('UTC') if stamp.tzinfo is None else stamp.tz_convert('UTC')
    if stamp != stamp.normalize():
        raise ValueError('Dates must align to UTC midnight.')
    return stamp


def load_history(start, end, csv=None, download=None):
    """Return complete OHLC history with SMA200, rejecting stale/gapped data."""
    start, end = utc_day(start), utc_day(end)
    if start >= end or end > pd.Timestamp.now(tz='UTC').normalize():
        raise ValueError('Require start < end and only fully completed daily candles.')
    warmup = start - pd.Timedelta(days=200)
    if csv is None:
        frame = (download or yf.download)(
            'BTC-USD', start=warmup.date().isoformat(), end=end.date().isoformat(),
            interval='1d', auto_adjust=False, progress=False, threads=False,
            multi_level_index=False, timeout=15,
        )
    else:
        frame = pd.read_csv(csv, index_col='timestamp', parse_dates=['timestamp'])
    if frame is None or frame.empty:
        raise ValueError('No BTC-USD history was returned.')
    if isinstance(frame.columns, pd.MultiIndex):
        frame = frame.xs('BTC-USD', axis=1, level=-1)
    frame = frame[['Open','High','Low','Close']].copy()
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True))
    if frame.index.has_duplicates or not (frame.index == frame.index.normalize()).all():
        raise ValueError('Daily timestamps are duplicated or not UTC midnight.')
    expected = pd.date_range(warmup, end - pd.Timedelta(days=1), freq='D')
    frame = frame.reindex(expected).apply(pd.to_numeric, errors='coerce')
    if not frame.map(lambda value: pd.notna(value) and 0 < value < float('inf')).all().all():
        raise ValueError('Missing, nonfinite or nonpositive OHLC data; no forward-fill allowed.')
    if ((frame.High < frame[['Open','Close','Low']].max(axis=1)).any() or
            (frame.Low > frame[['Open','Close','High']].min(axis=1)).any()):
        raise ValueError('Invalid OHLC range.')
    frame['SMA200'] = frame.Close.rolling(200, min_periods=200).mean()
    frame['PreviousSMA200'] = frame.SMA200.shift(1)
    frame.index.name = 'timestamp'
    return frame


def simulate(history, start, end, *, hybrid=False, config=Config()):
    """Run identical execution/accounting rules; only the BUY gate differs."""
    start, end = utc_day(start), utc_day(end)
    frame = history.loc[(history.index >= start) & (history.index < end)]
    if frame.empty or frame[['SMA200','PreviousSMA200']].isna().any().any():
        raise ValueError('Simulation needs execution data and full SMA warm-up.')
    cash = peak = config.capital
    reference = D(str(frame.Open.iloc[0]))
    pending = []
    lots, trades, curve = [], [], []
    realized = fees = max_drawdown = D(0)
    buy_count = sell_count = blocked = unfunded = 0
    for day, row in frame.iterrows():
        opening, high, close = (D(str(row[name])) for name in ('Open','High','Close'))
        sma, previous_sma = D(str(row.SMA200)), D(str(row.PreviousSMA200))
        # These signals were made after the preceding day's close, not this day's.
        for rung in pending:
            if hybrid and opening <= previous_sma:
                blocked += 1
                continue
            if cash < config.ticket:
                unfunded += 1
                continue  # No borrowing, deposits, partial tickets or future cash.
            notional = config.ticket / (1 + config.fee)
            quantity, fee = notional / opening, config.ticket - notional
            cash -= config.ticket
            fees += fee
            lots.append(dict(quantity=quantity, entry=opening,
                             target=opening * (1 + config.recovery), cost=config.ticket))
            trades.append(dict(timestamp=day.isoformat(), side='BUY', price=float(opening),
                               quantity=float(quantity), fee=float(fee), realized_pnl=0.,
                               signal_rung=float(rung), execution='next_daily_open'))
            buy_count += 1
        # SELL limits are independent of macro trend. A lot bought at the open
        # may sell later that day; no intra-day replacement BUYs are simulated.
        retained = []
        for lot in lots:
            if high >= lot['target']:
                gross = lot['quantity'] * lot['target']
                fee = gross * config.fee
                profit = gross - fee - lot['cost']
                cash += gross - fee
                fees += fee
                realized += profit
                sell_count += 1
                trades.append(dict(timestamp=day.isoformat(), side='SELL', price=float(lot['target']),
                                   quantity=float(lot['quantity']), fee=float(fee), realized_pnl=float(profit),
                                   signal_rung=None, execution='daily_high_touches_limit'))
            else:
                retained.append(lot)
        lots = retained
        pending = []
        reference = max(reference, close)
        while close <= reference * (1 - config.drop):
            reference *= 1 - config.drop
            if not hybrid or close > sma:
                pending.append(reference)
            else:
                blocked += 1
        held = sum((lot['quantity'] for lot in lots), D(0))
        cost = sum((lot['cost'] for lot in lots), D(0))
        equity = cash + held * close
        unrealized = held * close - cost
        assert cash >= 0
        assert abs(equity - config.capital - realized - unrealized) < D('1e-12')
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
        curve.append(dict(timestamp=day.isoformat(), cash=float(cash), btc=float(held),
                          close=float(close), equity=float(equity), sma200=float(sma),
                          realized_pnl=float(realized), unrealized_pnl=float(unrealized),
                          signal_uptrend=bool(close > sma), reference=float(reference)))
    result = dict(scenario='B: Hybrid SMA200' if hybrid else 'A: Pure Grid',
                  initial_capital=float(config.capital), final_portfolio_value=float(equity),
                  remaining_usdt=float(cash), held_btc=float(held), held_btc_value=float(held * close),
                  realized_pnl=float(realized), unrealized_pnl=float(unrealized),
                  total_return_pct=float((equity / config.capital - 1) * 100),
                  max_drawdown_pct=float(max_drawdown * 100), fees_usdt=float(fees),
                  buy_executions=buy_count, sell_executions=sell_count,
                  filtered_buy_signals=blocked, unfunded_buy_signals=unfunded,
                  unexecuted_end_signals=len(pending), daily_candles=len(frame),
                  final_mark_price=float(close))
    return result, pd.DataFrame(curve), pd.DataFrame(trades, columns=[
        'timestamp','side','price','quantity','fee','realized_pnl','signal_rung','execution'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--start', default='2021-01-01')
    parser.add_argument('--end', default='2023-01-01', help='Exclusive UTC end date')
    parser.add_argument('--capital', type=D, default=D('10000'))
    parser.add_argument('--ticket', type=D, default=D('500'), help='All-in USDT per purchase, including BUY fee')
    parser.add_argument('--drop-pct', type=D, default=D('5'))
    parser.add_argument('--recovery-pct', type=D, default=D('5'))
    parser.add_argument('--fee', type=D, default=D('0.001'), help='Fraction per side; default 0.1%%')
    parser.add_argument('--csv', type=Path, help='Replay saved OHLC including 200-day warm-up, without network')
    parser.add_argument('--output', type=Path, default=Path('hybrid_backtest_results'))
    args = parser.parse_args(argv)
    try:
        config = Config(args.capital,args.ticket,args.drop_pct / 100,args.recovery_pct / 100,args.fee)
        start, end = utc_day(args.start), utc_day(args.end)
        names = ['btc_usd_daily.csv','summary.json','equity_pure.csv','equity_hybrid.csv',
                 'trades_pure.csv','trades_hybrid.csv']
        if args.csv and args.csv.resolve() in [(args.output / name).resolve() for name in names]:
            raise ValueError('Choose a different output directory so replay cannot overwrite the input.')
        history = load_history(start,end,args.csv)
        args.output.mkdir(parents=True,exist_ok=True)
        snapshot = args.output / names[0]
        history[['Open','High','Low','Close']].to_csv(snapshot)
        results = []
        for hybrid, label in ((False,'pure'),(True,'hybrid')):
            result, equity, trades = simulate(history,start,end,hybrid=hybrid,config=config)
            results.append(result)
            equity.to_csv(args.output / f'equity_{label}.csv',index=False)
            trades.to_csv(args.output / f'trades_{label}.csv',index=False)
        summary = dict(start_utc=start.isoformat(),end_exclusive_utc=end.isoformat(),
                       symbol='BTC-USD',source=str(args.csv) if args.csv else 'Yahoo Finance via yfinance',
                       history_sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                       script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       config={key:str(value) for key,value in asdict(config).items()},
                       yfinance_version=yf.__version__,pandas_version=pd.__version__,
                       model_notes=__doc__,results=results)
        (args.output / 'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
        comparison = pd.DataFrame([{
            'Scenario':r['scenario'],'Final Portfolio (USDT)':r['final_portfolio_value'],
            'Max DD (%)':r['max_drawdown_pct'],'Return (%)':r['total_return_pct'],
            'Cash (USDT)':r['remaining_usdt'],'Held BTC':r['held_btc'],
            'BUYs':r['buy_executions'],'SELLs':r['sell_executions'],
        } for r in results])
        print(f'BTC-USD | {start.date()} to {end.date()} (exclusive) | {results[0]["daily_candles"]} daily bars')
        print(comparison.to_string(index=False,float_format=lambda value:f'{value:,.4f}'))
        print('\nDD uses daily closing equity. Unsold BTC remains marked; no final forced sale.')
        print('Daily toy model: next-open BUYs, full SELL fills on touch, no slippage, USD=USDT.')
        print('This paired historical illustration does not prove profitability or replay the live engine.')
        print(f'Reports: {args.output.resolve()}')
        return 0
    except Exception as error:
        parser.exit(1,f'Backtest failed: {type(error).__name__}: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
