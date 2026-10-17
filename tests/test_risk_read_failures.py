"""Durable price-breach verification without selling stale or reserved BTC."""
import asyncio
import json
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import ccxt
import engine
from database import GridDatabase
from exchange_handler import CircuitOpenError
from models import TradingHalt, RunnerLeaseLost
from portfolio import _portfolio_wallet
from settings import (ENGINE_RUNNING, ENGINE_HALTED, ENGINE_STATUS_KEY, SAFETY_MODE_KEY,
                      LIQUIDATING, LIQUIDATED, LIQUIDATION_KEY, LIQUIDATION_ALARM_KEY,
                      PAUSED_MANUAL)
from tests.fakes import make_bot, OfflineGridBot


class RiskReadFailureTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path=Path(directory.name)/'grid_testnet.sqlite'
        self.alerts=patch.object(engine,'schedule_telegram_alert',return_value=False).start()
        self.addCleanup(patch.stopall)
        patch.object(engine,'network_backoff',return_value=0).start()

    def grid(self):
        bot,exchange=make_bot(self.path,initial_base=D(1))
        bot.run_cycle();bot.run_cycle()
        bot.database.set_state(ENGINE_STATUS_KEY,ENGINE_RUNNING)
        return bot,exchange

    def pending(self,bot,exchange,error=None):
        exchange.price=D(69)
        attempts=[]
        def fetch_order(*args,**kwargs):
            attempts.append(1)
            raise error or ccxt.RequestTimeout('order endpoint unavailable')
        with patch.object(exchange,'fetch_order',fetch_order):
            with self.assertRaises(type(error) if error else ccxt.RequestTimeout):
                bot.run_cycle()
        self.assertTrue(attempts)
        state=json.loads(bot.database.get_state(LIQUIDATION_KEY))
        self.assertEqual(state['phase'],'verifying')
        self.assertEqual(state['trigger_price'],'69')
        self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY),LIQUIDATING)
        self.assertTrue(bot.database.get_state(LIQUIDATION_ALARM_KEY))
        self.assertEqual(exchange.market_sell_calls,0)
        self.assertEqual(exchange.cancel_calls,[])
        return state

    def fill_all_sells(self,bot,exchange):
        seed=bot.database.fetch_latest_orders_by_level()[0]
        order=exchange.orders[seed['client_order_id']]
        sells=[row for row in bot.database.fetch_active_grids() if row['side']=='SELL']
        fee=D(order['filled'])-sum((D(row['amount']) for row in sells),D(0))
        order['fees']=[{'currency':'BTC','cost':str(fee)}]
        exchange.base_free-=fee
        bot._save_fill_snapshot(seed['order_id'],order)
        for row in sells:
            exchange.fill(row['client_order_id'])

    def test_breached_stop_and_order_timeout_create_durable_verification(self):
        bot,exchange=self.grid()
        self.pending(bot,exchange)
        events=[row['trigger_event'] for row in bot.database.fetch_transition_journal()]
        self.assertIn('HARD_STOP_VERIFICATION_PENDING',events)
        self.assertTrue(bot.is_paused)

    def test_pending_verification_blocks_grid_writes_and_start(self):
        bot,exchange=self.grid()
        self.pending(bot,exchange)
        count=len(exchange.orders)
        for side in ('BUY','SELL'):
            with self.assertRaisesRegex(TradingHalt,'pending'):
                bot._submit_order(1,side,D(90),D(1))
        with self.assertRaisesRegex(TradingHalt,'finish before START'):
            bot.start_engine()
        self.assertEqual(len(exchange.orders),count)

    def test_repeated_failure_keeps_original_trigger_and_places_no_orders(self):
        bot,exchange=self.grid()
        original=self.pending(bot,exchange)
        exchange.price=D(100)  # A bounce must not erase an unverified breach.
        with patch.object(exchange,'fetch_order',side_effect=ccxt.RequestTimeout('still down')):
            with self.assertRaises(ccxt.RequestTimeout):
                bot.run_cycle()
        self.assertEqual(json.loads(bot.database.get_state(LIQUIDATION_KEY)),original)
        self.assertEqual(exchange.market_sell_calls,0)
        self.assertEqual(exchange.cancel_calls,[])
        self.assertIn('still down',bot.database.get_state(LIQUIDATION_ALARM_KEY))

    def test_recovery_prioritizes_verified_liquidation_even_if_ticker_and_audit_fail(self):
        bot,exchange=self.grid()
        self.pending(bot,exchange)
        exchange.price=D(100)
        with patch.object(exchange,'fetch_ticker',side_effect=ccxt.RequestTimeout('public API down')),patch.object(bot,'_audit_full_book_if_due',side_effect=AssertionError('Routine audit must wait')):
            bot.run_cycle()
        self.assertEqual(exchange.market_sell_calls,1)
        self.assertEqual(exchange.base_free,D(1))  # Reserved wallet BTC remains.
        self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY),LIQUIDATED)
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY),ENGINE_HALTED)

    def test_unobserved_final_sell_resolves_pending_flat_without_canceling_buys(self):
        bot,exchange=self.grid()
        buys={row['order_id'] for row in bot.database.fetch_active_grids() if row['side']=='BUY'}
        self.pending(bot,exchange)
        self.fill_all_sells(bot,exchange)
        bot.run_cycle()
        self.assertIsNone(bot.database.get_state(LIQUIDATION_KEY))
        self.assertIsNone(bot.database.get_state(LIQUIDATION_ALARM_KEY))
        self.assertIsNone(bot.database.get_state(SAFETY_MODE_KEY))
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY),ENGINE_RUNNING)
        self.assertEqual({r['order_id'] for r in bot.database.fetch_active_grids() if r['side']=='BUY'},buys)
        self.assertEqual(exchange.cancel_calls,[])
        self.assertEqual(exchange.market_sell_calls,0)
        self.assertEqual(exchange.base_free,D(1))
        self.assertFalse(bot.is_paused)
        bot.run_cycle()  # A following ordinary cycle must also preserve the flat grid.
        self.assertEqual(exchange.cancel_calls,[])

    def test_pending_verification_survives_ready_mode_process_restart(self):
        bot,exchange=self.grid()
        self.pending(bot,exchange)
        restored=OfflineGridBot(bot.config,exchange,GridDatabase(self.path))
        asyncio.run(engine._reconcile_service_boot(restored,ready=True))
        restored.prepare(persist=False)
        with patch.object(restored,'_audit_full_book_if_due',side_effect=AssertionError('Audit delayed risk')):
            restored.run_cycle()
        self.assertEqual(exchange.market_sell_calls,1)
        self.assertEqual(exchange.base_free,D(1))
        self.assertEqual(restored.database.get_state(SAFETY_MODE_KEY),LIQUIDATED)

    def test_emergency_precedes_failing_routine_book_audit(self):
        bot,exchange=self.grid()
        exchange.price=D(69)
        with patch.object(bot,'_audit_full_book_if_due',side_effect=ccxt.RequestTimeout('audit unavailable')) as audit:
            bot.run_cycle()
        audit.assert_not_called()
        self.assertEqual(exchange.market_sell_calls,1)

    def test_healthy_price_still_runs_audit_and_does_not_latch_stop(self):
        bot,exchange=self.grid()
        with patch.object(bot,'_audit_full_book_if_due',side_effect=ccxt.RequestTimeout('audit unavailable')):
            with self.assertRaises(ccxt.RequestTimeout):
                bot.run_cycle()
        self.assertIsNone(bot.database.get_state(LIQUIDATION_KEY))
        self.assertEqual(exchange.market_sell_calls,0)

    def test_equal_and_above_stop_timeouts_do_not_create_exit(self):
        for price in (D(70),D(100)):
            with self.subTest(price=price),tempfile.TemporaryDirectory() as directory:
                bot,exchange=make_bot(Path(directory)/'grid_testnet.sqlite')
                bot.run_cycle();bot.run_cycle()
                exchange.price=price
                with patch.object(exchange,'fetch_order',side_effect=ccxt.RequestTimeout('unavailable')):
                    with self.assertRaises(ccxt.RequestTimeout):
                        bot.run_cycle()
                self.assertIsNone(bot.database.get_state(LIQUIDATION_KEY))

    def test_reset_risk_check_latches_rate_limited_order_reads(self):
        bot,exchange=self.grid()
        with patch.object(exchange,'fetch_order',side_effect=CircuitOpenError(60)):
            with self.assertRaises(CircuitOpenError):
                bot._evaluate_automatic_stop(D(69))
        self.assertEqual(json.loads(bot.database.get_state(LIQUIDATION_KEY))['phase'],'verifying')
        self.assertEqual(exchange.market_sell_calls,0)

    def test_unknown_inventory_during_pending_verification_halts_without_selling(self):
        bot,exchange=self.grid()
        self.pending(bot,exchange)
        with patch.object(bot,'_bot_btc_balance',side_effect=TradingHalt('ledger unavailable')):
            bot.run_cycle()
        state=json.loads(bot.database.get_state(LIQUIDATION_KEY))
        self.assertEqual(state['phase'],'failed')
        self.assertEqual(exchange.market_sell_calls,0)
        self.assertIn('ledger unavailable',bot.database.get_state(LIQUIDATION_ALARM_KEY))

    def test_manual_panic_can_override_pending_flat_verification(self):
        bot,exchange=self.grid()
        self.pending(bot,exchange)
        self.fill_all_sells(bot,exchange)
        result=bot.force_liquidate()
        self.assertTrue(result['completed'])
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY),ENGINE_HALTED)
        self.assertEqual(exchange.market_sell_calls,0)
        self.assertTrue(exchange.cancel_calls)

    def test_flat_recovery_restores_preexisting_manual_pause(self):
        bot,exchange=self.grid()
        bot.database.set_state(SAFETY_MODE_KEY,PAUSED_MANUAL)
        bot.is_paused=True
        self.pending(bot,exchange)
        self.fill_all_sells(bot,exchange)
        bot.run_cycle()
        self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY),PAUSED_MANUAL)
        self.assertTrue(bot.is_paused)

    def test_dashboard_inventory_stays_unknown_during_verification(self):
        bot,exchange=self.grid()
        self.pending(bot,exchange)
        self.assertIsNone(_portfolio_wallet(bot.database)['btc_held'])

    def test_lease_loss_during_verification_does_not_rewrite_risk_state(self):
        bot,exchange=self.grid()
        before=self.pending(bot,exchange)
        with patch.object(bot,'_fetch_order',side_effect=RunnerLeaseLost('lease expired')):
            with self.assertRaises(RunnerLeaseLost):
                bot.run_cycle()
        self.assertEqual(json.loads(bot.database.get_state(LIQUIDATION_KEY)),before)
        self.assertEqual(exchange.market_sell_calls,0)

    def test_async_service_loop_cannot_put_a_failing_audit_ahead_of_stop(self):
        bot,exchange=self.grid()
        bot.ready_mode=True
        exchange.price=D(69)
        def stop_after_exit(delay):
            bot.stop_controller.stop_requested.set()
            return True
        with patch.object(bot,'_audit_full_book_if_due',side_effect=AssertionError('Audit ran before emergency')),patch.object(bot.stop_controller.stop_requested,'wait',side_effect=stop_after_exit):
            asyncio.run(bot.run(AsyncMock()))
        self.assertEqual(exchange.market_sell_calls,1)
        self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY),LIQUIDATED)
