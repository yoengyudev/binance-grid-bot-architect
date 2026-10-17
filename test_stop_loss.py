#!/usr/bin/env python3
"""Daily BTC-USD study of 15% portfolio-equity stops: Pure vs Hybrid SMA200.

Reuses the standalone backtest_hybrid_grid data loader and configuration;
never imports the live engine, credentials, database or exchange clients.
$10,000 initially, $500 all-in tickets, 5% dip rungs, entry * 1.05 SELL
limits and 0.1% fees per side. Close-derived BUY signals execute at the next
open. Hybrid requires signal close > SMA200 and execution open > the prior
completed SMA200. SELL limits remain active below SMA200 in both scenarios.

Stop equity = cash + BTC * daily close (before hypothetical exit costs).
Campaign peak is the highest daily closing equity since the last liquidation,
including initial/post-liquidation cash. With positive BTC, a close <= 85%
of that peak queues a full exit at the NEXT open, before BUYs or grid SELLs.
Thus 15% is a TRIGGER, not a guaranteed realized loss: gaps and fees matter.
Each trigger counts once, even if the dataset ends before its exit executes.
No cash deposits: reset uses remaining capital, resets the price reference
and campaign peak, and can signal a restart at the liquidation day's close.
The global drawdown peak is NEVER reset. No same-day stop/re-entry cycle.

200 prior days warm up SMA200; they are not traded. End is exclusive and
defaults to today's UTC midnight, excluding the incomplete current candle.
Daily-close stops miss intraday breaches; daily High touching a SELL limit
assumes a full fill. No slippage/spread, no leverage, USD=USDT, no final forced
sale. These are model counts, not exact real-world stop frequencies. This
equity stop differs from the live bot's fixed grid-price catastrophic stop.
"""
import argparse
from dataclasses import asdict, dataclass
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

import pandas as pd
import yfinance as yf

from backtest_hybrid_grid import Config, load_history, utc_day


@dataclass(frozen=True)
class StopConfig(Config):
    stop: D = D('0.15')

    def __post_init__(self):
        super().__post_init__()
        if not self.stop.is_finite() or not 0 < self.stop < 1:
            raise ValueError('Require a finite 0 < stop < 1.')


def simulate(history, start, end, *, hybrid=False, config=StopConfig()):
    """Paired daily experiment with distinct campaign and global equity peaks."""
    start, end = utc_day(start), utc_day(end)
    frame = history.loc[(history.index >= start) & (history.index < end)]
    if frame.empty or frame[['SMA200', 'PreviousSMA200']].isna().any().any():
        raise ValueError('Simulation needs execution data and full SMA200 warm-up.')
    cash = campaign_peak = global_peak = config.capital
    reference = D(str(frame.Open.iloc[0]))
    lots, pending, trades, curve, stops = [], [], [], [], []
    pending_stop = None
    realized = fees = max_drawdown = D(0)
    buys = grid_sells = blocked = unfunded = liquidations = 0
    for day, row in frame.iterrows():
        opening, high, close, sma, previous_sma = (
            D(str(row[name])) for name in ('Open', 'High', 'Close', 'SMA200', 'PreviousSMA200'))
        if pending_stop is not None:
            # Exit intent was known at the preceding close; no use of today's High.
            quantity = sum((lot['quantity'] for lot in lots), D(0))
            cost = sum((lot['cost'] for lot in lots), D(0))
            gross = quantity * opening
            fee = gross * config.fee
            profit = gross - fee - cost
            cash += gross - fee
            fees += fee
            realized += profit
            liquidations += 1
            trades.append(dict(timestamp=day.isoformat(), side='SELL', reason='hard_stop',
                               price=float(opening), quantity=float(quantity), fee=float(fee),
                               realized_pnl=float(profit)))
            pending_stop.update(exit_timestamp=day.isoformat(), exit_price=float(opening),
                                exit_fee=float(fee), exit_realized_pnl=float(profit),
                                equity_after_exit=float(cash),
                                loss_from_campaign_peak_pct=float((1-cash/campaign_peak)*100))
            lots, pending = [], []
            pending_stop = None
            campaign_peak = cash
            reference = opening
            # No BUY executes on the liquidation day. The close may queue a new
            # signal for tomorrow, subject to the same macro gate and cash limits.
        else:
            for _rung in pending:
                if hybrid and opening <= previous_sma:
                    blocked += 1
                    continue
                if cash < config.ticket:
                    unfunded += 1
                    continue
                notional = config.ticket / (1+config.fee)
                quantity, fee = notional/opening, config.ticket-notional
                cash -= config.ticket
                fees += fee
                lots.append(dict(quantity=quantity, cost=config.ticket,
                                 target=opening*(1+config.recovery)))
                trades.append(dict(timestamp=day.isoformat(), side='BUY', reason='grid',
                                   price=float(opening), quantity=float(quantity),
                                   fee=float(fee), realized_pnl=0.))
                buys += 1
        pending = []
        retained = []
        for lot in lots:
            if high < lot['target']:
                retained.append(lot)
                continue
            gross = lot['quantity']*lot['target']
            fee = gross*config.fee
            profit = gross-fee-lot['cost']
            cash += gross-fee
            fees += fee
            realized += profit
            grid_sells += 1
            trades.append(dict(timestamp=day.isoformat(), side='SELL', reason='grid',
                               price=float(lot['target']), quantity=float(lot['quantity']),
                               fee=float(fee), realized_pnl=float(profit)))
        lots = retained
        held = sum((lot['quantity'] for lot in lots), D(0))
        cost = sum((lot['cost'] for lot in lots), D(0))
        equity = cash+held*close
        unrealized = held*close-cost
        assert cash >= 0
        assert abs(equity-config.capital-realized-unrealized) < D('1e-12')
        campaign_peak = max(campaign_peak, equity)
        global_peak = max(global_peak, equity)
        max_drawdown = max(max_drawdown, (global_peak-equity)/global_peak)
        threshold = campaign_peak*(1-config.stop)
        if held > 0 and equity <= threshold:
            pending_stop = dict(trigger_timestamp=day.isoformat(), campaign_peak=float(campaign_peak),
                                trigger_equity=float(equity), threshold_equity=float(threshold),
                                trigger_price=float(close), btc_to_sell=float(held),
                                exit_timestamp=None, exit_price=None, exit_fee=None,
                                exit_realized_pnl=None, equity_after_exit=None,
                                loss_from_campaign_peak_pct=None)
            stops.append(pending_stop)
        else:
            reference = max(reference, close)
            while close <= reference*(1-config.drop):
                reference *= 1-config.drop
                if not hybrid or close > sma:
                    pending.append(reference)
                else:
                    blocked += 1
        curve.append(dict(timestamp=day.isoformat(), cash=float(cash), btc=float(held),
                          close=float(close), equity=float(equity), sma200=float(sma),
                          realized_pnl=float(realized), unrealized_pnl=float(unrealized),
                          campaign_peak=float(campaign_peak), global_peak=float(global_peak),
                          stop_pending=pending_stop is not None, stop_count=len(stops)))
    result = dict(scenario='B: Hybrid SMA200' if hybrid else 'A: Pure Grid',
                  hard_stop_triggers=len(stops), completed_liquidations=liquidations,
                  pending_end_liquidations=int(pending_stop is not None),
                  daily_candles=len(frame), initial_capital=float(config.capital),
                  final_portfolio_value=float(equity), remaining_usdt=float(cash),
                  held_btc=float(held), final_mark_price=float(close),
                  realized_pnl=float(realized), unrealized_pnl=float(unrealized),
                  total_return_pct=float((equity/config.capital-1)*100),
                  max_closing_drawdown_pct=float(max_drawdown*100), fees_usdt=float(fees),
                  buy_executions=buys, grid_sell_executions=grid_sells,
                  filtered_buy_signals=blocked, unfunded_buy_signals=unfunded,
                  unexecuted_end_buy_signals=len(pending))
    return (result, pd.DataFrame(curve), pd.DataFrame(trades, columns=[
        'timestamp', 'side', 'reason', 'price', 'quantity', 'fee', 'realized_pnl']),
        pd.DataFrame(stops, columns=[
            'trigger_timestamp', 'campaign_peak', 'trigger_equity', 'threshold_equity',
            'trigger_price', 'btc_to_sell', 'exit_timestamp', 'exit_price', 'exit_fee',
            'exit_realized_pnl', 'equity_after_exit', 'loss_from_campaign_peak_pct']))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--start', default='2020-01-01')
    parser.add_argument('--end', default=pd.Timestamp.now(tz='UTC').date().isoformat(),
                        help='Exclusive UTC end; default today (completed days only)')
    parser.add_argument('--capital', type=D, default=D('10000'))
    parser.add_argument('--ticket', type=D, default=D('500'))
    parser.add_argument('--fee', type=D, default=D('0.001'), help='Fraction per side')
    parser.add_argument('--stop-pct', type=D, default=D('15'))
    parser.add_argument('--csv', type=Path, help='Offline OHLC snapshot including 200-day warm-up')
    parser.add_argument('--output', type=Path, default=Path('stop_loss_results'))
    args = parser.parse_args(argv)
    try:
        config = StopConfig(capital=args.capital, ticket=args.ticket, fee=args.fee, stop=args.stop_pct/100)
        start, end = utc_day(args.start), utc_day(args.end)
        names = ['btc_usd_daily.csv', 'summary.json'] + [
            f'{kind}_{label}.csv' for kind in ('equity', 'trades', 'stops') for label in ('pure', 'hybrid')]
        if args.csv and args.csv.resolve() in [(args.output/name).resolve() for name in names]:
            raise ValueError('Replay output must not overwrite the input CSV.')
        history = load_history(start, end, csv=args.csv)
        args.output.mkdir(parents=True, exist_ok=True)
        snapshot = args.output/'btc_usd_daily.csv'
        history[['Open', 'High', 'Low', 'Close']].to_csv(snapshot)
        results = []
        for hybrid, label in ((False, 'pure'), (True, 'hybrid')):
            result, equity, trades, stops = simulate(history, start, end, hybrid=hybrid, config=config)
            results.append(result)
            for kind, data in (('equity', equity), ('trades', trades), ('stops', stops)):
                data.to_csv(args.output/f'{kind}_{label}.csv', index=False)
        summary = dict(start_utc=start.isoformat(), end_exclusive_utc=end.isoformat(),
                       symbol='BTC-USD', source=str(args.csv) if args.csv else 'Yahoo Finance via yfinance',
                       history_sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                       code_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (
                           Path(__file__), Path(__file__).with_name('backtest_hybrid_grid.py'))},
                       config={key: str(value) for key, value in asdict(config).items()},
                       pandas_version=pd.__version__, yfinance_version=yf.__version__,
                       model_notes=__doc__, results=results)
        (args.output/'summary.json').write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
        print(f'BTC-USD | {start.date()} to {end.date()} (exclusive) | {results[0]["daily_candles"]} daily candles')
        print(f'{config.stop*100}% portfolio-equity stop triggers:')
        for result in results:
            print(f'  {result["scenario"]}: {result["hard_stop_triggers"]}')
        print(pd.DataFrame([{
            'Scenario': r['scenario'], 'Final Equity (USD)': r['final_portfolio_value'],
            'Max Closing DD (%)': r['max_closing_drawdown_pct'],
            'Completed Exits': r['completed_liquidations'], 'Pending Exits': r['pending_end_liquidations'],
        } for r in results]).to_string(index=False, float_format=lambda value: f'{value:,.4f}'))
        print('\nDaily-close detection; next-open liquidation. Fees/gaps can exceed the 15% trigger.')
        print('Resets retain remaining capital; no deposits. Intraday stop breaches are not measured.')
        print('This differs from the live fixed grid-price stop and does not prove future safety.')
        print(f'Reports: {args.output.resolve()}')
        return 0
    except Exception as error:
        parser.exit(1, f'Stop study failed: {type(error).__name__}: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
