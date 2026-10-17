"""Deterministic checks of equity-stop timing, resets, macro gating and fees."""
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from backtest_hybrid_grid import load_history
from test_stop_loss import StopConfig, main, simulate

START = pd.Timestamp('2021-01-01T00:00:00Z')
NO_FEES = StopConfig(capital=D(1000), fee=D(0))


def history(rows):
    source = pd.DataFrame([[100, 100, 100, 100]]*200+rows,
                          columns=['Open', 'High', 'Low', 'Close'],
                          index=pd.date_range(START-pd.Timedelta(days=200), periods=200+len(rows), freq='D'))
    end = START+pd.Timedelta(days=len(rows))
    return load_history(START, end, download=lambda *a, **k: source), end


def run(rows, *, config=NO_FEES, hybrid=False):
    data, end = history(rows)
    return simulate(data, START, end, config=config, hybrid=hybrid)


class StopLossSimulationTests(unittest.TestCase):
    def test_exact_threshold_queues_once_and_exits_next_open_before_sell_limits(self):
        rows = [[100, 100, 90, 90], [90, 90, 90, 90],
                [90, 90, 76.5, 76.5], [70, 200, 70, 90]]
        result, curve, trades, stops = run(rows)
        self.assertEqual(result['hard_stop_triggers'], 1)
        self.assertEqual(result['completed_liquidations'], 1)
        self.assertEqual(result['grid_sell_executions'], 0)
        self.assertEqual(stops.iloc[0].trigger_equity, 850)
        self.assertEqual(stops.iloc[0].threshold_equity, 850)
        self.assertEqual(stops.iloc[0].exit_price, 70)
        self.assertTrue(curve.iloc[2].stop_pending)
        self.assertEqual(curve.iloc[3].btc, 0)
        self.assertGreater(stops.iloc[0].loss_from_campaign_peak_pct, 15)
        exits = trades[trades.reason == 'hard_stop']
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits.iloc[0].timestamp, (START+pd.Timedelta(days=3)).isoformat())
        self.assertFalse(((trades.side == 'BUY') & (trades.timestamp == exits.iloc[0].timestamp)).any())

    def test_stop_on_last_candle_is_counted_but_not_filled_with_future_price(self):
        result, _, trades, stops = run([[100, 100, 90, 90], [90, 90, 90, 90], [90, 90, 76.5, 76.5]])
        self.assertEqual(result['hard_stop_triggers'], 1)
        self.assertEqual(result['completed_liquidations'], 0)
        self.assertEqual(result['pending_end_liquidations'], 1)
        self.assertEqual(result['unexecuted_end_buy_signals'], 0)
        self.assertFalse((trades.reason == 'hard_stop').any())
        self.assertTrue(pd.isna(stops.iloc[0].exit_timestamp))
        self.assertGreater(result['held_btc'], 0)

    def test_reset_retains_losses_and_global_peak_and_never_refills_capital(self):
        rows = [[100, 100, 90, 90], [90, 90, 90, 90], [90, 90, 76.5, 76.5],
                [70, 70, 70, 70], [70, 70, 60, 60], [60, 60, 20, 20], [10, 10, 10, 10],
                [10, 10, 5, 5], [5, 5, 5, 5]]
        result, curve, _, stops = run(rows)
        self.assertEqual(result['hard_stop_triggers'], 2)
        self.assertEqual(result['completed_liquidations'], 2)
        self.assertAlmostEqual(curve.iloc[3].campaign_peak, 1000*70/90)
        self.assertEqual(curve.iloc[3].global_peak, 1000)
        self.assertLess(stops.iloc[1].campaign_peak, 1000)
        self.assertGreater(result['unfunded_buy_signals'], 0)
        self.assertLess(result['remaining_usdt'], 500)
        self.assertEqual(result['held_btc'], 0)
        self.assertAlmostEqual(result['final_portfolio_value'], 1000+result['realized_pnl'])
        self.assertGreater(result['max_closing_drawdown_pct'], 60)
        self.assertTrue((curve.cash >= 0).all())

    def test_stop_uses_total_portfolio_equity_including_cash(self):
        result, _, _, stops = run([[100, 100, 90, 90], [90, 90, 90, 90], [90, 90, 45, 45]],
                                  config=StopConfig(capital=D(10000), fee=D(0)))
        self.assertEqual(result['final_portfolio_value'], 9500)
        self.assertEqual(result['hard_stop_triggers'], 0)
        self.assertTrue(stops.empty)

    def test_flat_portfolio_does_not_trigger_and_warmup_is_not_traded(self):
        result, _, trades, stops = run([[100, 100, 100, 100]]*3)
        self.assertEqual(result['daily_candles'], 3)
        self.assertEqual(result['final_portfolio_value'], 1000)
        self.assertEqual(result['hard_stop_triggers'], 0)
        self.assertTrue(trades.empty and stops.empty)

    def test_hybrid_blocks_equal_below_sma_and_still_sells_existing_inventory(self):
        result, _, trades, _ = run([[110, 110, 100, 100], [100, 100, 100, 100]], hybrid=True)
        self.assertEqual(result['buy_executions'], 0)
        self.assertGreater(result['filtered_buy_signals'], 0)
        result, _, trades, _ = run([[120, 120, 110, 110], [110, 110, 110, 110],
                                     [95, 116, 90, 95]], hybrid=True)
        self.assertEqual(result['buy_executions'], result['grid_sell_executions'])
        self.assertGreater(result['grid_sell_executions'], 0)
        self.assertEqual(result['held_btc'], 0)

    def test_pending_hybrid_buy_is_blocked_by_gap_below_previous_sma(self):
        result, _, trades, _ = run([[120, 120, 110, 110], [90, 90, 90, 90]], hybrid=True)
        self.assertEqual(result['buy_executions'], 0)
        self.assertTrue(trades.empty)

    def test_restart_is_gated_by_macro_not_only_initial_deployment(self):
        rows = [[120, 120, 110, 110], [110, 110, 70, 70], [70, 70, 70, 70],
                [70, 70, 60, 60], [60, 60, 60, 60]]
        result, _, trades, _ = run(rows, hybrid=True)
        self.assertEqual(result['completed_liquidations'], 1)
        first_buy_day = (START+pd.Timedelta(days=1)).isoformat()
        self.assertTrue((trades.loc[trades.side == 'BUY', 'timestamp'] == first_buy_day).all())
        self.assertGreater(result['filtered_buy_signals'], 0)

    def test_buy_and_exit_fees_and_open_cost_accounting(self):
        result, curve, trades, stops = run([[100, 100, 90, 90], [90, 90, 90, 90],
                                           [90, 90, 70, 70], [70, 70, 70, 70]],
                                          config=StopConfig(capital=D(1000)))
        self.assertEqual(result['completed_liquidations'], 1)
        self.assertAlmostEqual(result['fees_usdt'], trades.fee.sum())
        self.assertGreater(stops.iloc[0].exit_fee, 0)
        self.assertAlmostEqual(result['final_portfolio_value'], 1000+trades.realized_pnl.sum())
        self.assertTrue((abs(curve.equity-curve.cash-curve.btc*curve.close) < 1e-8).all())
        self.assertTrue((abs(curve.equity-1000-curve.realized_pnl-curve.unrealized_pnl) < 1e-8).all())

    def test_daily_closing_model_does_not_claim_intraday_stop_measurement(self):
        result, _, _, _ = run([[100, 100, 90, 90], [90, 90, 90, 90], [90, 90, 1, 90]])
        self.assertEqual(result['hard_stop_triggers'], 0)

    def test_future_data_cannot_change_earlier_signals_or_fills(self):
        rows = [[100, 100, 90, 90], [90, 90, 90, 90], [90, 90, 76.5, 76.5], [70, 70, 70, 70]]
        _, curve, trades, _ = run(rows)
        rows[-1] = [200, 200, 200, 200]
        _, changed, changed_trades, _ = run(rows)
        pd.testing.assert_frame_equal(curve.iloc[:3], changed.iloc[:3])
        cutoff = (START+pd.Timedelta(days=3)).isoformat()
        pd.testing.assert_frame_equal(trades[trades.timestamp < cutoff],
                                      changed_trades[changed_trades.timestamp < cutoff])

    def test_saved_csv_replay_preserves_exact_results_without_network(self):
        rows = [[100, 100, 90, 90], [90, 90, 90, 90], [90, 90, 76.5, 76.5], [70, 70, 70, 70]]
        data, end = history(rows)
        expected = simulate(data, START, end)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'history.csv'
            data[['Open', 'High', 'Low', 'Close']].to_csv(path)
            with patch('backtest_hybrid_grid.yf.download', side_effect=AssertionError('No network')):
                replay = simulate(load_history(START, end, csv=path), START, end)
        self.assertEqual(expected[0], replay[0])
        for left, right in zip(expected[1:], replay[1:]):
            pd.testing.assert_frame_equal(left, right)

    def test_invalid_stop_and_missing_warmup_rejected(self):
        for stop in (D(0), D(1), D('NaN'), D('Infinity')):
            with self.subTest(stop=stop), self.assertRaises(ValueError):
                StopConfig(stop=stop)
        data, end = history([[100, 100, 100, 100]])
        data.loc[START, 'PreviousSMA200'] = float('nan')
        with self.assertRaises(ValueError):
            simulate(data, START, end)

    def test_replay_output_cannot_overwrite_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'btc_usd_daily.csv'
            path.write_text('preserve', encoding='utf-8')
            with self.assertRaises(SystemExit), patch('sys.stderr'):
                main(['--csv', str(path), '--output', directory])
            self.assertEqual(path.read_text(encoding='utf-8'), 'preserve')


if __name__ == '__main__':
    unittest.main()
