#!/usr/bin/env python3
"""Offline rolling selection with continuous OOS accounts and protected cap latches.

No application, environment, credentials, live database or exchange clients are
loaded. Parameters selected on trailing training data apply only at the next
permitted grid installation; no test boundary clears inventory, stops or latches.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stderr
from dataclasses import asdict, dataclass, replace
from decimal import Decimal as D
import hashlib
import itertools
import json
from pathlib import Path

import pandas as pd
from backtest_grid import Config, GridBacktest, read_candles, read_buy_signal_candles
from budget_policy import CAP_EXHAUSTED


@dataclass(frozen=True)
class Parameters:
    atr_multiple: D = D('2')
    min_grid_return: D = D('.008')

    def __post_init__(self):
        if (not self.atr_multiple.is_finite() or self.atr_multiple <= 0 or
                not self.min_grid_return.is_finite() or not D('.008') <= self.min_grid_return < 1):
            raise ValueError('ATR must be positive and grid gross spacing at least 0.8%.')

    def json(self):
        return {key:str(value) for key,value in asdict(self).items()}


@dataclass(frozen=True)
class Fold:
    number: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp

    def json(self):
        return {key:value.isoformat() if isinstance(value,pd.Timestamp) else value
                for key,value in asdict(self).items()}


def utc(value):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError('Dates must explicitly include a timezone.')
    stamp = stamp.tz_convert('UTC')
    if stamp != stamp.floor('5min'):
        raise ValueError('Dates must align to closed 5m bar boundaries.')
    return stamp


def rolling_folds(start,end,train_months=6,test_months=3):
    start,end = utc(start),utc(end)
    if (isinstance(train_months,bool) or isinstance(test_months,bool) or
            not isinstance(train_months,int) or not isinstance(test_months,int) or
            min(train_months,test_months)<=0 or start>=end):
        raise ValueError('Require positive training/test months and start before end.')
    result = []
    index = 0
    while True:
        train_start = start+pd.DateOffset(months=index*test_months)
        test_start = start+pd.DateOffset(months=train_months+index*test_months)
        test_end = start+pd.DateOffset(months=train_months+(index+1)*test_months)
        if test_end>end:
            break
        result.append(Fold(index+1,train_start,test_start,test_start,test_end))
        index += 1
    if not result:
        raise ValueError('Insufficient data window for one full train/test fold.')
    return result


class DailyCurve(list):
    """Storage only; parent mark() still computes every drawdown/accounting step."""
    def append(self,mark):
        if self and self[-1]['timestamp'][:10]==mark['timestamp'][:10]:
            self[-1] = mark
        else:
            super().append(mark)


class ValidatedBacktest(GridBacktest):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.curve = DailyCurve()
        self.maximum_committed_cost = D(0)
        self.cap_checks = 0

    def mark(self,at,price,event='mark'):
        total = self.position_cost()+self.buy_commitment()
        self.maximum_committed_cost = max(self.maximum_committed_cost,total)
        self.cap_checks += 1
        if total>self.allocation+D('.000001') or self.free_quote() < -D('1e-18'):
            raise AssertionError('Allocation cap or available cash invariant failed.')
        super().mark(at,price,event)


class RollingAccount(ValidatedBacktest):
    """Select now, install later; a new window cannot force an order or recovery."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.pending_parameters = Parameters(self.c.atr_multiple,self.c.min_grid_return)
        self.parameter_installations = []

    def select_parameters(self,parameters):
        self.pending_parameters = parameters

    def _pending_config(self):
        return replace(self.c,**asdict(self.pending_parameters))

    def _record_installation(self,at):
        self.parameter_installations.append(dict(timestamp=pd.Timestamp(at).isoformat(),
            parameters=Parameters(self.c.atr_multiple,self.c.min_grid_return).json()))

    def start_grid(self,at,center,atr):
        self.c = self._pending_config()
        super().start_grid(at,center,atr)
        self._record_installation(at)

    def _breakout(self,price,at):
        if self.mode!='RUNNING':
            return super()._breakout(price,at)
        previous,count = self.c,self.breakout_count
        self.c = self._pending_config()
        try:
            super()._breakout(price,at)
        finally:
            if self.breakout_count==count:
                self.c = previous
        if self.breakout_count>count:
            self._record_installation(at)


def window_inputs(frames,start,end):
    """Training cannot even inspect bars at/after its selection boundary."""
    fine = frames['execution']
    warmup = start-pd.Timedelta(hours=50)
    fine = fine.loc[(fine.index>=warmup)&(fine.index<end)].copy()
    expected = pd.date_range(warmup,end-pd.Timedelta(minutes=5),freq='5min')
    if not fine.index.equals(expected):
        raise ValueError('Incomplete execution window/warm-up; do not shift dates or fill gaps.')
    daily_start = start.normalize()-pd.Timedelta(days=200)
    daily_end = end.normalize()
    daily = {key:frames[key].loc[(frames[key].index>=daily_start)&
                                (frames[key].index<daily_end)].copy()
             for key in ('binance_daily','yahoo_daily')}
    return dict(execution=fine,**daily)


def run_account(account,frames,start,end,folder):
    folder.mkdir(parents=True,exist_ok=False)
    inputs = window_inputs(frames,start,end)
    with (folder/'progress.log').open('w') as log,redirect_stderr(log):
        metrics = account.run(inputs['execution'],start,end,
                              trend_frame=inputs['binance_daily'],buy_signal_frame=inputs['yahoo_daily'])
    (folder/'metrics.json').write_text(json.dumps(metrics,indent=2)+'\n')
    return metrics


def training_case(task):
    base,parameters,fold,frames,folder = task
    config = replace(base,**asdict(parameters))
    paths = []
    for route in ('ohlc','olhc'):
        sim = ValidatedBacktest(config,route)
        metrics = run_account(sim,frames,fold.train_start,fold.train_end,folder/route)
        pd.DataFrame(sim.fills).to_csv(folder/route/'fills.csv',index=False)
        pd.DataFrame(sim.events).to_csv(folder/route/'events.csv',index=False)
        paths.append(dict(path=route,metrics=metrics,cap_checks=sim.cap_checks,
                          maximum_committed_cost=str(sim.maximum_committed_cost)))
    return dict(parameters=parameters.json(),paths=paths,
                latest_execution_input_exclusive=fold.train_end.isoformat())


def select_training(rows,min_sell_fills=3):
    """Worst-path (net return - drawdown); ties prefer fewer cap bars, then baseline."""
    scored = []
    for row in rows:
        paths = row['paths']
        eligible = all(p['metrics']['sell_fills']>=min_sell_fills for p in paths)
        score = min(p['metrics']['total_return_pct']-p['metrics']['max_drawdown_pct'] for p in paths)
        cap_fraction = max(p['metrics']['state_bar_counts'].get(CAP_EXHAUSTED,0)/
                           sum(p['metrics']['state_bar_counts'].values()) for p in paths)
        scored.append(dict(row,eligible=eligible,score=score,cap_bar_fraction=cap_fraction))
    eligible = [r for r in scored if r['eligible']]
    baseline = Parameters().json()
    if not any(r['parameters']==baseline for r in scored):
        raise ValueError('Candidate grid must include the fixed 2 ATR / 0.8% baseline.')
    if not eligible:
        return Parameters(),scored,'No candidate met minimum training SELL fills; retain baseline.'
    def key(row):
        p = row['parameters']
        return (row['score'],-row['cap_bar_fraction'],p==baseline,
                -D(p['atr_multiple']),-D(p['min_grid_return']))
    selected = max(eligible,key=key)['parameters']
    return Parameters(**{k:D(v) for k,v in selected.items()}),scored,'Selected on training data only.'


def write_account(account,folder):
    folder.mkdir(parents=True,exist_ok=True)
    for name,rows in [('fills',account.fills),('events',account.events),('daily-equity',account.curve)]:
        pd.DataFrame(rows).to_csv(folder/f'{name}.csv',index=False)
    if isinstance(account,RollingAccount):
        (folder/'parameter-installations.json').write_text(json.dumps(account.parameter_installations,indent=2)+'\n')


def run_pipeline(frames,folds,candidates,output,*,base=None,workers=1,min_sell_fills=3,stress_slippage=None):
    base = base or Config(levels=5,replacement_buy_policy='residual')
    if (base.replacement_buy_policy!='residual' or base.allocation_policy!='total_cost_cap'
            or base.restart_after_stop or base.stop_mode!='fixed' or not base.trend_filter
            or base.buy_trend_policy!='production'):
        raise ValueError('WFO requires residual latch, strict cap, both macro gates, fixed stop and manual recovery.')
    if workers<1 or min_sell_fills<1 or not candidates or len(set(candidates))!=len(candidates):
        raise ValueError('Require positive workers/fill threshold and unique candidates.')
    output.mkdir(parents=True,exist_ok=False)
    accounts = {route:RollingAccount(base,route) for route in ('ohlc','olhc')}
    references = {route:ValidatedBacktest(base,route) for route in ('ohlc','olhc')}
    groups = dict(selected=accounts,fixed_reference=references)
    if stress_slippage is not None:
        stress = replace(base,slippage_bps=stress_slippage)
        groups['selected_stress'] = {route:RollingAccount(stress,route) for route in ('ohlc','olhc')}
        groups['fixed_reference_stress'] = {route:ValidatedBacktest(stress,route) for route in ('ohlc','olhc')}
    report = dict(complete=False,folds=[],oos_start=folds[0].test_start.isoformat(),
                  oos_end=folds[-1].test_end.isoformat(),
                  continuity='Same account, cash, inventory, cost basis, orders, stop and cap latch across OOS folds. '
                  'No forced re-anchor/restart or budget reset at a boundary. New parameters await a permitted installation.')
    pool = ProcessPoolExecutor(max_workers=workers) if workers>1 else None
    try:
        for index,fold in enumerate(folds):
            if fold.train_end!=fold.test_start or (index and fold.test_start!=folds[index-1].test_end):
                raise ValueError('Require chronological, adjacent, non-overlapping OOS folds.')
            training = window_inputs(frames,fold.train_start,fold.train_end)
            tasks = [(base,p,fold,training,output/'training'/f'fold-{fold.number}'/f'candidate-{i+1}')
                     for i,p in enumerate(candidates)]
            rows = list(pool.map(training_case,tasks)) if pool else [training_case(t) for t in tasks]
            selected,scored,reason = select_training(rows,min_sell_fills)
            record = dict(fold=fold.json(),selected_parameters=selected.json(),selection_reason=reason,training=scored,oos=[])
            # Persist the training decision before examining the OOS simulation.
            decision = output/f'fold-{fold.number}-selection.json'
            decision.write_text(json.dumps(record,indent=2)+'\n')
            for route in ('ohlc','olhc'):
                for label,group in groups.items():
                    account = group[route]
                    if isinstance(account,RollingAccount):
                        account.select_parameters(selected)
                    before = dict(cash=str(account.cash),btc=str(account.held),mode=account.mode,
                                  cost=str(account.position_cost()),stop=str(account.stop),orders=len(account.orders))
                    metrics = run_account(account,frames,fold.test_start,fold.test_end,
                                          output/'oos'/label/route/f'fold-{fold.number}')
                    write_account(account,output/'oos'/label/route)
                    record['oos'].append(dict(policy=label,path=route,before=before,
                        cumulative_metrics=metrics,cap_checks=account.cap_checks,
                        slippage_bps=str(account.c.slippage_bps),
                        maximum_committed_cost=str(account.maximum_committed_cost)))
            report['folds'].append(record)
            (output/'walk-forward.json').write_text(json.dumps(report,indent=2)+'\n')
            print(f'Fold {fold.number}: selected {selected.json()}; OOS accounts retained.',flush=True)
        report['complete'] = True
        report['final'] = report['folds'][-1]['oos']
        (output/'walk-forward.json').write_text(json.dumps(report,indent=2)+'\n')
        return report
    finally:
        if pool:
            pool.shutdown(wait=True,cancel_futures=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('csv','trend-csv','buy-signal-csv','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    for name in ('start','end'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--train-months',type=int,default=6)
    parser.add_argument('--test-months',type=int,default=3)
    parser.add_argument('--atr-multiples',default='1.5,2,3')
    parser.add_argument('--spacing-percent',default='0.8,1.2')
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--min-training-sells',type=int,default=3)
    parser.add_argument('--slippage-bps',type=D,default=D(10))
    parser.add_argument('--stress-slippage-bps',type=D,default=D(25),help='OOS shadow accounts only; never used to select parameters')
    args = parser.parse_args(argv)
    start,end = utc(args.start),utc(args.end)
    if end>pd.Timestamp.now(tz='UTC').floor('5min'):
        raise ValueError('Future or incomplete execution bars are not allowed.')
    folds = rolling_folds(start,end,args.train_months,args.test_months)
    candidates = [Parameters(D(a),D(s)/100) for a,s in itertools.product(
        args.atr_multiples.split(','),args.spacing_percent.split(','))]
    if Parameters() not in candidates:
        raise ValueError('Include ATR 2 and spacing 0.8% as the baseline.')
    frames = dict(execution=read_candles(args.csv),binance_daily=read_candles(args.trend_csv,'1d'),
                  yahoo_daily=read_buy_signal_candles(args.buy_signal_csv))
    sources = [args.csv,args.trend_csv,args.buy_signal_csv,Path(__file__),
               Path(__file__).with_name('backtest_grid.py'),Path(__file__).with_name('grid_geometry.py'),
               Path(__file__).with_name('budget_policy.py')]
    base = Config(levels=5,replacement_buy_policy='residual',slippage_bps=args.slippage_bps)
    replace(base,slippage_bps=args.stress_slippage_bps)  # Validate before writing the manifest.
    manifest = dict(protocol='6m/3m rolling selection by default; already inspected history must be labeled retrospective.',
        source_sha256={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        folds=[f.json() for f in folds],candidates=[p.json() for p in candidates],
        min_training_sell_fills=args.min_training_sells,
        objective='Worst-path net return minus max drawdown; ties prefer fewer CAP_EXHAUSTED bars, then baseline.',
        config={k:str(v) if isinstance(v,D) else v for k,v in asdict(base).items()},
        stress_slippage_bps=str(args.stress_slippage_bps),
        excluded_tail_start=folds[-1].test_end.isoformat() if folds[-1].test_end<end else None,
        no_automatic_live_parameter_changes=True)
    # No result data is examined before this protocol/source record is written.
    args.output.parent.mkdir(parents=True,exist_ok=True)
    manifest_path = args.output.with_name(args.output.name+'-manifest.json')
    if manifest_path.exists() or args.output.exists():
        raise ValueError('Choose a fresh output path; earlier evidence is preserved.')
    manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
    report = run_pipeline(frames,folds,candidates,args.output,base=base,workers=args.workers,
                          min_sell_fills=args.min_training_sells,stress_slippage=args.stress_slippage_bps)
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==expected
               for p,expected in manifest['source_sha256'].items()), 'Sources changed during validation.'
    print(json.dumps([dict(policy=r['policy'],path=r['path'],
                          return_pct=r['cumulative_metrics']['total_return_pct'],
                          max_drawdown_pct=r['cumulative_metrics']['max_drawdown_pct']) for r in report['final']],indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
