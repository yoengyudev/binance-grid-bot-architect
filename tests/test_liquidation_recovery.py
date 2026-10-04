"""Completed exits recover through a saved grid, never by bypassing START guards."""
import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx

import api
import engine
from models import TradingHalt
from settings import (
    ENGINE_FAULT_KEY, ENGINE_HALTED, ENGINE_IDLE, ENGINE_STATUS_KEY,
    ENGINE_STOP_CLEANUP_KEY, GHOST_ORDER_ALARM_KEY, LIQUIDATED,
    LIQUIDATION_ALARM_KEY, LIQUIDATION_KEY, ORDER_CLEANUP_ALARM_KEY,
    SAFETY_MODE_KEY,
)
from tests.fakes import make_bot


class LiquidationRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'grid_testnet.sqlite'
        for module in (api, engine):
            alerts = patch.object(module, 'schedule_telegram_alert', return_value=False)
            alerts.start()
            self.addCleanup(alerts.stop)
        self.bot, self.exchange = make_bot(self.path, initial_base=Decimal(0))
        self.bot.run_cycle()
        self.bot.run_cycle()
        self.exchange.price = Decimal('69')
        self.bot.run_cycle()
        self.bot.ready_mode = True
        self.exchange.price = Decimal('100')
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)

    def queue(self):
        return self.bot.request_manual_recenter('100', '20', '30', '1000', 10)

    def test_start_after_completed_exit_explains_new_grid_without_mutating_lock(self):
        before = self.bot.database.get_state(LIQUIDATION_KEY)
        next_id = self.exchange.next_id
        with self.assertRaisesRegex(TradingHalt, 'Hard-stop liquidation is complete. Save a new grid'):
            self.bot.start_engine()
        self.assertEqual(self.bot.database.get_state(LIQUIDATION_KEY), before)
        self.assertEqual(self.bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_HALTED)
        self.assertEqual(self.exchange.next_id, next_id)

    def test_unresolved_or_invalid_liquidation_cannot_be_reset(self):
        complete = self.bot.database.get_state(LIQUIDATION_KEY)
        for change in ({'phase': 'confirming'}, {'phase': 'failed'},
                       {'residual_base': '0.001'}, {'residual_base': 'NaN'}):
            with self.subTest(change=change):
                state = {**json.loads(complete), **change}
                self.bot.database.set_state(LIQUIDATION_KEY, json.dumps(state))
                with self.assertRaisesRegex(TradingHalt, 'unresolved'):
                    self.queue()
                self.assertEqual(self.bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_HALTED)
                self.assertIsNone(self.bot.database.get_state('grid_reset'))

    def test_fault_and_cleanup_flags_cannot_be_cleared_by_new_grid(self):
        for key in (ENGINE_FAULT_KEY, ORDER_CLEANUP_ALARM_KEY,
                    GHOST_ORDER_ALARM_KEY, LIQUIDATION_ALARM_KEY, ENGINE_STOP_CLEANUP_KEY):
            with self.subTest(key=key):
                self.bot.database.set_state(key, '1')
                with self.assertRaises(TradingHalt):
                    self.queue()
                self.assertEqual(self.bot.database.get_state(key), '1')
                self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)
                self.bot.database.clear_state(key)

    def test_live_bot_order_blocks_reset_even_when_local_completion_is_saved(self):
        self.exchange.create_order('BTC/USDT', 'limit', 'buy', 0.1, 90,
                                   {'newClientOrderId': self.bot.order_client_prefix + 'untracked'})
        with self.assertRaisesRegex(TradingHalt, 'Bot orders remain'):
            self.queue()
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)

    def test_failed_idle_commit_rolls_back_reset_and_preserves_history(self):
        before = self.bot.database.get_state(LIQUIDATION_KEY)
        orders = self.bot.database.fetch_all_orders()
        trades = self.bot.database.fetch_trade_history()
        journal = self.bot.database.fetch_transition_journal()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_idle BEFORE INSERT ON bot_state "
                "WHEN NEW.key='engine_status' AND NEW.value='IDLE' BEGIN "
                "SELECT RAISE(ABORT, 'injected commit failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.queue()
        self.assertEqual(self.bot.database.get_state(LIQUIDATION_KEY), before)
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)
        self.assertEqual(self.bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_HALTED)
        self.assertEqual(self.bot.database.fetch_all_orders(), orders)
        self.assertEqual(self.bot.database.fetch_trade_history(), trades)
        self.assertEqual(self.bot.database.fetch_transition_journal(), journal)
        self.assertFalse(self.bot.grid_needs_reset)

    def test_authenticated_save_preserves_history_and_requires_reviewed_start(self):
        fields = ('grid_bot', 'ready', 'standby')
        previous = {field: getattr(api.app.state, field) for field in fields}
        api.app.state.grid_bot = self.bot
        api.app.state.ready = True
        api.app.state.standby = False
        next_id = self.exchange.next_id
        trades = self.bot.database.fetch_trade_history()
        journal = self.bot.database.fetch_transition_journal()

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                         base_url='https://testserver') as client:
                headers = {'Origin': 'http://localhost:5173'}
                login = await client.post('/api/auth/login', headers=headers,
                                          json={'password': 'liquidation-recovery-test-password'})
                self.assertEqual(login.status_code, 200)
                before = (await client.get('/api/bot/status')).json()
                self.assertTrue(before['can_reset_liquidation'])
                blocked = await client.post('/api/engine/start', headers=headers)
                self.assertEqual(blocked.status_code, 409)
                self.assertIn('Save a new grid', blocked.json()['detail'])
                saved = await client.post('/api/bot/grid/recenter', headers=headers, json={
                    'center_price': 100, 'width_percentage': 20, 'stop_loss_percentage': 30,
                    'allocated_capital': 1000, 'grid_levels': 10,
                })
                self.assertEqual(saved.status_code, 202, saved.text)
                after = (await client.get('/api/bot/status')).json()
                self.assertEqual(after['engine_status'], ENGINE_IDLE)
                self.assertEqual(after['trading_state'], ENGINE_IDLE)
                self.assertFalse(after['can_reset_liquidation'])
                self.assertEqual(after['pending_grid']['source'], 'manual_liquidation_reset')
                self.assertEqual(Decimal(after['pending_grid']['center']), Decimal('100'))
                self.assertEqual(Decimal(after['pending_grid']['stop_loss']), Decimal('70'))
                self.assertEqual(self.exchange.next_id, next_id)
                self.assertEqual(self.bot.database.fetch_trade_history(), trades)
                self.assertEqual(self.bot.database.fetch_transition_journal()[:len(journal)], journal)
                self.assertEqual(self.exchange.fetch_open_orders('BTC/USDT'), [])
                denied = await client.post('/api/engine/start', headers=headers)
                self.assertEqual(denied.status_code, 409)
                self.assertIn('Review the saved pending grid', denied.json()['detail'])
                token = hashlib.sha256(self.bot.database.get_state('grid_reset').encode()).hexdigest()
                started = await client.post('/api/engine/start', headers=headers,
                                            json={'pending_grid_token': token})
                self.assertEqual(started.status_code, 200, started.text)
                self.assertEqual(started.json()['engine_status'], 'RUNNING')
                self.assertEqual(self.exchange.next_id, next_id)
        try:
            with patch.dict(os.environ, {'BOT_ADMIN_PASSWORD': 'liquidation-recovery-test-password',
                                        'BOT_JWT_SECRET': 'liquidation-recovery-test-signing-secret-32',
                                        'FRONTEND_URL': 'http://localhost:5173'}):
                asyncio.run(scenario())
        finally:
            for field, value in previous.items():
                setattr(api.app.state, field, value)
