"""Daily toy-model timing, SMA gating, capital constraints and marked accounting."""
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from backtest_hybrid_grid import Config, load_history, simulate

START = pd.Timestamp('2021-01-01T00:00:00Z')


def source(rows):
    return pd.DataFrame([[100,100,100,100]] * 200 + rows,
                        columns=['Open','High','Low','Close'],
                        index=pd.date_range(START-pd.Timedelta(days=200),periods=200+len(rows),freq='D'))


def run(rows, *, hybrid=False, config=Config()):
    end = START+pd.Timedelta(days=len(rows))
    data = load_history(START,end,download=lambda *a,**k:source(rows))
    return simulate(data,START,end,hybrid=hybrid,config=config)


class HybridGridBacktestTests(unittest.TestCase):
    def test_warmup_is_fetched_but_never_traded(self):
        download = Mock(return_value=source([[100,100,100,100]]))
        data = load_history(START,START+pd.Timedelta(days=1),download=download)
        self.assertEqual(download.call_args.kwargs['start'],'2020-06-15')
        self.assertEqual(download.call_args.kwargs['end'],'2021-01-02')
        result, curve, trades = simulate(data,START,START+pd.Timedelta(days=1))
        self.assertEqual(result['daily_candles'],1)
        self.assertEqual(result['final_portfolio_value'],10000)
        self.assertTrue(trades.empty)
        self.assertEqual(len(curve),1)
        self.assertTrue(pd.notna(data.loc[START,'PreviousSMA200']))

    def test_multiple_crossed_rungs_execute_only_at_next_open(self):
        rows = [[100,100,90,90],[85,85,85,85]]
        result, curve, trades = run(rows)
        self.assertEqual(result['buy_executions'],2)
        self.assertEqual(set(trades.side),{'BUY'})
        self.assertEqual(set(trades.price),{85})
        self.assertEqual(set(trades.timestamp),{(START+pd.Timedelta(days=1)).isoformat()})
        self.assertEqual(curve.iloc[0].cash,10000)
        self.assertEqual(curve.iloc[1].cash,9000)

    def test_end_signal_never_executes_using_future_data(self):
        result, _, trades = run([[100,100,90,90]])
        self.assertTrue(trades.empty)
        self.assertEqual(result['unexecuted_end_signals'],2)
        self.assertEqual(result['final_portfolio_value'],10000)

    def test_hybrid_blocks_below_sma_and_at_equality(self):
        result, _, trades = run([[110,110,100,100],[100,100,100,100]],hybrid=True)
        self.assertTrue(trades.empty)  # close == SMA exactly; strict gate.
        self.assertGreater(result['filtered_buy_signals'],0)
        _, _, pure = run([[110,110,100,100],[100,100,100,100]])
        self.assertFalse(pure.empty)

    def test_gap_below_last_known_sma_blocks_pending_hybrid_buys(self):
        rows = [[120,120,110,110],[90,90,90,90]]
        result, _, trades = run(rows,hybrid=True)
        self.assertTrue(trades.empty)
        self.assertGreater(result['filtered_buy_signals'],0)

    def test_high_touch_sells_existing_lots_even_below_sma(self):
        rows = [[120,120,110,110],[110,110,110,110],[95,116,90,95]]
        result, _, trades = run(rows,hybrid=True)
        self.assertGreater(result['buy_executions'],0)
        self.assertEqual(result['sell_executions'],result['buy_executions'])
        sells = trades[trades.side=='SELL']
        self.assertTrue((sells.price==115.5).all())
        self.assertGreater(result['realized_pnl'],0)
        self.assertEqual(result['held_btc'],0)

    def test_cash_is_finite_and_ticket_includes_fee(self):
        result, curve, trades = run([[100,100,50,50],[50,50,50,50]],config=Config(capital=D(1000)))
        self.assertEqual(result['buy_executions'],2)
        self.assertGreater(result['unfunded_buy_signals'],0)
        self.assertEqual(result['remaining_usdt'],0)
        self.assertTrue((curve.cash>=0).all())
        first = trades.iloc[0]
        self.assertAlmostEqual(first.price*first.quantity+first.fee,500,places=10)

    def test_open_losses_and_drawdown_remain_visible_without_forced_sale(self):
        result, curve, trades = run([[100,100,90,90],[90,90,90,90],[45,45,45,45]])
        self.assertEqual(result['realized_pnl'],0)
        self.assertLess(result['unrealized_pnl'],0)
        self.assertLess(result['final_portfolio_value'],10000)
        self.assertGreater(result['max_drawdown_pct'],0)
        self.assertTrue((trades.side=='BUY').all())
        self.assertAlmostEqual(result['final_portfolio_value'],10000+result['realized_pnl']+result['unrealized_pnl'])
        self.assertTrue((abs(curve.equity-curve.cash-curve.btc*curve.close)<1e-8).all())

    def test_missing_days_and_invalid_ohlc_are_rejected(self):
        valid = source([[100,101,99,100]])
        bad = valid.copy(); bad.iloc[-1,bad.columns.get_loc('High')] = 98
        for frame in (valid.drop(valid.index[50]),bad,pd.DataFrame()):
            with self.subTest(size=len(frame)), self.assertRaises(ValueError):
                load_history(START,START+pd.Timedelta(days=1),download=lambda *a,**k:frame)

    def test_csv_replay_matches_download_without_network(self):
        rows = [[100,100,90,90],[90,100,90,100]]
        end = START+pd.Timedelta(days=2)
        downloaded = load_history(START,end,download=lambda *a,**k:source(rows))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'data.csv'
            downloaded[['Open','High','Low','Close']].to_csv(path)
            with patch('backtest_hybrid_grid.yf.download',side_effect=AssertionError('No network')):
                replay = load_history(START,end,csv=path)
        pd.testing.assert_frame_equal(downloaded,replay)

    def test_future_candles_cannot_change_earlier_fills_or_equity(self):
        rows = [[100,100,90,90],[90,90,90,90],[90,100,90,100]]
        _, curve, trades = run(rows)
        rows[-1] = [200,300,200,300]
        _, changed, changed_trades = run(rows)
        pd.testing.assert_frame_equal(curve.iloc[:2],changed.iloc[:2])
        cutoff = (START+pd.Timedelta(days=2)).isoformat()
        pd.testing.assert_frame_equal(trades[trades.timestamp<cutoff],changed_trades[changed_trades.timestamp<cutoff])

    def test_invalid_parameters_are_rejected(self):
        for kwargs in ({'capital':D('-1')},{'fee':D('nan')},{'ticket':D(0)},
                       {'drop':D(1)},{'recovery':D(0)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(**kwargs)


if __name__ == '__main__':
    unittest.main()
