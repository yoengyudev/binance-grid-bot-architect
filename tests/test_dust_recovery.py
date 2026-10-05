"""Deterministic coverage for unsellable inventory and responsive controls."""
import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

import httpx
import api
import engine
from database import GridDatabase
from engine import GridBot, _reconcile_service_boot
from models import TradingHalt
from portfolio import _portfolio_wallet
from settings import ENGINE_HALTED, ENGINE_IDLE, ENGINE_STATUS_KEY, ENGINE_STOP_CLEANUP_KEY, LIQUIDATION_KEY, SAFETY_MODE_KEY
from tests.fakes import make_bot


class DustRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'grid_testnet.sqlite'
        alerts = patch.object(engine, 'schedule_telegram_alert', return_value=False)
        alerts.start()
        self.addCleanup(alerts.stop)

    def dust_bot(self):
        bot, exchange = make_bot(self.path, initial_base=Decimal('0'))
        bot.ready_mode = True
        bot._submit_order(0, 'BUY', Decimal('100'), Decimal('0.1'), order_type='MARKET')
        exchange.price = Decimal('69')
        with patch.object(engine.time, 'sleep') as sleep:
            bot.run_cycle()
            sleep.assert_not_called()
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_HALTED)
        self.assertEqual(json.loads(bot.database.get_state(LIQUIDATION_KEY))['failure_type'], 'LiquidationDust')
        return bot, exchange

    def token(self, bot):
        return hashlib.sha256(bot.database.get_state(LIQUIDATION_KEY).encode()).hexdigest()

    def test_dust_halts_without_retry_loop_and_stop_preserves_inventory(self):
        bot, exchange = self.dust_bot()
        before = bot.database.get_state(LIQUIDATION_KEY)
        self.assertEqual(_portfolio_wallet(bot.database)['btc_held'], 0.1)
        self.assertEqual(bot.stop_engine()['engine_status'], ENGINE_HALTED)
        self.assertEqual(bot.database.get_state(LIQUIDATION_KEY), before)
        self.assertEqual(exchange.market_sell_calls, 0)
        self.assertEqual(exchange.base_free, Decimal('0.1'))

    def test_stop_lock_contention_returns_before_proxy_timeout(self):
        bot, _ = self.dust_bot()
        acquired, release = Event(), Event()
        def hold_lock():
            with bot._cycle_lock:
                acquired.set()
                release.wait(5)
        worker = Thread(target=hold_lock)
        worker.start()
        self.assertTrue(acquired.wait(2))
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(TradingHalt, 'STOP was not applied'):
                bot.stop_engine()
            self.assertLess(time.monotonic() - started, 3)
        finally:
            release.set()
            worker.join(2)

    def test_dust_acknowledgment_records_cost_and_requires_separate_start(self):
        bot, exchange = self.dust_bot()
        original_orders = set(exchange.orders)
        with self.assertRaisesRegex(TradingHalt, 'changed'):
            bot.acknowledge_dust('0' * 64)
        result = bot.acknowledge_dust(self.token(bot))
        self.assertEqual(result['engine_status'], ENGINE_HALTED)
        reserve = bot.database.fetch_dust_reserves()[0]
        self.assertEqual(Decimal(reserve['quantity']), Decimal('0.1'))
        self.assertEqual(Decimal(reserve['cost_basis_quote']), Decimal('10'))
        self.assertEqual(reserve['status'], 'UNMONITORED')
        self.assertEqual(exchange.base_free, Decimal('0.1'))
        self.assertEqual(_portfolio_wallet(bot.database)['btc_held'], 0)
        self.assertEqual(set(exchange.orders), original_orders)
        bot._assert_liquidation_reset_ready()
        bot.request_manual_recenter('69', '20', '30', '1000', 10)
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
        self.assertEqual(set(exchange.orders), original_orders)
        self.assertEqual(bot.baseline_base, Decimal('0.1'))
        self.assertEqual(bot.database.fetch_dust_reserves()[0], reserve)
        self.assertEqual(bot.database.fetch_all_orders(), [])
        token = hashlib.sha256(bot.database.get_state('grid_reset').encode()).hexdigest()
        bot.start_engine(pending_grid_token=token)
        bot.reset_grid()
        self.assertEqual(bot.baseline_base, Decimal('0.1'))
        self.assertEqual(bot.database.fetch_dust_reserves()[0], reserve)
        self.assertGreater(bot._bot_btc_balance(), 0)

    def test_restart_preserves_halt_and_acknowledged_reserve(self):
        bot, exchange = self.dust_bot()
        restarted = GridBot(bot.config, exchange, GridDatabase(self.path))
        restarted.ready_mode = True
        asyncio.run(_reconcile_service_boot(restarted, ready=True))
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_HALTED)
        self.assertIsNone(bot.database.get_state(ENGINE_STOP_CLEANUP_KEY))
        restarted.acknowledge_dust(self.token(bot))
        reserves = bot.database.fetch_dust_reserves()
        again = GridBot(bot.config, exchange, GridDatabase(self.path))
        again.ready_mode = True
        asyncio.run(_reconcile_service_boot(again, ready=True))
        again._assert_liquidation_reset_ready()
        self.assertEqual(again.database.fetch_dust_reserves(), reserves)

    def test_sellable_inventory_and_unknown_faults_cannot_be_set_aside(self):
        bot, exchange = self.dust_bot()
        exchange.price = Decimal('200')
        with self.assertRaisesRegex(TradingHalt, 'sellable'):
            bot.acknowledge_dust(self.token(bot))
        exchange.price = Decimal('69')
        state = json.loads(bot.database.get_state(LIQUIDATION_KEY))
        state['failure_type'] = 'RequestTimeout'
        bot.database.set_state(LIQUIDATION_KEY, json.dumps(state))
        with self.assertRaisesRegex(TradingHalt, 'Only a verified'):
            bot.acknowledge_dust(self.token(bot))
        self.assertEqual(bot.database.fetch_dust_reserves(), [])

    def test_acknowledged_dust_is_not_liquidated_or_lost_on_factory_reset(self):
        bot, exchange = self.dust_bot()
        bot.acknowledge_dust(self.token(bot))
        reserves = bot.database.fetch_dust_reserves()
        bot.liquidate(Decimal('1'))
        self.assertEqual(json.loads(bot.database.get_state(LIQUIDATION_KEY))['phase'], 'dust_acknowledged')
        with self.assertRaisesRegex(TradingHalt, 'outside this grid'):
            bot.force_liquidate()
        bot.factory_reset()
        self.assertEqual(bot.database.fetch_dust_reserves(), reserves)
        self.assertEqual(exchange.base_free, Decimal('0.1'))
        self.assertEqual(exchange.market_sell_calls, 0)

    def test_missing_reserve_cannot_falsely_clear_inventory_or_open_configuration(self):
        bot, _ = self.dust_bot()
        state = json.loads(bot.database.get_state(LIQUIDATION_KEY))
        state.update(phase='dust_acknowledged', dust_reserve_id=1)
        bot.database.set_state(LIQUIDATION_KEY, json.dumps(state))
        bot.database.set_state(SAFETY_MODE_KEY, 'LIQUIDATED')
        bot.database.clear_state('halt_reason')
        bot.database.clear_state('hard_stop_alarm')
        self.assertIsNone(_portfolio_wallet(bot.database)['btc_held'])
        with self.assertRaisesRegex(TradingHalt, 'unresolved'):
            bot._assert_liquidation_reset_ready()

    def test_unknown_balance_and_cleanup_flags_cannot_unlock(self):
        bot, exchange = self.dust_bot()
        with patch.object(exchange, 'fetch_balance', return_value={}):
            with self.assertRaisesRegex(TradingHalt, 'unavailable'):
                bot.acknowledge_dust(self.token(bot))
        bot.database.set_state(ENGINE_STOP_CLEANUP_KEY, '1')
        with self.assertRaisesRegex(TradingHalt, 'cleanup'):
            bot.acknowledge_dust(self.token(bot))
        self.assertEqual(bot.database.fetch_dust_reserves(), [])

    def test_acknowledgment_is_atomic_under_write_failure(self):
        bot, _ = self.dust_bot()
        original = bot.database.get_state(LIQUIDATION_KEY)
        write = bot.database.set_state
        def fail(key, value, **kwargs):
            if key == LIQUIDATION_KEY:
                raise RuntimeError('injected disk failure')
            return write(key, value, **kwargs)
        with patch.object(bot.database, 'set_state', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'disk failure'):
                bot.acknowledge_dust(self.token(bot))
        self.assertEqual(bot.database.fetch_dust_reserves(), [])
        self.assertEqual(bot.database.get_state(LIQUIDATION_KEY), original)
        self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY), 'HALTED')

    def test_partial_market_sale_records_remaining_cost_without_second_sale(self):
        bot, exchange = make_bot(self.path, initial_base=Decimal('0'))
        bot.run_cycle()
        bot.run_cycle()
        sell = exchange.create_market_sell_order
        def leave_dust(*args, **kwargs):
            order = sell(*args, **kwargs)
            residual = Decimal('0.1')
            sold = Decimal(order['filled']) - residual
            exchange.base_free += residual
            exchange.quote_free -= residual * exchange.price
            raw = exchange.orders[order['clientOrderId']]
            raw.update(status='canceled', filled=str(sold), cost=str(sold * exchange.price))
            return dict(raw)
        exchange.price = Decimal('69')
        with patch.object(exchange, 'create_market_sell_order', side_effect=leave_dust):
            bot.run_cycle()
        self.assertEqual(exchange.market_sell_calls, 1)
        state = json.loads(bot.database.get_state(LIQUIDATION_KEY))
        self.assertEqual(Decimal(state['sold_base']), Decimal('4.9'))
        bot.acknowledge_dust(self.token(bot))
        self.assertEqual(Decimal(bot.database.fetch_dust_reserves()[0]['cost_basis_quote']), 10)
        self.assertEqual(len(bot.database.fetch_trade_history()), 1)
        self.assertEqual(exchange.market_sell_calls, 1)

    def test_api_auth_origin_strict_confirmation_and_stale_review(self):
        bot, exchange = self.dust_bot()
        previous = api.app.state.grid_bot, api.app.state.ready, api.app.state.standby
        api.app.state.grid_bot, api.app.state.ready, api.app.state.standby = bot, True, False
        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='https://testserver') as client:
                origin = {'Origin': 'http://localhost:5173'}
                url = '/api/admin/acknowledge-dust'
                body = {'confirmed': True, 'recovery_token': self.token(bot)}
                self.assertEqual((await client.post(url, headers=origin, json=body)).status_code, 401)
                await client.post('/api/auth/login', headers=origin, json={'password': 'dust-test-password'})
                self.assertEqual((await client.post(url, headers={'Origin': 'https://foreign.invalid'}, json=body)).status_code, 403)
                for confirmed, expected in ((False, 400), ('true', 422)):
                    self.assertEqual((await client.post(url, headers=origin, json={**body, 'confirmed': confirmed})).status_code, expected)
                status = (await client.get('/api/bot/status')).json()
                self.assertTrue(status['dust_recovery']['can_acknowledge'])
                self.assertEqual(status['wallet']['btc_held'], 0.1)
                self.assertEqual((await client.post('/api/engine/stop', headers=origin)).status_code, 200)
                self.assertEqual((await client.post(url, headers=origin, json=body)).status_code, 200)
                self.assertEqual((await client.post(url, headers=origin, json=body)).status_code, 409)
                status = (await client.get('/api/bot/status')).json()
                self.assertTrue(status['can_reset_liquidation'])
                self.assertEqual(len(status['dust_reserves']), 1)
                self.assertEqual(exchange.market_sell_calls, 0)
        try:
            with patch.dict(os.environ, {'BOT_ADMIN_PASSWORD': 'dust-test-password', 'BOT_JWT_SECRET': 'dust-test-private-signing-secret-32-chars'}):
                asyncio.run(exercise())
        finally:
            api.app.state.grid_bot, api.app.state.ready, api.app.state.standby = previous
