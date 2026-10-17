"""Manual risk exits against a deterministic exchange and real SQLite."""
import asyncio
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import ccxt
import httpx

import api
import engine
from database import GridDatabase
from tests.fakes import OfflineGridBot as GridBot
from engine import _reconcile_service_boot
from models import RunnerLeaseLost, TradingHalt
from settings import (
    ENGINE_HALTED, ENGINE_IDLE, ENGINE_RUNNING, ENGINE_STATUS_KEY,
    LAST_MARKET_PRICE_KEY, LIQUIDATED, LIQUIDATING, LIQUIDATION_HALTED,
    LIQUIDATION_KEY, SAFETY_MODE_KEY,
)
from tests.fakes import make_bot
from tests.test_fault_injections import NoFillExchange, SimulatedCrash


class ManualLiquidationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'grid_testnet.sqlite'
        alerts = patch.object(engine, 'schedule_telegram_alert', return_value=False)
        self.alerts = alerts.start()
        self.addCleanup(alerts.stop)

    def live_bot(self):
        bot, exchange = make_bot(self.path)
        bot.run_cycle()
        bot.run_cycle()
        bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        bot.database.set_state(LAST_MARKET_PRICE_KEY, str(exchange.price))
        self.assertTrue(exchange.fetch_open_orders())
        return bot, exchange

    def assert_completed(self, bot, exchange, result):
        self.assertTrue(result['completed'])
        self.assertEqual(result['engine_status'], ENGINE_HALTED)
        self.assertEqual(result['mode'], LIQUIDATED)
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(Decimal(result['residual_base']), 0)
        self.assertEqual(exchange.fetch_open_orders(), [])
        self.assertEqual(bot.database.fetch_active_grids(), [])
        self.assertEqual(exchange.base_free, Decimal('1'))  # Pre-grid BTC is preserved.

    def test_manual_exit_above_stop_cancels_sells_halts_and_is_idempotent(self):
        bot, exchange = self.live_bot()
        self.assertGreater(exchange.price, bot.config.stop_loss_price)
        stop = bot.config.stop_loss_price
        # The liquidation must own the cycle lock and commit its halt intent before HTTP.
        original = exchange.create_market_sell_order

        def sell(*args, **kwargs):
            self.assertTrue(bot._cycle_lock._is_owned())
            self.assertFalse(bot.database.in_transaction)
            self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATING)
            self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_HALTED)
            self.assertEqual(exchange.fetch_open_orders(), [])
            return original(*args, **kwargs)

        with patch.object(exchange, 'create_market_sell_order', side_effect=sell):
            result = bot.force_liquidate()
        self.assert_completed(bot, exchange, result)
        self.assertEqual(bot.config.stop_loss_price, stop)
        self.assertEqual(exchange.market_sell_calls, 1)
        self.assertEqual(len(bot.database.fetch_trade_history()), 1)
        journal = bot.database.fetch_transition_journal()
        self.assertTrue(any(row['trigger_event'] == 'MANUAL_PANIC_LIQUIDATION' for row in journal))
        self.assertTrue(any('MANUAL PANIC' in call.args[0] for call in self.alerts.call_args_list))
        self.assertFalse(any('HARD STOP TRIGGERED' in call.args[0] for call in self.alerts.call_args_list))
        self.assertEqual(bot.force_liquidate(), result)
        self.assertEqual(exchange.market_sell_calls, 1)
        self.assertEqual(bot.database.fetch_transition_journal(), journal)
        with self.assertRaises(TradingHalt):
            bot.start_engine()

    def test_stopped_bot_can_panic_without_preparing_price_bounds(self):
        bot, exchange = self.live_bot()
        bot.stop_engine()
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
        # Reopen as an IDLE ready-mode service, with no in-memory market/baseline.
        reopened = GridBot(bot.config, exchange, GridDatabase(self.path))
        exchange.price = Decimal('200')  # Outside the configured grid.
        result = reopened.force_liquidate()
        self.assert_completed(reopened, exchange, result)

    def test_ticker_failure_does_not_block_manual_risk_exit(self):
        bot, exchange = self.live_bot()
        with patch.object(exchange, 'fetch_ticker', side_effect=ccxt.RequestTimeout('public ticker unavailable')):
            self.assert_completed(bot, exchange, bot.force_liquidate())

    def test_ambiguous_market_response_is_reconciled_without_duplicate_sell(self):
        bot, exchange = self.live_bot()
        exchange.market_sell_timeout = 'after'
        with patch.object(engine.time, 'sleep'):
            result = bot.force_liquidate()
        self.assert_completed(bot, exchange, result)
        self.assertEqual(exchange.market_sell_calls, 1)

    def test_safety_paused_bot_retries_cancel_timeout_before_selling(self):
        bot, exchange = self.live_bot()
        bot.set_manual_pause(True)
        exchange.cancel_timeout = 'before'
        with patch.object(engine.time, 'sleep'):
            self.assert_completed(bot, exchange, bot.force_liquidate())
        self.assertEqual(exchange.market_sell_calls, 1)

    def test_unfilled_manual_market_exit_stays_liquidating_until_verified(self):
        exchange = NoFillExchange()
        bot, _ = make_bot(self.path, exchange=exchange)
        bot.run_cycle()
        bot.run_cycle()
        bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        with patch.object(engine.time, 'sleep', side_effect=SimulatedCrash()):
            with self.assertRaises(SimulatedCrash):
                bot.force_liquidate()
        self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATING)
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_HALTED)
        state = json.loads(bot.database.get_state(LIQUIDATION_KEY))
        self.assertEqual(state['phase'], 'confirming')
        self.assertEqual(bot.database.fetch_trade_history(), [])
        exchange.release_fill(state['client_order_id'], Decimal('80'))
        result = bot.force_liquidate()
        self.assert_completed(bot, exchange, result)
        self.assertEqual(exchange.market_sell_calls, 1)

    def test_interrupted_manual_exit_resumes_on_ready_restart(self):
        bot, exchange = self.live_bot()
        exchange.market_sell_crash_after_accept = True
        with self.assertRaises(KeyboardInterrupt):
            bot.force_liquidate()
        persisted = json.loads(bot.database.get_state(LIQUIDATION_KEY))
        self.assertEqual(persisted['trigger_event'], 'MANUAL_PANIC_LIQUIDATION')
        self.assertEqual(persisted['phase'], 'submitting')
        reopened = GridBot(bot.config, exchange, GridDatabase(self.path))
        reopened.ready_mode = True
        asyncio.run(_reconcile_service_boot(reopened, ready=True))
        self.assertEqual(reopened.database.get_state(ENGINE_STATUS_KEY), ENGINE_RUNNING)
        reopened.prepare(persist=False)
        reopened.run_cycle()
        self.assert_completed(reopened, exchange, reopened.force_liquidate())
        self.assertEqual(exchange.market_sell_calls, 1)

    def test_permanent_exchange_rejection_does_not_claim_liquidated(self):
        bot, exchange = self.live_bot()
        with patch.object(exchange, 'create_market_sell_order', side_effect=ccxt.PermissionDenied('sale denied')):
            result = bot.force_liquidate()
        self.assertFalse(result['completed'])
        self.assertEqual(result['engine_status'], ENGINE_HALTED)
        self.assertEqual(result['mode'], LIQUIDATION_HALTED)
        self.assertGreater(exchange.base_free, 1)
        self.assertEqual(bot.database.fetch_trade_history(), [])
        with self.assertRaisesRegex(TradingHalt, 'unresolved'):
            bot.force_liquidate()

    def test_missing_baseline_or_lost_runner_cannot_sell_account_inventory(self):
        bot, exchange = self.live_bot()
        with patch.object(bot, 'assert_runner_lease', side_effect=RunnerLeaseLost('fenced')):
            with self.assertRaises(RunnerLeaseLost):
                bot.force_liquidate()
        run = json.loads(bot.database.get_state('grid_run'))
        run.pop('baseline_base')
        bot.database.set_state('grid_run', json.dumps(run))
        with self.assertRaisesRegex(TradingHalt, 'baseline'):
            bot.force_liquidate()
        self.assertEqual(exchange.market_sell_calls, 0)
        self.assertTrue(exchange.fetch_open_orders())


class ManualLiquidationApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_origin_confirmation_and_real_liquidation(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = make_bot(Path(directory) / 'grid_testnet.sqlite')
            saved = (api.app.state.grid_bot, api.app.state.ready)
            api.app.state.grid_bot, api.app.state.ready = bot, True
            try:
                with patch.dict(os.environ, {
                    'BOT_ADMIN_PASSWORD': 'private-panic-test-password',
                    'BOT_JWT_SECRET': 'private-panic-test-signing-secret-32-chars',
                }), patch.object(engine, 'schedule_telegram_alert', return_value=False):
                    bot.run_cycle()
                    bot.run_cycle()
                    bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                                 base_url='https://testserver') as client:
                        origin = {'Origin': 'http://localhost:5173'}
                        url = '/api/admin/force_liquidate'
                        denied = await client.post(url, headers=origin, json={'confirmed': True})
                        self.assertEqual(denied.status_code, 401)
                        await client.post('/api/auth/login', headers=origin,
                                          json={'password': 'private-panic-test-password'})
                        foreign = await client.post(url, headers={'Origin': 'https://other.test'}, json={'confirmed': True})
                        self.assertEqual(foreign.status_code, 403)
                        for body, code in (({}, 422), ({'confirmed': False}, 400), ({'confirmed': 'true'}, 422)):
                            self.assertEqual((await client.post(url, headers=origin, json=body)).status_code, code)
                        api.app.state.ready = False
                        offline = await client.post(url, headers=origin, json={'confirmed': True})
                        self.assertEqual(offline.status_code, 503)
                        api.app.state.ready = True
                        self.assertEqual(exchange.market_sell_calls, 0)
                        completed = await client.post(url, headers=origin, json={'confirmed': True})
                        self.assertEqual(completed.status_code, 200)
                        self.assertEqual(completed.headers['cache-control'], 'no-store')
                        self.assertEqual(completed.json()['engine_status'], ENGINE_HALTED)
                        self.assertEqual(completed.json()['mode'], LIQUIDATED)
                        self.assertTrue(completed.json()['completed'])
                        repeated = await client.post(url, headers=origin, json={'confirmed': True})
                        self.assertEqual(repeated.json(), completed.json())
                        self.assertEqual(exchange.market_sell_calls, 1)
                        with patch.object(bot, 'force_liquidate', return_value={
                            'completed': False, 'phase': 'failed', 'engine_status': ENGINE_HALTED,
                            'mode': LIQUIDATION_HALTED, 'residual_base': '0.00058',
                        }):
                            failed = await client.post(url, headers=origin, json={'confirmed': True})
                            self.assertEqual(failed.status_code, 409)
                            self.assertEqual(failed.json()['detail']['code'], 'LIQUIDATION_INCOMPLETE')
                            self.assertFalse(failed.json()['detail']['completed'])
            finally:
                api.app.state.grid_bot, api.app.state.ready = saved
