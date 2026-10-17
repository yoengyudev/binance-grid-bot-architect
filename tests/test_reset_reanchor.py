"""A new grid releases a reset lock only after verified, atomic validation."""

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from unittest.mock import patch

import ccxt
import httpx

from database import GridDatabase
from tests.fakes import OfflineGridBot as GridBot
from engine import _reconcile_service_boot
from models import GridConfig, GridSizingError, TradingHalt
from settings import (
    BREAKOUT_TIMER_KEY, ENGINE_FAULT_KEY, ENGINE_HALTED, ENGINE_IDLE,
    ENGINE_RUNNING, ENGINE_STATUS_KEY, ENGINE_STOP_CLEANUP_KEY,
    GHOST_ORDER_ALARM_KEY, LIQUIDATION_ALARM_KEY, LIQUIDATION_KEY,
    ORDER_CLEANUP_ALARM_KEY, PAUSED_MANUAL, PAUSED_RESET, RISK_OVERRIDE_DENIED,
    SAFETY_MODE_KEY, SAFETY_PAUSE_NOTICE_KEY, TRAILING_STOP_KEY,
)
from tests.fakes import FakeSpotExchange, make_bot, restore_retained_floor


class ResetReanchorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "grid_testnet.sqlite"
        self.bot, self.exchange = make_bot(self.path, initial_base=Decimal("0"))
        self.bot.run_cycle()
        self.bot.run_cycle()
        self.bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        self.bot.prepare_factory_reset()
        self.bot.stop_engine()

    def queue(self, **kwargs):
        return self.bot.request_manual_recenter(
            kwargs.get("center", "100"), kwargs.get("width", "20"),
            kwargs.get("stop", "30"), "1000", 10,
        )

    def assert_locked(self):
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY), PAUSED_RESET)
        self.assertIsNone(self.bot.database.get_state("grid_reset"))
        self.assertTrue(self.bot.is_paused)
        self.assertFalse(self.bot.grid_needs_reset)

    def test_queue_releases_lock_but_requires_reviewed_start(self):
        manual = self.exchange.create_order(
            "BTC/USDT", "limit", "buy", 0.2, 50,
            {"newClientOrderId": "manual-reset-order"},
        )
        self.bot.database.set_state(SAFETY_PAUSE_NOTICE_KEY, "1")
        next_id = self.exchange.next_id
        run_before = self.bot.database.get_state("grid_run")
        orders_before = self.bot.database.fetch_all_orders()

        self.assertEqual(self.queue(), (Decimal("80"), Decimal("120")))

        saved = self.bot.database.get_state("grid_reset")
        self.assertEqual(json.loads(saved)["buy_levels"], 5)
        self.assertIsNone(self.bot.database.get_state(SAFETY_MODE_KEY))
        self.assertIsNone(self.bot.database.get_state(SAFETY_PAUSE_NOTICE_KEY))
        self.assertEqual(self.bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
        self.assertFalse(self.bot.is_paused)
        self.assertTrue(self.bot.grid_needs_reset)
        self.assertEqual(self.exchange.next_id, next_id)
        self.assertEqual(self.bot.database.get_state("grid_run"), run_before)
        self.assertEqual(self.bot.database.fetch_all_orders(), orders_before)
        self.assertEqual(self.exchange.fetch_open_orders(), [manual])
        event = self.bot.database.fetch_transition_journal()[-1]
        self.assertEqual((event["previous_state"], event["new_state"], event["trigger_event"]),
                         (PAUSED_RESET, "NORMAL", "RESET_REANCHOR_QUEUED"))

        # A process restart restores the pending request, not an active engine.
        reopened = GridBot(self.bot.config, self.exchange, GridDatabase(self.path))
        reopened.ready_mode = True
        reopened.prepare(persist=False)
        self.assertFalse(reopened.is_paused)
        self.assertTrue(reopened.grid_needs_reset)
        with self.assertRaisesRegex(TradingHalt, "Review the saved pending grid"):
            reopened.start_engine(auto_cover_inventory=True)
        token = hashlib.sha256(saved.encode("utf-8")).hexdigest()
        result = reopened.start_engine(auto_cover_inventory=True, pending_grid_token=token)
        self.assertEqual(result["engine_status"], ENGINE_RUNNING)
        self.assertEqual(self.exchange.next_id, next_id)

    def test_running_or_halted_engine_cannot_unlock(self):
        for state in (ENGINE_RUNNING, ENGINE_HALTED):
            with self.subTest(state=state):
                self.bot.database.set_state(ENGINE_STATUS_KEY, state)
                with self.assertRaisesRegex(TradingHalt, "STOP the engine"):
                    self.queue()
                self.assert_locked()

    def test_ready_restart_reanchors_before_prepare_and_restores_saved_risk_state(self):
        reopened = GridBot(self.bot.config, self.exchange, GridDatabase(self.path))
        reopened.ready_mode = True
        self.assertIsNone(reopened.high_water_mark)
        self.assertIsNone(reopened.baseline_base)
        asyncio.run(_reconcile_service_boot(reopened, ready=True))
        next_id = self.exchange.next_id

        # This is the production ready-mode path: no prepare() before the API call.
        reopened.request_manual_recenter('100', '20', '30', '1000', 10)
        self.assertEqual(reopened.high_water_mark, Decimal('100'))
        self.assertEqual(reopened.stop_loss_distance, Decimal('30'))
        self.assertEqual(reopened.config.stop_loss_price, Decimal('70'))
        self.assertIsNone(reopened.database.get_state(SAFETY_MODE_KEY))
        self.assertEqual(reopened.database.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
        self.assertEqual(self.exchange.next_id, next_id)

        saved = reopened.database.get_state('grid_reset')
        token = hashlib.sha256(saved.encode('utf-8')).hexdigest()
        reopened.start_engine(auto_cover_inventory=True, pending_grid_token=token)
        self.assertTrue(reopened.reset_grid())
        reopened.run_cycle()
        reopened.run_cycle()
        self.assertEqual(reopened.database.get_state(ENGINE_STATUS_KEY), ENGINE_RUNNING)
        self.assertIsNone(reopened.database.get_state('grid_reset'))
        self.assertEqual(len(reopened.database.fetch_active_grids()), 10)

    def test_ready_restart_rejects_missing_or_corrupt_saved_trailing_state(self):
        original = self.bot.database.get_state(TRAILING_STOP_KEY)
        for value in (None, '{broken', '{"high_water_mark":"100","stop_loss_distance":"20"}'):
            with self.subTest(value=value):
                if value is None:
                    self.bot.database.clear_state(TRAILING_STOP_KEY)
                else:
                    self.bot.database.set_state(TRAILING_STOP_KEY, value)
                reopened = GridBot(self.bot.config, self.exchange, GridDatabase(self.path))
                reopened.ready_mode = True
                asyncio.run(_reconcile_service_boot(reopened, ready=True))
                with self.assertRaises(TradingHalt):
                    reopened.request_manual_recenter('100', '20', '30', '1000', 10)
                self.assertEqual(reopened.database.get_state(SAFETY_MODE_KEY), PAUSED_RESET)
                self.assertIsNone(reopened.database.get_state('grid_reset'))
                self.assertIsNone(reopened.high_water_mark)
        self.bot.database.set_state(TRAILING_STOP_KEY, original)

    def test_ready_restart_reanchor_keeps_retained_floor_monotonic(self):
        reopened = GridBot(self.bot.config, self.exchange, GridDatabase(self.path))
        reopened.ready_mode = True
        asyncio.run(_reconcile_service_boot(reopened, ready=True))
        self.exchange.price = Decimal('110')
        restore_retained_floor(reopened, 80, 110)
        with self.assertRaisesRegex(ValueError, RISK_OVERRIDE_DENIED):
            reopened.request_manual_recenter('110', '20', '30', '1000', 10)
        self.assertEqual(reopened.high_water_mark, Decimal('110'))
        self.assertEqual(reopened.stop_loss_distance, Decimal('30'))
        self.assertEqual(reopened.config.stop_loss_price, Decimal('80'))
        self.assertIsNone(reopened.database.get_state('grid_reset'))
        self.assertEqual(reopened.database.get_state(SAFETY_MODE_KEY), PAUSED_RESET)

        reopened.request_manual_recenter('110', '20', '25', '1000', 10)
        pending = json.loads(reopened.database.get_state('grid_reset'))
        self.assertEqual(Decimal(pending['stop_loss_price']), Decimal('82.5'))
        self.assertEqual(reopened.config.stop_loss_price, Decimal('80'))
        self.assertEqual(reopened.database.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)

    def test_pending_cleanup_or_fault_cannot_unlock(self):
        for key in (ENGINE_STOP_CLEANUP_KEY, ENGINE_FAULT_KEY, "halt_reason",
                    ORDER_CLEANUP_ALARM_KEY, GHOST_ORDER_ALARM_KEY,
                    LIQUIDATION_KEY, LIQUIDATION_ALARM_KEY):
            with self.subTest(key=key):
                self.bot.database.set_state(key, "1")
                with self.assertRaises(TradingHalt):
                    self.queue()
                self.assert_locked()
                self.bot.database.clear_state(key)

    def test_active_ledger_orders_cannot_unlock(self):
        with patch.object(self.bot.database, "fetch_active_grids", return_value=[{"id": "pending"}]):
            with self.assertRaisesRegex(TradingHalt, "cleanup is incomplete"):
                self.queue()
        self.assert_locked()

    def test_bot_order_on_another_symbol_cannot_unlock(self):
        self.exchange.create_order(
            "ETH/USDT", "limit", "buy", 0.2, 50,
            {"newClientOrderId": self.bot.order_client_prefix + "-ghost"},
        )
        with self.assertRaisesRegex(TradingHalt, "Bot orders remain on Binance"):
            self.queue()
        self.assert_locked()

    def test_failed_or_malformed_exchange_verification_cannot_unlock(self):
        for response in (ccxt.RequestTimeout("audit timed out"), [None]):
            with self.subTest(response=response):
                if isinstance(response, Exception):
                    mocked = patch.object(self.bot, "_full_account_open_orders", side_effect=response)
                    expected = ccxt.RequestTimeout
                else:
                    mocked = patch.object(self.bot, "_full_account_open_orders", return_value=response)
                    expected = TradingHalt
                with mocked, self.assertRaises(expected):
                    self.queue()
                self.assert_locked()

    def test_invalid_grid_or_lower_hard_stop_cannot_unlock(self):
        with self.assertRaisesRegex(ValueError, RISK_OVERRIDE_DENIED):
            self.queue(stop="31")
        self.assert_locked()
        with self.assertRaisesRegex(ValueError, "Center must be close"):
            self.queue(center="120", width="10", stop="30")
        self.assert_locked()

    def test_validation_failure_keeps_existing_pending_grid(self):
        existing = json.dumps({"phase": "canceling", "source": "previous"})
        self.bot.database.set_state("grid_reset", existing)
        with self.assertRaises(ValueError):
            self.queue(stop="31")
        self.assertEqual(self.bot.database.get_state("grid_reset"), existing)
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY), PAUSED_RESET)

    def test_other_pause_modes_are_not_released(self):
        self.bot.database.set_state(SAFETY_MODE_KEY, PAUSED_MANUAL)
        with self.assertRaisesRegex(TradingHalt, "Release Safety Pause"):
            self.queue()
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY), PAUSED_MANUAL)
        self.assertIsNone(self.bot.database.get_state("grid_reset"))

    def test_state_change_during_validation_cannot_unlock(self):
        real_check = self.bot._check_grid_buy_capital

        def concurrent_pause(*args, **kwargs):
            real_check(*args, **kwargs)
            self.bot.database.set_state(SAFETY_MODE_KEY, PAUSED_MANUAL)

        with patch.object(self.bot, "_check_grid_buy_capital", side_effect=concurrent_pause):
            with self.assertRaisesRegex(TradingHalt, "Safety mode changed"):
                self.queue()
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY), PAUSED_MANUAL)
        self.assertIsNone(self.bot.database.get_state("grid_reset"))
        self.assertTrue(self.bot.is_paused)

    def test_journal_failure_rolls_back_grid_and_unlock_together(self):
        self.bot.database.set_state(BREAKOUT_TIMER_KEY, "keep-me")
        self.bot.database.set_state(SAFETY_PAUSE_NOTICE_KEY, "1")
        before = self.bot.database.fetch_transition_journal()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""
                CREATE TRIGGER fail_reset_unlock BEFORE INSERT ON transition_journal
                WHEN NEW.trigger_event = 'RESET_REANCHOR_QUEUED'
                BEGIN SELECT RAISE(ABORT, 'simulated journal failure'); END
            """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "simulated journal failure"):
            self.queue()

        recovered = GridDatabase(self.path)
        self.assert_locked()
        self.assertEqual(recovered.get_state(SAFETY_MODE_KEY), PAUSED_RESET)
        self.assertIsNone(recovered.get_state("grid_reset"))
        self.assertEqual(recovered.get_state(BREAKOUT_TIMER_KEY), "keep-me")
        self.assertEqual(recovered.get_state(SAFETY_PAUSE_NOTICE_KEY), "1")
        self.assertEqual(recovered.fetch_transition_journal(), before)

    def test_api_reanchor_unlocks_and_status_exposes_reviewable_grid(self):
        import api
        from settings import FRONTEND_ORIGIN

        self.bot = GridBot(self.bot.config, self.exchange, GridDatabase(self.path))
        self.bot.ready_mode = True
        asyncio.run(_reconcile_service_boot(self.bot, ready=True))
        self.assertIsNone(self.bot.high_water_mark)

        async def exercise():
            previous_state = (api.app.state.grid_bot, api.app.state.standby,
                              api.app.state.monitor_only)
            previous_overrides = api.app.dependency_overrides.copy()
            api.app.state.grid_bot = self.bot
            api.app.state.standby = False
            api.app.state.monitor_only = False
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=api.app), base_url="http://testserver",
                ) as client:
                    payload = {"center_price": 100, "width_percentage": 20,
                               "stop_loss_percentage": 30, "allocated_capital": 1000,
                               "grid_levels": 10}
                    headers = {"Origin": FRONTEND_ORIGIN}
                    denied = await client.post("/api/bot/grid/recenter", json=payload, headers=headers)
                    self.assertEqual(denied.status_code, 401)
                    self.assert_locked()
                    api.app.dependency_overrides[api.get_current_user] = lambda: "admin"
                    self.bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
                    blocked = await client.post("/api/bot/grid/recenter", json=payload, headers=headers)
                    self.assertEqual(blocked.status_code, 409)
                    self.assert_locked()
                    self.bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_IDLE)
                    accepted = await client.post("/api/bot/grid/recenter", json=payload, headers=headers)
                    self.assertEqual(accepted.status_code, 202)
                    self.assertEqual(accepted.json()["lower_bound"], 80)
                    status = await client.get("/api/bot/status")
                    self.assertEqual(status.status_code, 200)
                    data = status.json()
                    self.assertIsNone(data["pause_mode"])
                    self.assertEqual(data["engine_status"], ENGINE_IDLE)
                    self.assertEqual(data["trading_state"], ENGINE_IDLE)
                    self.assertEqual(data["pending_grid"]["source"], "manual_recenter")
                    self.assertTrue(data["pending_grid"]["token"])
            finally:
                (api.app.state.grid_bot, api.app.state.standby,
                 api.app.state.monitor_only) = previous_state
                api.app.dependency_overrides.clear()
                api.app.dependency_overrides.update(previous_overrides)

        asyncio.run(exercise())

    def test_small_carried_btc_rejects_ten_levels_but_accepts_eight(self):
        class SpotPrecisionExchange(FakeSpotExchange):
            def market(self, symbol):
                market = super().market(symbol)
                market['limits'] = {'amount': {'min': 0.00001}, 'cost': {'min': 5}}
                return market

            def amount_to_precision(self, symbol, amount):
                return str(Decimal(amount).quantize(Decimal('0.00001'), rounding=ROUND_DOWN))

        with tempfile.TemporaryDirectory() as directory:
            exchange = SpotPrecisionExchange()
            exchange.price = Decimal('84812')
            exchange.base_free = Decimal(0)
            config = GridConfig(
                'BTC/USDT', Decimal('81.42'), Decimal('80050'), Decimal('88862.67'),
                Decimal('2.5'), Decimal('50'), Decimal('79789.7'), 2,
            )
            bot = GridBot(config, exchange, GridDatabase(Path(directory) / 'grid_testnet.sqlite'))
            bot.prepare(persist=True)
            bot.run_cycle()
            bot.run_cycle()
            bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
            bot.prepare_factory_reset()
            bot.stop_engine()
            self.assertEqual(exchange.base_free, Decimal('0.00048'))
            width = '5'
            stop = '5.8'
            for capital in ('150', '1500'):
                with self.subTest(capital=capital):
                    with self.assertRaisesRegex(GridSizingError, r'Existing BTC .*5 SELL levels'):
                        bot.request_manual_recenter('84812', width, stop, capital, 10)
                    self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY), PAUSED_RESET)
                    self.assertIsNone(bot.database.get_state('grid_reset'))
            # Re-open without prepare(), as EC2 does after a ready-mode restart.
            bot = GridBot(bot.config, exchange, GridDatabase(bot.database.path))
            bot.ready_mode = True
            asyncio.run(_reconcile_service_boot(bot, ready=True))
            next_id = exchange.next_id
            bot.request_manual_recenter('84812', width, stop, '150', 8)
            self.assertIsNone(bot.database.get_state(SAFETY_MODE_KEY))
            self.assertEqual(json.loads(bot.database.get_state('grid_reset'))['sell_levels'], 4)
            self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
            self.assertEqual(exchange.next_id, next_id)
            saved = bot.database.get_state('grid_reset')
            bot.start_engine(auto_cover_inventory=True,
                             pending_grid_token=hashlib.sha256(saved.encode('utf-8')).hexdigest())
            self.assertTrue(bot.reset_grid())
            bot.run_cycle()
            bot.run_cycle()
            self.assertEqual(len(bot.database.fetch_active_grids()), 8)
            self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_RUNNING)


if __name__ == "__main__":
    unittest.main()
