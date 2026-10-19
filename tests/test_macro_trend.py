"""Deterministic Yahoo parsing, background refresh, and live BUY-only suspension."""
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
import tempfile
from threading import Event
import unittest
import json
from unittest.mock import Mock, patch

import ccxt
import pandas as pd

import engine
from database import GridDatabase
from engine import GridBot
from macro_trend import MacroTrendMonitor, MacroTrendSnapshot, fetch_macro_trend
from models import TradingHalt
from settings import (MACRO_BUY_PAUSE_KEY, SAFETY_MODE_KEY, PAUSED_MANUAL,
                      PAUSED_RESET, LIQUIDATED, ENGINE_RUNNING, ENGINE_STATUS_KEY)
from tests.fakes import make_bot

NOW = pd.Timestamp('2026-10-17T01:00:00Z')


def history(last=100):
    frame = pd.DataFrame({'Close': [100.] * 250},
                         index=pd.date_range('2026-02-09', periods=250, freq='D', tz='UTC'))
    # Define dates relative to NOW to avoid accidental stale fixture history.
    frame.index = pd.date_range(NOW.normalize() - pd.Timedelta(days=250), periods=250, freq='D')
    frame.iloc[-1, 0] = last
    return frame


class YahooDataTests(unittest.TestCase):
    def test_up_down_and_equality_use_strictly_greater_than_sma(self):
        for close, expected in ((101, True), (99, False), (100, False)):
            with self.subTest(close=close):
                result = fetch_macro_trend(now=NOW, download=Mock(return_value=history(close)))
                self.assertIs(result.uptrend, expected)
                self.assertAlmostEqual(result.sma200, (19900 + close) / 200)

    def test_download_requests_exact_250_closed_utc_days_and_bounded_timeout(self):
        download = Mock(return_value=history())
        result = fetch_macro_trend(now=NOW, download=download)
        args, kwargs = download.call_args
        self.assertEqual(args, ('BTC-USD',))
        self.assertEqual(NOW.normalize() - pd.Timestamp(kwargs['start'], tz='UTC'), pd.Timedelta(days=250))
        self.assertEqual(pd.Timestamp(kwargs['end']), NOW)
        self.assertEqual((kwargs['interval'], kwargs['timeout'], kwargs['threads']), ('1d', 10, False))
        self.assertFalse(kwargs['auto_adjust'])
        self.assertEqual(result.candle_day, '2026-10-16')

    def test_live_ended_fetch_bypasses_cached_incomplete_historical_response(self):
        cached = history(101).iloc[:-1]
        requests = []
        def download(*args, **kwargs):
            end = pd.Timestamp(kwargs['end'])
            requests.append(end)
            # Model yfinance's historical-cache selection, not its private API.
            return cached if end + pd.Timedelta(minutes=30) <= NOW else history(101)
        result = fetch_macro_trend(now=NOW, download=download)
        self.assertTrue(result.uptrend)
        self.assertEqual(requests, [NOW])

    def test_single_ticker_multiindex_and_flat_columns_agree(self):
        flat = history(101)
        multi = flat.copy()
        multi.columns = pd.MultiIndex.from_tuples([('Close','BTC-USD')])
        self.assertEqual(fetch_macro_trend(now=NOW, download=lambda *a,**k: flat),
                         fetch_macro_trend(now=NOW, download=lambda *a,**k: multi))

    def test_forming_day_is_not_used(self):
        frame = history()
        frame.loc[NOW.normalize(), 'Close'] = 100000
        self.assertFalse(fetch_macro_trend(now=NOW, download=lambda *a,**k: frame).uptrend)

    def test_empty_insufficient_stale_missing_duplicate_nonfinite_data_rejected(self):
        bad = history(); bad.iloc[-1, 0] = float('inf')
        nan = history(); nan.iloc[-1, 0] = float('nan')
        duplicate = pd.concat([history(),history().tail(1)])
        for frame in (None, pd.DataFrame(), history().tail(199), history().iloc[:-1],
                      history().drop(history().index[-100]), duplicate, bad, nan):
            with self.subTest(case=type(frame)), self.assertRaises(ValueError):
                fetch_macro_trend(now=NOW, download=lambda *a,**k: frame)


class MonitorTests(unittest.TestCase):
    def monitor(self):
        self.now = 0.
        self.utc = NOW.to_pydatetime()
        self.jobs = []
        self.fetch = Mock(return_value=MacroTrendSnapshot(101,100,'2026-10-16'))
        return MacroTrendMonitor(self.fetch, clock=lambda:self.now,
                                 utcnow=lambda:self.utc, launch=self.jobs.append)

    def test_cold_start_is_false_and_concurrent_calls_share_one_refresh(self):
        monitor = self.monitor()
        for _ in range(20):
            self.assertFalse(monitor.get_macro_trend())
        self.assertEqual(len(self.jobs), 1)
        self.fetch.assert_not_called()
        self.jobs.pop()()
        self.assertTrue(monitor.get_macro_trend())

    def test_status_is_read_only_and_reports_missing_then_fresh_then_stale(self):
        monitor = self.monitor()
        self.assertEqual(monitor.status()['reason_code'], 'DATA_UNAVAILABLE')
        self.assertEqual(self.jobs, [])
        monitor.get_macro_trend(); self.jobs.pop()()
        status = monitor.status()
        self.assertTrue(status['available'])
        self.assertTrue(status['buy_allowed'])
        self.assertEqual(status['last_success_at'], self.utc.isoformat())
        self.now = 901
        self.assertEqual(monitor.status()['reason_code'], 'DATA_STALE')
        self.assertEqual(self.jobs, [])  # Telemetry cannot start a download.

    def test_failed_refresh_retains_last_success_time_and_exposes_error(self):
        monitor = self.monitor()
        monitor.get_macro_trend(); self.jobs.pop()()
        success = monitor.status()['last_success_at']
        self.now = 300
        self.utc = (NOW + pd.Timedelta(minutes=5)).to_pydatetime()
        self.fetch.side_effect = ValueError('Missing daily close')
        monitor.get_macro_trend(); self.jobs.pop()()
        status = monitor.status()
        self.assertFalse(status['available'])
        self.assertFalse(status['buy_allowed'])
        self.assertEqual(status['last_success_at'], success)
        self.assertEqual(status['last_attempt_at'], self.utc.isoformat())
        self.assertIn('Missing daily close', status['error'])

    def test_late_daily_publication_retries_fresh_request_and_recovers(self):
        monitor = self.monitor()
        published = False
        requests = []
        def download(*args, **kwargs):
            requests.append(kwargs['end'])
            return history(101) if published else history(101).iloc[:-1]
        monitor.fetch = lambda: fetch_macro_trend(now=self.utc, download=download)
        monitor.get_macro_trend(); self.jobs.pop()()
        self.assertFalse(monitor.get_macro_trend())
        self.assertEqual(self.jobs, [])
        self.assertIsNone(monitor.status()['last_success_at'])
        self.now = 30
        self.utc = (NOW + pd.Timedelta(seconds=30)).to_pydatetime()
        published = True
        monitor.get_macro_trend(); self.jobs.pop()()
        self.assertTrue(monitor.get_macro_trend())
        self.assertNotEqual(requests[0], requests[1])
        self.assertIsNone(monitor.status()['error'])

    def test_fresh_downtrend_is_distinct_from_unavailable_data(self):
        monitor = self.monitor()
        self.fetch.return_value = MacroTrendSnapshot(99, 100, '2026-10-16')
        monitor.get_macro_trend(); self.jobs.pop()()
        self.assertEqual(monitor.status()['reason_code'], 'BELOW_OR_EQUAL_SMA200')
        self.assertTrue(monitor.status()['available'])
        self.assertFalse(monitor.status()['buy_allowed'])

    def test_rollover_status_rejects_previous_signal_without_fetching(self):
        monitor = self.monitor()
        monitor.get_macro_trend(); self.jobs.pop()()
        self.utc = datetime(2026, 10, 18, tzinfo=timezone.utc)
        self.assertFalse(monitor.status()['available'])
        self.assertEqual(monitor.status()['reason_code'], 'DATA_STALE')
        self.assertEqual(self.jobs, [])

    def test_soft_refresh_uses_fresh_cache_but_hard_expiry_blocks(self):
        monitor = self.monitor()
        monitor.get_macro_trend(); self.jobs.pop()()
        self.now = 300
        self.assertTrue(monitor.get_macro_trend())
        self.assertEqual(len(self.jobs), 1)
        self.now = 901
        self.assertFalse(monitor.get_macro_trend())
        self.assertEqual(len(self.jobs), 1)  # No piled-up downloads while worker is stalled.

    def test_failure_invalidates_positive_cache_and_retries_with_backoff(self):
        monitor = self.monitor()
        monitor.get_macro_trend(); self.jobs.pop()()
        self.fetch.side_effect = TimeoutError('Yahoo timeout')
        self.now = 300
        monitor.get_macro_trend(); self.jobs.pop()()
        self.assertFalse(monitor.get_macro_trend())
        self.assertEqual(self.jobs, [])
        self.now = 330
        self.assertFalse(monitor.get_macro_trend())
        self.assertEqual(len(self.jobs), 1)

    def test_utc_rollover_invalidates_cache_immediately(self):
        monitor = self.monitor()
        self.utc = datetime(2026,10,17,23,59,tzinfo=timezone.utc)
        monitor.get_macro_trend(); self.jobs.pop()()
        self.utc = datetime(2026,10,18,0,0,tzinfo=timezone.utc)
        self.assertFalse(monitor.get_macro_trend())
        self.assertEqual(len(self.jobs), 1)

    def test_worker_start_failure_is_safe_and_backed_off(self):
        monitor = self.monitor()
        monitor._launch = Mock(side_effect=RuntimeError('no threads'))
        self.assertFalse(monitor.get_macro_trend())
        self.assertFalse(monitor.get_macro_trend())
        self.assertEqual(monitor._launch.call_count, 1)

    def test_slow_download_cannot_block_a_trading_caller(self):
        started, release, finished = Event(), Event(), Event()
        def fetch():
            started.set()
            try:
                release.wait(3)
                return MacroTrendSnapshot(101,100,'2026-10-16')
            finally:
                finished.set()
        monitor = MacroTrendMonitor(fetch, utcnow=lambda:NOW.to_pydatetime())
        try:
            self.assertFalse(monitor.get_macro_trend())
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.is_set())
            self.assertFalse(monitor.get_macro_trend())
        finally:
            release.set()
            self.assertTrue(finished.wait(1))


class MacroBuyPauseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'grid_testnet.sqlite'
        self.bot, self.exchange = make_bot(self.path, initial_base=D(0))
        self.bot.run_cycle(); self.bot.run_cycle()
        self.bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        self.trend = Mock(return_value=False)
        self.bot._macro_trend_provider = self.trend

    def test_downtrend_cancels_only_bot_buys_retains_sells_and_inventory(self):
        manual = self.exchange.create_order('BTC/USDT','limit','buy',.2,50,{'newClientOrderId':'manual'})
        sells = {o['id'] for o in self.exchange.fetch_open_orders('BTC/USDT') if o['side']=='sell'}
        held = self.bot._bot_btc_balance()
        self.bot.run_cycle()
        live = self.exchange.fetch_open_orders('BTC/USDT')
        self.assertEqual({o['id'] for o in live if o['side']=='sell'}, sells)
        self.assertEqual([o['id'] for o in live if o['side']=='buy'], [manual['id']])
        self.assertEqual(self.bot._bot_btc_balance(), held)
        self.assertEqual(self.bot.database.get_state(MACRO_BUY_PAUSE_KEY), '1')
        self.assertIsNone(self.bot.database.get_state(SAFETY_MODE_KEY))
        self.assertEqual(self.exchange.market_sell_calls, 0)

    def test_filled_buy_gets_a_sell_even_during_macro_pause(self):
        buy = next(r for r in self.bot.database.fetch_active_grids() if r['side']=='BUY')
        self.exchange.fill(buy['client_order_id'])
        self.bot.run_cycle()
        row = self.bot.database.fetch_latest_orders_by_level()[buy['level']]
        self.assertEqual((row['side'],row['status']), ('SELL','OPEN'))

    def test_existing_sell_fills_are_accounted_without_rebuy(self):
        self.bot.run_cycle()
        sell = next(r for r in self.bot.database.fetch_active_grids() if r['side']=='SELL')
        self.exchange.fill(sell['client_order_id'])
        self.bot.run_cycle()
        self.assertIsNotNone(self.bot.database.get_trade_by_sell_order_id(sell['order_id']))
        self.assertFalse(any(r['side']=='BUY' for r in self.bot.database.fetch_active_grids()))

    def test_recovery_restores_only_eligible_buys(self):
        self.bot.run_cycle()
        self.trend.return_value = True
        self.bot.run_cycle()
        self.assertIsNone(self.bot.database.get_state(MACRO_BUY_PAUSE_KEY))
        self.assertTrue(any(r['side']=='BUY' for r in self.bot.database.fetch_active_grids()))

    def test_recovery_cannot_release_a_manual_pause(self):
        self.bot.set_manual_pause(True)
        self.bot.run_cycle()
        self.trend.return_value = True
        self.bot.run_cycle()
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY), PAUSED_MANUAL)
        self.assertFalse(any(r['side']=='BUY' for r in self.bot.database.fetch_active_grids()))

    def test_hard_stop_has_priority_over_macro_pause(self):
        self.exchange.price = D(69)
        self.bot.run_cycle()
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)
        self.assertEqual(self.exchange.market_sell_calls, 1)

    def test_seed_and_last_moment_limit_buys_are_blocked_before_intent(self):
        before = len(self.bot.database.fetch_all_orders())
        for kind, level in (('LIMIT',1),('MARKET',0)):
            self.assertIsNone(self.bot._submit_order(level,'BUY',D(90),D(1),order_type=kind))
        self.assertEqual(len(self.bot.database.fetch_all_orders()), before)

    def test_cancellation_timeout_retries_and_late_cancel_is_not_a_fault(self):
        self.exchange.cancel_timeout = 'after'
        self.bot.run_cycle()
        self.assertEqual(self.bot.database.get_state(MACRO_BUY_PAUSE_KEY),'1')
        self.bot.run_cycle()
        self.assertFalse(any(r['side']=='BUY' for r in self.bot.database.fetch_active_grids()))
        self.assertIsNone(self.bot.database.get_state('halt_reason'))
        self.assertTrue(any(r['side']=='SELL' for r in self.bot.database.fetch_active_grids()))

    def test_partial_cancel_fill_is_protected_by_a_sell(self):
        buy = next(r for r in self.bot.database.fetch_active_grids() if r['side']=='BUY')
        self.exchange.orders[buy['client_order_id']].update(filled='1',cost=buy['price'])
        self.exchange.base_free += 1
        self.bot.run_cycle()
        row = self.bot.database.fetch_latest_orders_by_level()[buy['level']]
        self.assertEqual((row['side'],row['status']),('SELL','OPEN'))

    def test_restart_recognizes_canceled_macro_lanes_and_reapplies_guard(self):
        self.bot.run_cycle()
        recovered = GridBot(self.bot.config,self.exchange,GridDatabase(self.path),
                            macro_trend_provider=lambda:False)
        recovered.prepare(persist=False)
        recovered.run_cycle()
        self.assertFalse(any(r['side']=='BUY' for r in recovered.database.fetch_active_grids()))

    def test_pending_reanchor_during_downtrend_never_cancels_existing_sells(self):
        self.bot._macro_trend_provider = lambda:True
        self.bot.request_manual_recenter('100','20','30','1000',8)
        self.bot._macro_trend_provider = lambda:False
        sells = {r['order_id'] for r in self.bot.database.fetch_active_grids() if r['side']=='SELL'}
        self.assertFalse(self.bot.reset_grid())
        self.assertTrue(self.bot.grid_needs_reset)
        self.assertEqual({r['order_id'] for r in self.bot.database.fetch_active_grids() if r['side']=='SELL'},sells)

    def test_stopped_grid_restart_restores_sells_while_macro_buys_stay_blocked(self):
        self.bot.stop_engine()
        held = self.bot._bot_btc_balance()
        buy_ids = {key for key,row in self.exchange.orders.items() if row['side']=='buy'}
        self.bot.start_engine(auto_cover_inventory=True)
        self.assertTrue(self.bot.grid_needs_reset)
        # These are deliberate STOP cancellations, not failed working lanes.
        self.assertTrue(any(r['side'] == 'SELL' and r['status'] == 'CANCELED'
                            for r in self.bot.database.fetch_all_orders()))
        self.assertTrue(self.bot.reset_grid())
        self.assertFalse(self.bot.grid_needs_reset)
        active = self.bot.database.fetch_active_grids()
        self.assertTrue(active)
        self.assertTrue(all(r['side']=='SELL' for r in active))
        self.assertEqual({key for key,row in self.exchange.orders.items() if row['side']=='buy'},buy_ids)
        self.assertEqual(self.bot._bot_btc_balance(), held)
        self.assertEqual(self.bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_RUNNING)
        self.trend.return_value = True
        self.bot.run_cycle()
        self.assertTrue(any(r['side'] == 'SELL' for r in self.bot.database.fetch_active_grids()))
        self.assertTrue(any(r['side'] == 'BUY' for r in self.bot.database.fetch_active_grids()))
        self.assertEqual(self.exchange.market_sell_calls, 0)

    def test_stopped_grid_with_pending_reset_still_liquidates_below_hard_stop(self):
        self.bot.stop_engine()
        self.bot.start_engine(auto_cover_inventory=True)
        self.exchange.price = D(69)
        self.assertFalse(self.bot.reset_grid())
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)
        self.assertEqual(self.exchange.market_sell_calls, 1)

    def test_macro_refresh_finishing_mid_cycle_cannot_replay_stopped_lanes(self):
        self.bot.stop_engine()
        old_ids=set(self.exchange.orders)
        self.bot.start_engine(auto_cover_inventory=True)
        calls=iter([False]+[True]*100)
        self.trend.side_effect=lambda:next(calls)
        self.assertTrue(self.bot.reset_grid())
        live=self.exchange.fetch_open_orders('BTC/USDT')
        self.assertTrue(live)
        self.assertTrue(all(row['id'] not in old_ids for row in live))

    def test_cold_process_resume_preserves_reserved_btc_and_never_seeds(self):
        saved=json.loads(self.bot.database.get_state('grid_run'))
        saved['baseline_base']='1'
        self.bot.database.set_state('grid_run',json.dumps(saved))
        self.bot.baseline_base=D(1)
        self.exchange.base_free+=D(1)
        held=self.bot._bot_btc_balance()
        self.bot.stop_engine()
        buys={key for key,row in self.exchange.orders.items() if row['side']=='buy'}
        recovered=GridBot(self.bot.config,self.exchange,GridDatabase(self.path),
                          macro_trend_provider=lambda:False)
        recovered.ready_mode=True
        recovered.start_engine(auto_cover_inventory=True)
        self.assertTrue(recovered.reset_grid())
        self.assertEqual(D(json.loads(recovered.database.get_state('grid_run'))['baseline_base']),D(1))
        self.assertEqual(recovered._bot_btc_balance(),held)
        self.assertEqual({key for key,row in self.exchange.orders.items() if row['side']=='buy'},buys)
        self.assertTrue(all(r['side']=='SELL' for r in recovered.database.fetch_active_grids()))

    def test_deferred_reanchor_protects_new_buy_fill_and_accounts_sell_fill(self):
        self.bot._macro_trend_provider = lambda: True
        self.bot.request_manual_recenter('100','20','30','1000',8)
        self.bot._macro_trend_provider = self.trend
        buy = next(r for r in self.bot.database.fetch_active_grids() if r['side'] == 'BUY')
        sell = next(r for r in self.bot.database.fetch_active_grids() if r['side'] == 'SELL')
        self.exchange.fill(buy['client_order_id'])
        self.exchange.fill(sell['client_order_id'])
        self.assertFalse(self.bot.reset_grid())
        replacement = self.bot.database.fetch_latest_orders_by_level()[buy['level']]
        self.assertEqual((replacement['side'], replacement['status']), ('SELL', 'OPEN'))
        self.assertIsNotNone(self.bot.database.get_trade_by_sell_order_id(sell['order_id']))
        count = len(self.exchange.orders)
        self.assertFalse(self.bot.reset_grid())
        self.assertEqual(len(self.exchange.orders), count)

    def test_unexpected_cancellation_outside_rebuild_still_faults(self):
        sell = next(r for r in self.bot.database.fetch_active_grids() if r['side'] == 'SELL')
        self.exchange.cancel_order(sell['order_id'], self.bot.config.symbol,
                                   {'origClientOrderId': sell['client_order_id']})
        with self.assertRaisesRegex(TradingHalt, 'ended as canceled'):
            self.bot.run_cycle()

    def test_provider_exception_suspends_buys_without_stopping_sells(self):
        self.trend.side_effect = RuntimeError('Yahoo cache failure')
        self.bot.run_cycle()
        self.assertEqual(self.bot.database.get_state(MACRO_BUY_PAUSE_KEY),'1')
        self.assertTrue(any(r['side']=='SELL' for r in self.bot.database.fetch_active_grids()))

    def test_console_warning_is_logged_only_when_state_changes(self):
        with self.assertLogs('engine',level='WARNING') as logs:
            self.bot.run_cycle(); self.bot.run_cycle()
        warnings = [line for line in logs.output if 'Buy orders suspended to protect capital' in line]
        self.assertEqual(len(warnings),1)

    def test_production_default_provider_blocks_fresh_seed_until_uptrend(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        exchange = type(self.exchange)()
        exchange.base_free = D(0)
        with patch.object(engine,'get_macro_trend',return_value=False) as provider:
            fresh = GridBot(self.bot.config,exchange,
                            GridDatabase(Path(directory.name)/'grid_testnet.sqlite'))
            fresh.prepare(persist=True)
            fresh.run_cycle()
            self.assertEqual(exchange.orders,{})
            self.assertEqual(fresh.database.get_state(MACRO_BUY_PAUSE_KEY),'1')
            provider.return_value = True
            fresh.run_cycle()
            self.assertEqual(len(exchange.orders),1)
            self.assertEqual(next(iter(exchange.orders.values()))['type'],'market')

    def test_macro_recovery_does_not_release_a_reset_lock(self):
        self.bot.prepare_factory_reset()
        self.trend.return_value = True
        self.bot.run_cycle()
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY),PAUSED_RESET)
        self.assertEqual(self.exchange.fetch_open_orders('BTC/USDT'),[])


if __name__ == '__main__':
    unittest.main()
