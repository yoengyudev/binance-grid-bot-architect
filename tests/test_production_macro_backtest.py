"""Production Yahoo BUY gating with independent Binance entry signals."""
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal as D
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from backtest_grid import (Config, GridBacktest, main, prepare_buy_signals,
                           read_buy_signal_candles)
from tests.test_backtest_grid import START, fine_candles
from tests.test_daily_macro_backtest import daily_history


def yahoo_history():
    daily = daily_history(extra_days=4)
    # Yesterday's positive close approves the first day's BUYs.
    daily.loc[START-pd.Timedelta(days=1), ['high','close']] = [101,101]
    return daily


class ProductionMacroBacktestTests(unittest.TestCase):
    def bot(self):
        bot = GridBacktest(Config(levels=5))
        bot.macro_sma = D(99)
        bot.buy_macro_close, bot.buy_macro_sma = D(101), D(100)
        bot.start_grid(START,D(100),D(1))
        return bot

    def test_default_policy_is_production_and_legacy_is_explicit(self):
        self.assertEqual(Config().buy_trend_policy,'production')
        with self.assertRaises(ValueError):
            Config(buy_trend_policy='guess')

    def test_negative_yahoo_signal_cancels_only_buys_preserves_cost_and_stop(self):
        bot = self.bot()
        before = (bot.cash,bot.held,bot.stop,sum(lot.cost for lot in bot.lots.values()))
        sells = {k:o for k,o in bot.orders.items() if o.side=='SELL'}
        bot.buy_macro_close = D(99)
        bot._point(D(100),START)
        self.assertEqual(bot.orders,sells)
        self.assertEqual((bot.cash,bot.held,bot.stop,sum(lot.cost for lot in bot.lots.values())),before)
        self.assertTrue(bot.buy_paused)
        self.assertEqual(bot.sma_canceled_buys,3)

    def test_equal_or_missing_yahoo_signal_cannot_seed(self):
        for close,sma in ((D(100),D(100)),(None,D(100)),(D(100),None)):
            bot=GridBacktest(Config())
            bot.buy_macro_close,bot.buy_macro_sma=close,sma
            with self.assertRaisesRegex(ValueError,'Yahoo'):
                bot.start_grid(START,D(100),D(1))
            self.assertEqual(bot.fills,[])

    def test_sell_fills_continue_while_buy_signal_negative(self):
        bot=self.bot()
        bot.buy_macro_close=D(99)
        held=bot.held
        bot._segment(D(100),D(103),START,START+pd.Timedelta(minutes=5))
        self.assertLess(bot.held,held)
        self.assertTrue(any(f['side']=='SELL' for f in bot.fills))
        self.assertTrue(all(o.side=='SELL' for o in bot.orders.values()))

    def test_yahoo_approval_restores_buys_even_if_binance_price_below_entry_sma(self):
        bot=self.bot()
        bot.buy_macro_close=D(99)
        bot._point(D(100),START)
        bot.buy_macro_close=D(101)
        bot.macro_sma=D(110)  # Existing grids use Yahoo for replacement BUYs.
        bot._point(D(100),START+pd.Timedelta(minutes=5))
        self.assertFalse(bot.buy_paused)
        self.assertEqual(sum(o.side=='BUY' for o in bot.orders.values()),3)

    def test_macro_pause_resets_breakout_timer_and_requires_new_four_hours(self):
        bot=self.bot()
        above=bot.upper+D(1)
        bot._point(above,START)
        self.assertEqual(bot.above_since,START)
        bot.buy_macro_close=D(99)
        bot._point(above,START+pd.Timedelta(hours=1))
        self.assertIsNone(bot.above_since)
        bot.buy_macro_close=D(101)
        resumed=START+pd.Timedelta(days=1)
        bot._point(above,resumed)
        self.assertEqual(bot.above_since,resumed)
        self.assertEqual(bot.breakout_count,0)
        bot._point(above,resumed+pd.Timedelta(hours=4))
        self.assertEqual(bot.breakout_count,1)

    def test_strict_stop_still_runs_while_buys_paused_and_clears_pause_flag(self):
        bot=self.bot()
        stop=bot.stop
        bot.buy_macro_close=D(99)
        bot._point(stop,START)
        self.assertEqual(bot.stop_count,0)
        bot._point(stop-D('.01'),START+pd.Timedelta(minutes=1))
        self.assertEqual(bot.stop_count,1)
        self.assertFalse(bot.buy_paused)
        self.assertTrue(bot.mode.startswith('HALTED'))

    def test_production_buy_gate_applies_to_explicit_no_stop_mode_too(self):
        bot=GridBacktest(Config(stop_mode='none'))
        bot.buy_macro_close,bot.buy_macro_sma=D(101),D(100)
        bot.start_grid(START,D(100),D(1))
        bot.buy_macro_close=D(99)
        bot._point(D(100),START)
        self.assertTrue(bot.buy_paused)
        self.assertTrue(all(o.side=='SELL' for o in bot.orders.values()))
        self.assertIsNone(bot.stop)

    def test_both_independent_gates_required_for_initial_entry(self):
        bot=GridBacktest(Config())
        bot.buy_macro_close,bot.buy_macro_sma=D(101),D(100)
        bot.macro_sma=D(110)
        bot._restart(START,D(100),D(1))
        self.assertEqual(bot.fills,[])
        bot.macro_sma=D(99)
        bot.buy_macro_close=D(99)
        bot._restart(START,D(100),D(1))
        self.assertEqual(bot.fills,[])
        bot.buy_macro_close=D(101)
        bot._restart(START,D(100),D(1))
        self.assertEqual(len(bot.fills),1)

    def test_current_daily_candle_does_not_change_preceding_signals(self):
        frame=yahoo_history()
        original=prepare_buy_signals(frame)
        changed=frame.copy()
        changed.loc[START,['high','close']]=[200,200]
        updated=prepare_buy_signals(changed)
        pd.testing.assert_frame_equal(original.loc[:START],updated.loc[:START])
        self.assertEqual(original.loc[START,'buy_close'],101)
        self.assertEqual(updated.loc[START+pd.Timedelta(days=1),'buy_close'],200)

    def test_missing_stale_or_short_yahoo_history_is_rejected_before_trading(self):
        frame=fine_candles([[101,101,101,101,1]])
        end=START+pd.Timedelta(minutes=5)
        for source in (None,yahoo_history().iloc[2:],yahoo_history().loc[:START-pd.Timedelta(days=2)]):
            bot=GridBacktest(Config())
            with self.assertRaises(ValueError):
                bot.run(frame,START,end,trend_frame=daily_history(),buy_signal_frame=source)
            self.assertEqual(bot.fills,[])
        with self.assertRaisesRegex(ValueError,'Missing'):
            prepare_buy_signals(yahoo_history().drop(yahoo_history().index[30]))

    def test_halted_bars_never_count_as_active_pause_or_blocked_entry(self):
        frame=fine_candles([[101,101,101,101,1]]*3)
        bot=self.bot()
        bot.buy_macro_close=D(99)
        bot._point(bot.stop-D(1),START-pd.Timedelta(seconds=1))
        prior_blocks=bot.entry_trend_blocked_bars
        report=bot.run(frame,START,START+pd.Timedelta(minutes=15),
                       trend_frame=daily_history(),buy_signal_frame=yahoo_history())
        self.assertEqual(report['buy_pause_bars'],0)
        self.assertFalse(report['final_buy_paused'])
        self.assertEqual(report['entry_trend_blocked_bars'],prior_blocks)
        self.assertEqual(report['state_bar_counts']['HALTED_DUST'],3)

    def test_offline_default_requires_yahoo_csv_without_network_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            execution,trend=root/'execution.csv',root/'trend.csv'
            fine_candles([[101,101,101,101,1]]).to_csv(execution,index_label='timestamp')
            daily_history().to_csv(trend,index_label='timestamp')
            with patch('backtest_grid.fetch_buy_signal_candles',side_effect=AssertionError('Network forbidden')),redirect_stderr(io.StringIO()) as errors:
                code=main(['--csv',str(execution),'--trend-csv',str(trend),'--start',START.isoformat(),
                           '--end',(START+pd.Timedelta(minutes=5)).isoformat(),'--output',str(root/'out')])
            self.assertEqual(code,1)
            self.assertIn('--buy-signal-csv',errors.getvalue())

    def test_offline_default_preserves_all_three_inputs_and_reports_yahoo_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            execution=root/'btc_usdt_5m.csv'
            trend=root/'btc_usdt_1d_trend.csv'
            yahoo=root/'btc_usd_1d_buy_signal.csv'
            fine_candles([[101,101,101,101,1]]*2).to_csv(execution,index_label='timestamp')
            daily_history().to_csv(trend,index_label='timestamp')
            yahoo_history().to_csv(yahoo,index_label='timestamp')
            paths=(execution,trend,yahoo)
            originals=[p.read_bytes() for p in paths]
            args=['--csv',str(execution),'--trend-csv',str(trend),'--buy-signal-csv',str(yahoo),
                  '--start',START.isoformat(),'--end',(START+pd.Timedelta(minutes=10)).isoformat(),
                  '--output',str(root)]
            with patch('backtest_grid.fetch_candles',side_effect=AssertionError('Network forbidden')),patch('backtest_grid.fetch_buy_signal_candles',side_effect=AssertionError('Network forbidden')),redirect_stdout(io.StringIO()):
                self.assertEqual(main(args),0)
            self.assertEqual([p.read_bytes() for p in paths],originals)
            summary=json.loads((root/'summary.json').read_text())
            self.assertEqual(summary['config']['buy_trend_policy'],'production')
            self.assertEqual(summary['buy_signal_file'],'buy_signal_snapshot.csv')
            self.assertTrue(summary['buy_signal_sha256'])

    def test_saved_yahoo_uppercase_csv_without_volume_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'yahoo.csv'
            frame=yahoo_history().drop(columns='volume').rename(columns=str.capitalize)
            frame.to_csv(path,index_label='timestamp')
            normalized=read_buy_signal_candles(path)
            self.assertEqual(normalized.close.iloc[-1],frame.Close.iloc[-1])
            self.assertTrue((normalized.volume==0).all())
