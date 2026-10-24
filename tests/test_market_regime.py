"""Closed-candle calculations and asynchronous fail-closed hourly gating."""
from dataclasses import replace
import math
import os
import unittest
from unittest.mock import Mock, patch

from market_regime import (HISTORY, HOUR_MS, RegimeMonitor, calculate_regime,
                           classify, configured_enabled, public_fetcher)

END = 1_792_771_200_000 // HOUR_MS * HOUR_MS


def candles(trend=0):
    return [[END-(HISTORY-i)*HOUR_MS, 100+i*trend,
             101+i*trend, 99+i*trend, 100+i*trend, 10] for i in range(HISTORY)]


class IndicatorTests(unittest.TestCase):
    def test_analytical_flat_and_directional_series(self):
        flat = calculate_regime(candles(), END+100)
        self.assertEqual((flat.regime, flat.adx, flat.plus_di, flat.minus_di,
                          flat.atr, flat.ema20_slope_atr), ('RANGE', 0, 0, 0, 2, 0))
        for trend, direction in ((1, 'UPTREND'), (-.3, 'DOWNTREND')):
            value = calculate_regime(candles(trend), END+100)
            self.assertEqual(value.regime, direction)
            self.assertAlmostEqual(value.adx, 100)
            self.assertAlmostEqual(value.atr, 2)
            self.assertAlmostEqual(value.ema20_slope_atr, trend/2)
            self.assertAlmostEqual(value.plus_di if trend > 0 else value.minus_di,
                                   abs(trend)/2*100)

    def test_boundaries_and_conflicting_indicators_are_not_guessed(self):
        self.assertEqual(classify(19.99, 10, 11, -.1), 'RANGE')
        for args in ((20,10,10,0), (24.99,10,30,-.2), (25,30,10,-.2),
                     (30,10,30,-.1), (10,10,10,.11)):
            self.assertEqual(classify(*args), 'TRANSITION')
        self.assertEqual(classify(25,10,30,-.11), 'DOWNTREND')
        with self.assertRaises(ValueError):
            classify(math.nan, 10, 10, 0)

    def test_forming_and_future_price_changes_cannot_change_signal(self):
        expected = calculate_regime(candles(), END+100)
        future = [[END, 1e9, 2e9, 1, 1e9, 1],
                  [END+HOUR_MS, 1, 2, .5, 1, 1]]
        self.assertEqual(calculate_regime(candles()+future, END+100), expected)
        self.assertEqual(calculate_regime(list(reversed(candles())), END+100), expected)

    def test_missing_stale_duplicate_misaligned_bad_ohlcv_are_rejected(self):
        missing = candles(); del missing[50]
        stale = candles()[:-1]
        duplicate = candles()+[candles()[-1]]
        shifted = candles(); shifted[-1][0] += 1
        for rows in (missing, stale, duplicate, shifted):
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                calculate_regime(rows, END)
        for column, value in ((1,0),(2,99),(3,102),(4,math.inf),(5,-1),(5,True)):
            rows = candles(); rows[-1][column] = value
            with self.subTest(column=column), self.assertRaises(ValueError):
                calculate_regime(rows, END)
        zero = [[r[0],100,100,100,100,0] for r in candles()]
        with self.assertRaises(ValueError):
            calculate_regime(zero, END)

    def test_enable_flag_is_strict_and_off_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(configured_enabled())
        with patch.dict(os.environ, {'GRID_REGIME_FILTER_ENABLED':'true'}):
            self.assertTrue(configured_enabled())
        with patch.dict(os.environ, {'GRID_REGIME_FILTER_ENABLED':'tru'}):
            with self.assertRaises(ValueError):
                configured_enabled()

    def test_fetch_uses_only_public_mainnet_client_without_loading_markets(self):
        client = Mock()
        client.public_get_klines.return_value = candles()
        with patch('exchange_handler.create_macro_exchange', return_value=client) as factory, \
                patch('market_regime.time.time', return_value=(END+100)/1000):
            fetch = public_fetcher('BTC/USDT')
            factory.assert_not_called()
            self.assertEqual(fetch().regime, 'RANGE')
            fetch()
        factory.assert_called_once()
        client.load_markets.assert_not_called()
        self.assertEqual(client.public_get_klines.call_args.args[0],
                         {'symbol':'BTCUSDT','interval':'1h','limit':241})


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.now, self.wall, self.jobs = 0., END/1000+10, []
        self.fetch = Mock(return_value=calculate_regime(candles(), END))
        self.monitor = RegimeMonitor(self.fetch, clock=lambda:self.now,
                                     wall_clock=lambda:self.wall, launch=self.jobs.append)

    def ready(self):
        self.assertFalse(self.monitor.buys_allowed())
        self.jobs.pop()()
        self.assertTrue(self.monitor.buys_allowed())

    def test_reads_do_not_fetch_and_concurrent_requests_are_single_flight(self):
        self.monitor.status()
        self.assertEqual(self.jobs, [])
        for _ in range(20):
            self.assertFalse(self.monitor.buys_allowed())
        self.assertEqual(len(self.jobs),1)
        self.fetch.assert_not_called()
        self.jobs.pop()()
        self.assertTrue(self.monitor.status()['buy_allowed'])

    def test_failed_refresh_invalidates_positive_cache_and_backs_off(self):
        self.ready()
        self.now=60
        self.fetch.side_effect=TimeoutError('offline')
        self.monitor.buys_allowed(); self.jobs.pop()()
        self.assertFalse(self.monitor.status()['buy_allowed'])
        self.now=89
        self.monitor.buys_allowed(); self.assertEqual(self.jobs,[])
        self.now=90
        self.monitor.buys_allowed(); self.jobs.pop()()
        self.now=149
        self.monitor.buys_allowed(); self.assertEqual(self.jobs,[])
        self.now=150
        self.monitor.buys_allowed(); self.assertEqual(len(self.jobs),1)

    def test_stale_cache_hour_rollover_and_slow_worker_fail_closed(self):
        self.ready()
        self.now=180
        self.assertFalse(self.monitor.status()['buy_allowed'])
        self.now=3600; self.wall+=3600
        self.assertFalse(self.monitor.status()['buy_allowed'])
        self.monitor.buys_allowed()  # Worker has the prior-hour response.
        self.jobs.pop()()
        self.assertFalse(self.monitor.status()['available'])

    def test_future_or_inconsistent_provider_results_fail_closed(self):
        for change in ({'closed_at_ms':END+HOUR_MS}, {'regime':'RANGE','adx':90},
                       {'atr':math.nan}, {'ema20_slope_atr':math.inf}):
            self.setUp()
            self.fetch.return_value=replace(self.fetch.return_value,**change)
            self.monitor.buys_allowed(); self.jobs.pop()()
            self.assertFalse(self.monitor.status()['buy_allowed'])

    def test_launch_failure_does_not_retry_every_trading_tick(self):
        self.monitor._launch=Mock(side_effect=RuntimeError('thread unavailable'))
        for _ in range(20):
            self.assertFalse(self.monitor.buys_allowed())
        self.monitor._launch.assert_called_once()


if __name__ == '__main__':
    unittest.main()
