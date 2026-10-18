"""Daily indicator availability and fixed catastrophe stops, without live I/O."""
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal as D
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from backtest_grid import Config, GridBacktest, atr_bounds, floor_step, main, prepare_indicators
from tests.test_backtest_grid import START, fine_candles


def daily_history(extra_days=2):
    return pd.DataFrame([[100, 101, 99, 100, 1000]] * (200 + extra_days),
                        columns=['open', 'high', 'low', 'close', 'volume'],
                        index=pd.date_range(START - pd.Timedelta(days=200), periods=200 + extra_days, freq='D'))


class DailyMacroBacktestTests(unittest.TestCase):
    def test_default_policy_is_daily_sma200_fifteen_percent_buffer(self):
        config = Config()
        self.assertEqual((config.trend_timeframe, config.trend_period, config.stop_mode, config.stop_gap),
                         ('1d', 200, 'fixed', D('.15')))
        # ATR bigger than 15% of the lower bound must not silently change this rule.
        low, high, stop = atr_bounds(D(100), D(20), config)
        self.assertEqual(stop, floor_step(low * D('.85'), D('.01')))
        self.assertLess(stop, low)

    def test_daily_sma_is_unavailable_until_next_utc_midnight(self):
        frame = fine_candles([[100, 101, 99, 100, 1]] * 289)
        source = daily_history()
        changed = source.copy()
        changed.loc[START, ['high', 'close']] = [300, 200]
        a = prepare_indicators(frame, trend_timeframe='1d', trend_period=200, trend_frame=source)
        b = prepare_indicators(frame, trend_timeframe='1d', trend_period=200, trend_frame=changed)
        last = START + pd.Timedelta(hours=23, minutes=55)
        pd.testing.assert_frame_equal(a.loc[START:last], b.loc[START:last])
        self.assertEqual(a.loc[last, 'macro_sma'], 100)
        self.assertEqual(b.loc[START + pd.Timedelta(days=1), 'macro_sma'], 100.5)
        # Daily SMA changes do not substitute a daily ATR for the selected 1h ATR.
        pd.testing.assert_series_equal(a.atr, b.atr)

    def test_daily_fifty_is_an_explicit_supported_variant(self):
        source = daily_history()
        source.loc[START - pd.Timedelta(days=1), ['high', 'close']] = [200, 200]
        frame = fine_candles([[101, 101, 101, 101, 1]])
        sma200 = prepare_indicators(frame, trend_timeframe='1d', trend_period=200, trend_frame=source)
        sma50 = prepare_indicators(frame, trend_timeframe='1d', trend_period=50, trend_frame=source)
        self.assertEqual(sma200.loc[START, 'macro_sma'], 100.5)
        self.assertEqual(sma50.loc[START, 'macro_sma'], 102)

    def test_daily_gate_blocks_equal_price_and_allows_only_strictly_above(self):
        frame = fine_candles([[100, 100, 100, 100, 1], [101, 101, 101, 101, 1]])
        bot = GridBacktest(Config(buy_trend_policy='legacy'))
        result = bot.run(frame, START, START + pd.Timedelta(minutes=10), trend_frame=daily_history())
        starts = [e for e in bot.events if e['event'] == 'GRID_STARTED']
        self.assertEqual(len(starts), 1)
        self.assertEqual(pd.Timestamp(starts[0]['timestamp']), START + pd.Timedelta(minutes=5))
        self.assertGreater(D(starts[0]['center']), D(starts[0]['macro_sma']))
        self.assertEqual(result['entry_trend_blocked_bars'], 1)

    def test_fixed_stop_does_not_ratchet_and_still_triggers_strictly_below(self):
        bot = GridBacktest(Config(stop_mode='fixed', buy_trend_policy='legacy'))
        bot.start_grid(START, D(100), D(1))
        original = bot.stop
        bot._point(D(150), START)
        bot._point(D(100), START + pd.Timedelta(hours=1))
        self.assertEqual(bot.stop, original)
        self.assertEqual(bot.stop_count, 0)
        bot._point(original, START + pd.Timedelta(hours=2))
        self.assertEqual(bot.stop_count, 0)
        bot._point(original - D('.01'), START + pd.Timedelta(hours=2, minutes=1))
        self.assertEqual(bot.stop_count, 1)
        self.assertEqual(bot.orders, {})

    def test_fixed_stop_repositions_only_after_a_successful_reanchor(self):
        bot = GridBacktest(Config(stop_mode='fixed', buy_trend_policy='legacy'))
        bot.start_grid(START, D(100), D(1))
        old_stop = bot.stop
        bot.macro_sma = D(90)
        bot._point(D(105), START)
        self.assertEqual(bot.stop, old_stop)
        bot._point(D(105), START + pd.Timedelta(hours=4))
        self.assertEqual(bot.breakout_count, 1)
        self.assertEqual(bot.stop, max(old_stop, floor_step(bot.lower * D('.85'), D('.01'))))
        new_stop = bot.stop
        bot._point(D(110), START + pd.Timedelta(hours=4, minutes=5))
        self.assertEqual(bot.stop, new_stop)

    def test_legacy_trailing_is_explicit_and_still_works(self):
        bot = GridBacktest(Config(stop_mode='trailing', stop_gap=D('.02'), buy_trend_policy='legacy'))
        bot.start_grid(START, D(100), D(1))
        initial = bot.stop
        bot._point(D(105), START)
        self.assertGreater(bot.stop, initial)

    def test_missing_daily_warmup_or_stale_tail_is_rejected(self):
        frame = fine_candles([[101, 101, 101, 101, 1]])
        with self.assertRaisesRegex(ValueError, 'Trend data must cover'):
            GridBacktest(Config(buy_trend_policy='legacy')).run(frame, START, START + pd.Timedelta(minutes=5),
                                      trend_frame=daily_history().iloc[1:])
        longer = fine_candles([[101, 101, 101, 101, 1]] * 865)
        with self.assertRaisesRegex(ValueError, 'Trend data must cover'):
            GridBacktest(Config(buy_trend_policy='legacy')).run(longer, START, START + pd.Timedelta(minutes=5 * 865),
                                      trend_frame=daily_history(extra_days=1))

    def test_offline_daily_replay_preserves_both_inputs_and_never_fetches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            execution = root / 'btc_usdt_5m.csv'
            trend = root / 'btc_usdt_1d_trend.csv'
            fine_candles([[101, 101, 101, 101, 1]] * 2).to_csv(execution, index_label='timestamp')
            daily_history().to_csv(trend, index_label='timestamp')
            originals = execution.read_bytes(), trend.read_bytes()
            args = ['--buy-trend-policy', 'legacy', '--csv', str(execution), '--trend-csv', str(trend), '--start', START.isoformat(),
                    '--end', (START + pd.Timedelta(minutes=10)).isoformat(), '--output', str(root)]
            with patch('backtest_grid.fetch_candles', side_effect=AssertionError('Network forbidden')), redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            self.assertEqual((execution.read_bytes(), trend.read_bytes()), originals)
            result = json.loads((root / 'summary.json').read_text())
            self.assertEqual(result['trend_file'], 'trend_snapshot.csv')
            self.assertTrue(result['trend_sha256'])
            self.assertEqual(result['results'][0]['trend_period'], 200)
            self.assertEqual(result['results'][0]['stop_mode'], 'fixed')

    def test_offline_daily_mode_requires_trend_file_instead_of_silent_network_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.csv'
            fine_candles([[101, 101, 101, 101, 1]]).to_csv(source, index_label='timestamp')
            with patch('backtest_grid.fetch_candles', side_effect=AssertionError('Network forbidden')), redirect_stderr(io.StringIO()) as errors:
                code = main(['--csv', str(source), '--start', START.isoformat(),
                             '--end', (START + pd.Timedelta(minutes=5)).isoformat(), '--output', directory])
            self.assertEqual(code, 1)
            self.assertIn('requires --trend-csv', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
