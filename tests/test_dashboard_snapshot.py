"""Coherent local projections without waiting on exchange I/O or engine locks."""
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from fastapi import HTTPException, Response
import api
import engine
from allocation import pending_buy_commitment, require_total_cap, tracked_position_cost
from database import GridDatabase
from models import GridSizingError
from settings import ENGINE_RUNNING, ENGINE_IDLE, ENGINE_STATUS_KEY
from tests.fakes import make_bot


class DashboardSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'grid_testnet.sqlite'
        self.alert = patch.object(engine, 'schedule_telegram_alert', return_value=False)
        self.alert.start(); self.addCleanup(self.alert.stop)
        self.bot, self.exchange = make_bot(self.path)
        self.bot.run_cycle(); self.bot.run_cycle()
        self.bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        for name, value in [('grid_bot', self.bot), ('standby', False), ('monitor_only', False)]:
            change = patch.object(api.app.state, name, value)
            change.start(); self.addCleanup(change.stop)

    def snapshot(self):
        return api.dashboard_snapshot(Response(), _='test')

    def hold_cycle(self):
        started, release = Event(), Event()
        def hold():
            with self.bot._cycle_lock:
                started.set(); release.wait(3)
        thread = Thread(target=hold)
        thread.start()
        self.assertTrue(started.wait(1))
        self.addCleanup(thread.join, 3)
        self.addCleanup(release.set)
        return release

    def test_active_snapshot_has_one_mark_and_ledger_without_exchange_calls(self):
        with patch.object(self.exchange, 'fetch_balance', side_effect=AssertionError('HTTP')), \
                patch.object(self.exchange, 'fetch_open_orders', side_effect=AssertionError('HTTP')), \
                patch.object(self.exchange, 'fetch_ticker', side_effect=AssertionError('HTTP')):
            snap = self.snapshot()
        self.assertEqual(snap['bot']['grid_levels'], len(snap['orders']['open_limits']))
        self.assertTrue(snap['mark_price_at'])
        wallet = snap['bot']['wallet']
        self.assertAlmostEqual(wallet['unrealized_pnl'],
            (float(snap['mark_price']) - wallet['average_cost']) * wallet['btc_held'])
        self.assertFalse(snap['reconciling'])

    def test_busy_engine_without_cache_returns_retry_without_waiting(self):
        self.hold_cycle()
        with self.assertRaises(HTTPException) as raised:
            self.snapshot()
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail['code'], 'ENGINE_RECONCILING')

    def test_busy_engine_returns_previous_complete_snapshot_with_original_time(self):
        previous = self.snapshot()
        release = self.hold_cycle()
        self.bot.database.set_state(ENGINE_STATUS_KEY, 'HALTED')
        cached = self.snapshot()
        self.assertTrue(cached['reconciling'])
        self.assertEqual(cached['snapshot_id'], previous['snapshot_id'])
        self.assertEqual(cached['captured_at'], previous['captured_at'])
        self.assertEqual(cached['bot'], previous['bot'])
        self.assertEqual(cached['control_state']['engine_status'], 'HALTED')
        release.set()

    def test_fill_updates_wallet_and_rows_together_in_next_snapshot(self):
        before = self.snapshot()
        buy = next(r for r in self.bot.database.fetch_active_grids() if r['side'] == 'BUY')
        self.exchange.fill(buy['client_order_id'])
        self.bot.run_cycle()
        after = self.snapshot()
        self.assertGreater(after['bot']['wallet']['btc_held'], before['bot']['wallet']['btc_held'])
        self.assertNotIn(buy['order_id'], [r['id'] for r in after['orders']['open_limits']])
        self.assertNotEqual(before['snapshot_id'], after['snapshot_id'])
        self.assertEqual(after['bot']['grid_levels'], len(after['orders']['open_limits']))

    def test_read_snapshot_is_consistent_and_rejects_writes(self):
        db = self.bot.database
        connection = sqlite3.connect(db.path)
        try:
            connection.execute('PRAGMA journal_mode=WAL')
        finally:
            connection.close()
        other = GridDatabase(self.path)
        db.set_state('snapshot_probe', 'before')
        with db.read_snapshot():
            self.assertEqual(db.get_state('snapshot_probe'), 'before')
            other.set_state('snapshot_probe', 'after')
            self.assertEqual(db.get_state('snapshot_probe'), 'before')
            with self.assertRaises(sqlite3.OperationalError):
                db.set_state('forbidden_write', 'x')
        self.assertEqual(db.get_state('snapshot_probe'), 'after')
        self.assertIsNone(db.get_state('forbidden_write'))

    def test_cap_rejects_reanchor_before_cancelling_or_persisting(self):
        before = len(self.exchange.orders)
        with self.assertRaisesRegex(GridSizingError, 'Total allocation cap exceeded'):
            self.bot.request_manual_recenter('100', '20', '30', '400', 4)
        self.assertEqual(self.exchange.cancel_calls, [])
        self.assertEqual(len(self.exchange.orders), before)
        self.assertIsNone(self.bot.database.get_state('grid_reset'))

    def test_accepted_new_grid_activates_cap_and_retains_reserved_btc(self):
        self.bot.request_manual_recenter('100', '20', '30', '2000', 4)
        self.assertTrue(json.loads(self.bot.database.get_state('grid_reset'))['total_allocation_cap'])
        self.bot.reset_grid()
        self.assertEqual(self.bot.database.get_state('total_allocation_cap'), '1')
        self.assertEqual(self.bot.baseline_base, D(1))
        cost = tracked_position_cost(self.bot.database) + pending_buy_commitment(self.bot.database)
        self.assertLessEqual(cost, self.bot.config.investment_quote)

    def test_runtime_cap_blocks_new_buy_without_creating_intent(self):
        self.bot.database.set_state('total_allocation_cap', '1')
        before = len(self.bot.database.fetch_all_orders())
        with self.assertRaisesRegex(GridSizingError, 'Total allocation cap exceeded'):
            self.bot._submit_order(99, 'BUY', D(90), D(5))
        self.assertEqual(len(self.bot.database.fetch_all_orders()), before)

    def test_fill_during_cancel_rechecks_cap_and_keeps_hard_stop_running(self):
        self.bot.request_manual_recenter('100', '20', '30', '1500', 4)
        before = len(self.exchange.orders)
        for row in self.bot.database.fetch_active_grids():
            if row['side'] == 'BUY':
                self.exchange.fill(row['client_order_id'])
        self.assertFalse(self.bot.reset_grid())
        self.assertEqual(len(self.exchange.orders), before)
        self.assertEqual(self.bot.database.get_state('safety_mode'), 'PAUSED_SIZING')
        self.assertEqual(self.bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_RUNNING)
        self.exchange.price = D(69)
        self.bot.run_cycle()
        self.assertEqual(self.exchange.market_sell_calls, 1)
        self.assertEqual(self.exchange.base_free, D(1))

    def test_start_rechecks_carried_position_before_rebuild(self):
        self.bot.database.set_state('total_allocation_cap', '1')
        for row in self.bot.database.fetch_active_grids():
            if row['side'] == 'BUY':
                self.exchange.fill(row['client_order_id'])
        self.bot.run_cycle()
        self.bot.stop_engine()
        before = len(self.exchange.orders)
        with self.assertRaisesRegex(GridSizingError, 'Total allocation cap exceeded'):
            self.bot.start_engine(auto_cover_inventory=True)
        self.assertEqual(self.bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
        self.assertEqual(len(self.exchange.orders), before)

    def test_cap_uses_total_cost_and_partial_remaining_commitments(self):
        require_total_cap(self.bot.config, D(500), D(500))
        with self.assertRaises(GridSizingError):
            require_total_cap(self.bot.config, D(501), D(500))
        row = next(r for r in self.bot.database.fetch_active_grids() if r['side'] == 'BUY')
        before = pending_buy_commitment(self.bot.database)
        self.bot.database.set_state('fill_snapshot:' + row['order_id'], json.dumps({'filled_base':'0.1'}))
        self.assertEqual(pending_buy_commitment(self.bot.database), before - D('0.1') * D(row['price']))
