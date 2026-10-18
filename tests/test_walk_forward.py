"""Temporal isolation, risk-state continuity and conservative rolling selection."""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from backtest_grid import Config
from budget_policy import CAP_EXHAUSTED
from walk_forward import (Fold,Parameters,RollingAccount,rolling_folds,select_training,
                          window_inputs,run_pipeline)
from tests.test_backtest_grid import START


class WalkForwardTests(unittest.TestCase):
    def test_month_end_windows_have_no_oos_gaps_or_overlap(self):
        folds = rolling_folds('2023-01-31T00:00:00Z','2024-01-31T00:00:00Z')
        self.assertEqual(len(folds),2)
        self.assertEqual(folds[0].train_end,folds[0].test_start)
        self.assertEqual(folds[0].test_end,folds[1].test_start)
        self.assertEqual(folds[1].test_end,pd.Timestamp('2024-01-31T00:00:00Z'))
        with self.assertRaises(ValueError):
            rolling_folds('2024-01-01','2025-01-01')
        with self.assertRaises(ValueError):
            rolling_folds('2024-01-01T00:00Z','2024-06-01T00:00Z')

    def frames(self):
        fine = pd.DataFrame({'open':100.,'high':101.,'low':99.,'close':100.,'volume':1.},
                            index=pd.date_range(START-pd.Timedelta(hours=50),START+pd.Timedelta(hours=2),freq='5min'))
        daily = pd.DataFrame({'open':50.,'high':51.,'low':49.,'close':50.,'volume':1.},
                             index=pd.date_range(START.normalize()-pd.Timedelta(days=210),START.normalize()+pd.Timedelta(days=2),freq='D'))
        return dict(execution=fine,binance_daily=daily.copy(),yahoo_daily=daily.copy())

    def test_training_inputs_exclude_boundary_and_all_future_prices(self):
        frames = self.frames()
        end = START+pd.Timedelta(hours=1)
        original = window_inputs(frames,START,end)
        for frame in frames.values():
            frame.loc[frame.index>=end,'close'] = 999999.
        changed = window_inputs(frames,START,end)
        for name in original:
            pd.testing.assert_frame_equal(original[name],changed[name])
            self.assertTrue((changed[name].index<end).all())
        frames['execution'] = frames['execution'].drop(START-pd.Timedelta(minutes=5))
        with self.assertRaisesRegex(ValueError,'Incomplete execution'):
            window_inputs(frames,START,end)

    def row(self,parameters,returns,drawdowns,sells=5):
        return dict(parameters=parameters.json(),paths=[dict(path=path,metrics=dict(
            sell_fills=sells,total_return_pct=ret,max_drawdown_pct=dd,
            state_bar_counts={'RUNNING':100})) for path,ret,dd in zip(('ohlc','olhc'),returns,drawdowns)])

    def test_selection_uses_worst_training_path_and_does_not_reward_no_trade_cash(self):
        base = self.row(Parameters(),[4,3],[1,1])
        wider = self.row(Parameters(D(3),D('.012')),[10,-1],[1,3])
        idle = self.row(Parameters(D('1.5'),D('.008')),[0,0],[0,0],sells=0)
        selected,scored,_ = select_training([base,wider,idle])
        self.assertEqual(selected,Parameters())
        self.assertFalse(scored[-1]['eligible'])
        idle_base = self.row(Parameters(),[0,0],[0,0],sells=0)
        selected,_,reason = select_training([idle_base,idle])
        self.assertEqual(selected,Parameters())
        self.assertIn('retain baseline',reason)

    def test_parameter_change_never_releases_cap_or_moves_existing_grid(self):
        sim = RollingAccount(Config(levels=5,trend_filter=False,replacement_buy_policy='residual'))
        sim.start_grid(START,D(80000),D(2000))
        sim.cash,sim.realized = D(300),D(200)
        next(iter(sim.lots.values())).cost = D(400)
        sim.orders.clear()
        sim._maintain(sim.anchor,START)
        before = sim.cash,sim.held,sim.position_cost(),sim.stop,dict(sim.orders),sim.c
        sim.select_parameters(Parameters(D(3),D('.012')))
        sim._point(D(90000),START+pd.Timedelta(hours=5))
        # SELLs may legitimately fill only via a segment, never just selection.
        self.assertEqual((sim.cash,sim.held,sim.position_cost(),sim.stop,dict(sim.orders),sim.c),before)
        self.assertEqual(sim.mode,CAP_EXHAUSTED)
        self.assertEqual(sim.breakout_count,0)

    def test_new_parameters_wait_for_a_permitted_installation(self):
        sim = RollingAccount(Config(levels=5,trend_filter=False,replacement_buy_policy='residual'))
        sim.select_parameters(Parameters(D(3),D('.012')))
        self.assertEqual(sim.c.atr_multiple,D(2))
        sim.start_grid(START,D(80000),D(2000))
        self.assertEqual(sim.c.atr_multiple,D(3))
        self.assertEqual(sim.c.min_grid_return,D('.012'))
        self.assertEqual(len(sim.parameter_installations),1)

    def test_pipeline_cannot_select_rejected_buy_policy_or_disable_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            for config in (Config(levels=5,replacement_buy_policy='budget_aware'),
                           Config(levels=5,replacement_buy_policy='residual',trend_filter=False),
                           Config(levels=5,replacement_buy_policy='residual',stop_mode='none')):
                with self.assertRaisesRegex(ValueError,'WFO requires'):
                    run_pipeline({},[],[Parameters()],Path(directory)/'out',base=config)

    def test_adjacent_oos_runs_keep_the_same_accounts_and_prior_risk_state(self):
        frames = self.frames()
        folds = [Fold(1,START-pd.Timedelta(minutes=5),START,START,START+pd.Timedelta(minutes=5)),
                 Fold(2,START,START+pd.Timedelta(minutes=5),START+pd.Timedelta(minutes=5),START+pd.Timedelta(minutes=10))]
        calls = {}
        def fake_training(task):
            return self.row(task[1],[1,1],[0,0])
        def fake_run(account,inputs,start,end,folder):
            ident = id(account)
            calls[ident] = calls.get(ident,0)+1
            if calls[ident]==1:
                account.cash = D(417)
                account.mode = CAP_EXHAUSTED
            else:
                self.assertEqual(account.cash,D(417))
                self.assertEqual(account.mode,CAP_EXHAUSTED)
            return {'ending_equity':417,'final_state':account.mode}
        with tempfile.TemporaryDirectory() as directory,patch('walk_forward.window_inputs',return_value=frames),\
                patch('walk_forward.training_case',side_effect=fake_training),\
                patch('walk_forward.run_account',side_effect=fake_run):
            result = run_pipeline(frames,folds,[Parameters()],Path(directory)/'out')
        self.assertTrue(result['complete'])
        self.assertEqual(len(calls),4)  # selected/reference accounts on both paths
        self.assertEqual(set(calls.values()),{2})


if __name__=='__main__':
    unittest.main()
