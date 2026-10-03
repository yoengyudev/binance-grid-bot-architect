"""Crash/restart scenarios against real SQLite and a deterministic exchange."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import ccxt

import engine
from database import GridDatabase
from engine import GridBot, _reconcile_service_boot
from models import TradingHalt
from portfolio import _portfolio_wallet
from settings import (
    ENGINE_FAULT_KEY, ENGINE_HALTED, ENGINE_IDLE, ENGINE_RUNNING,
    ENGINE_STATUS_KEY, ENGINE_STOP_CLEANUP_KEY, FILL_SNAPSHOT_PREFIX,
    LIQUIDATED, LIQUIDATING, LIQUIDATION_ALARM_KEY, LIQUIDATION_KEY,
    LIQUIDATION_NOTICE_KEY, SAFETY_MODE_KEY, TRAILING_STOP_KEY,
)
from tests.fakes import FakeSpotExchange, make_bot


class SimulatedCrash(BaseException):
    """Bypass normal fault handlers, as a killed process would."""


class NoFillExchange(FakeSpotExchange):
    """Accept a MARKET SELL but never execute it until the test releases it."""

    def create_market_sell_order(self, symbol, amount, params=None):
        self.market_sell_calls += 1
        quantity = Decimal(str(amount))
        client_id = params['newClientOrderId']
        self.base_free -= quantity
        order = {
            'id': str(self.next_id), 'clientOrderId': client_id,
            'symbol': symbol, 'side': 'sell', 'type': 'market',
            'status': 'open', 'amount': str(quantity), 'remaining': str(quantity),
            'filled': '0', 'cost': '0', 'average': None, 'price': None, 'fees': [],
        }
        self.next_id += 1
        self.orders[client_id] = order
        return dict(order)

    def release_fill(self, client_id, execution_price):
        order = self.orders[client_id]
        quantity = Decimal(order['amount'])
        proceeds = quantity * execution_price
        order.update(status='closed', filled=str(quantity), remaining='0',
                     average=str(execution_price), cost=str(proceeds))
        self.quote_free += proceeds


class FaultInjectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'grid_testnet.sqlite'
        alerts = patch.object(engine, 'schedule_telegram_alert', return_value=False)
        alerts.start()
        self.addCleanup(alerts.stop)

    def live_bot(self, exchange=None):
        bot, exchange = make_bot(self.path, initial_base=Decimal('0'), exchange=exchange)
        bot.run_cycle()
        bot.run_cycle()
        bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        self.assertTrue(exchange.fetch_open_orders('BTC/USDT'))
        return bot, exchange

    def reopen(self, bot, exchange):
        reopened = GridBot(bot.config, exchange, GridDatabase(self.path))
        reopened.ready_mode = True
        # Service run() prepares the saved geometry before executing cycles.
        reopened.prepare(persist=False)
        return reopened

    def install_abort(self, predicate):
        """Abort at a later SQL statement, after earlier writes have executed."""
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                'CREATE TRIGGER injected_failure BEFORE INSERT ON bot_state '
                f'WHEN {predicate} BEGIN '
                "SELECT RAISE(ABORT, 'injected disk failure'); END"
            )

    def test_ready_restart_cancels_active_exchange_orders_before_resting_idle(self):
        bot, exchange = self.live_bot()
        held = _portfolio_wallet(bot.database)['btc_held']
        submissions = len(exchange.orders)
        restarted = self.reopen(bot, exchange)
        asyncio.run(_reconcile_service_boot(restarted, ready=True))

        recovered = GridDatabase(self.path)
        self.assertEqual(recovered.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
        self.assertIsNone(recovered.get_state(ENGINE_STOP_CLEANUP_KEY))
        self.assertEqual(recovered.fetch_active_grids(), [])
        self.assertEqual(exchange.fetch_open_orders('BTC/USDT'), [])
        self.assertEqual(len(exchange.orders), submissions)
        self.assertEqual(_portfolio_wallet(recovered)['btc_held'], held)
        self.assertEqual(exchange.base_free, Decimal(str(held)))

    def test_ready_restart_with_cancellation_timeout_keeps_start_locked(self):
        bot, exchange = self.live_bot()
        restarted = self.reopen(bot, exchange)
        exchange.cancel_timeout = 'before'
        asyncio.run(_reconcile_service_boot(restarted, ready=True))
        recovered = GridDatabase(self.path)
        self.assertEqual(recovered.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
        self.assertEqual(recovered.get_state(ENGINE_STOP_CLEANUP_KEY), '1')
        self.assertTrue(recovered.fetch_active_grids())
        with self.assertRaisesRegex(TradingHalt, 'prior stop'):
            restarted.start_engine(auto_cover_inventory=True)
        restarted.reconcile_idle_orders()
        self.assertIsNone(recovered.get_state(ENGINE_STOP_CLEANUP_KEY))
        self.assertEqual(exchange.fetch_open_orders('BTC/USDT'), [])

    def test_timeout_during_stop_is_recovered_by_exact_order_ids_after_restart(self):
        for timing in ('before', 'after'):
            with self.subTest(timing=timing), tempfile.TemporaryDirectory() as directory:
                self.path = Path(directory) / 'grid_testnet.sqlite'
                bot, exchange = self.live_bot()
                owned = _portfolio_wallet(bot.database)['btc_held']
                original_ids = set(exchange.orders)
                exchange.cancel_timeout = timing
                with self.assertRaises(ccxt.RequestTimeout):
                    bot.stop_engine()
                recovered = GridDatabase(self.path)
                self.assertEqual(recovered.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
                self.assertEqual(recovered.get_state(ENGINE_STOP_CLEANUP_KEY), '1')
                self.assertTrue(recovered.fetch_active_grids())
                restarted = self.reopen(bot, exchange)
                asyncio.run(_reconcile_service_boot(restarted, ready=True))
                self.assertIsNone(recovered.get_state(ENGINE_STOP_CLEANUP_KEY))
                self.assertEqual(exchange.fetch_open_orders('BTC/USDT'), [])
                self.assertEqual(recovered.fetch_active_grids(), [])
                self.assertEqual(set(exchange.orders), original_ids)
                self.assertEqual(_portfolio_wallet(recovered)['btc_held'], owned)

    def test_stop_controller_persists_timeout_trace_and_preserves_unresolved_order(self):
        bot, exchange = self.live_bot()
        exchange.cancel_timeout = 'before'
        result = bot.stop_controller.request_stop()
        self.assertEqual(result.unresolved, 1)
        recovered = GridDatabase(self.path)
        self.assertEqual(recovered.get_state(ENGINE_STOP_CLEANUP_KEY), '1')
        self.assertEqual(len(recovered.fetch_active_grids()), 1)
        entry = next(row for row in recovered.fetch_transition_journal()
                     if row['trigger_event'] == 'STOP_CANCEL_FAILED')
        self.assertIn('RequestTimeout', entry['error_details'])
        self.assertIn('Simulated cancel request timeout', entry['error_details'])
        self.assertIn('Traceback', entry['error_details'])

    def test_hard_stop_no_fills_survives_crash_without_duplicate_sell_or_false_completion(self):
        bot, exchange = self.live_bot(NoFillExchange())
        owned = Decimal(str(_portfolio_wallet(bot.database)['btc_held']))
        exchange.price = Decimal('69')
        with patch.object(engine.time, 'sleep', side_effect=SimulatedCrash):
            with self.assertRaises(SimulatedCrash):
                bot.run_cycle()
        recovered = GridDatabase(self.path)
        state = json.loads(recovered.get_state(LIQUIDATION_KEY))
        client_id = state['client_order_id']
        self.assertEqual(state['phase'], 'confirming')
        self.assertEqual(recovered.get_state(SAFETY_MODE_KEY), LIQUIDATING)
        self.assertEqual(Decimal(state['sold_base']), 0)
        self.assertIsNotNone(recovered.get_state(LIQUIDATION_ALARM_KEY))
        self.assertIsNone(recovered.get_state(LIQUIDATION_NOTICE_KEY))
        self.assertEqual(Decimal(state['held_base']), owned)
        # An unresolved exchange sale must never be presented as a flat wallet.
        self.assertIsNone(_portfolio_wallet(recovered)['btc_held'])

        restarted = self.reopen(bot, exchange)
        asyncio.run(_reconcile_service_boot(restarted, ready=True))
        with patch.object(engine.time, 'sleep', side_effect=SimulatedCrash):
            with self.assertRaises(SimulatedCrash):
                restarted.run_cycle()
        self.assertEqual(exchange.market_sell_calls, 1)
        self.assertEqual(json.loads(recovered.get_state(LIQUIDATION_KEY))['client_order_id'], client_id)
        # A gap/slippage fill must use actual proceeds, even far below the stop.
        exchange.release_fill(client_id, Decimal('9'))
        restarted.run_cycle()
        final = json.loads(recovered.get_state(LIQUIDATION_KEY))
        self.assertEqual(final['phase'], 'complete')
        self.assertEqual(Decimal(final['residual_base']), 0)
        self.assertEqual(Decimal(final['proceeds_quote']), owned * 9)
        self.assertEqual(recovered.get_state(SAFETY_MODE_KEY), LIQUIDATED)
        self.assertEqual(recovered.get_state(ENGINE_STATUS_KEY), ENGINE_HALTED)
        self.assertEqual(exchange.market_sell_calls, 1)
        trades = recovered.fetch_trade_history()
        self.assertEqual(len(trades), 1)
        self.assertLess(Decimal(trades[0]['profit']), 0)
        self.assertEqual(exchange.fetch_open_orders('BTC/USDT'), [])

    def test_failed_fill_commit_rolls_back_snapshot_identity_and_status(self):
        bot, exchange = make_bot(self.path, initial_base=Decimal('0'))
        client_id = bot._submit_order(1, 'BUY', Decimal('90'), Decimal('1'))
        exchange.fill(client_id)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            # Reproduce a crash after exchange acceptance but before ID attachment.
            connection.execute('UPDATE grid_orders SET exchange_order_id = NULL WHERE order_id = ?',
                               (client_id,))
            connection.execute(
                "CREATE TRIGGER injected_failure BEFORE UPDATE OF status ON grid_orders "
                "WHEN NEW.status = 'FILLED' BEGIN "
                "SELECT RAISE(ABORT, 'injected disk failure'); END"
            )
        row = bot.database.get_order(client_id)
        with self.assertRaises(sqlite3.IntegrityError):
            bot._reconcile_order(row)
        recovered = GridDatabase(self.path)
        self.assertEqual(recovered.get_order(client_id)['status'], 'OPEN')
        self.assertIsNone(recovered.get_order(client_id)['exchange_order_id'])
        self.assertIsNone(recovered.get_state(FILL_SNAPSHOT_PREFIX + client_id))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('DROP TRIGGER injected_failure')
        bot._reconcile_order(recovered.get_order(client_id))
        self.assertEqual(recovered.get_order(client_id)['status'], 'FILLED')
        self.assertIsNotNone(recovered.get_order(client_id)['exchange_order_id'])
        self.assertIsNotNone(recovered.get_state(FILL_SNAPSHOT_PREFIX + client_id))

    def test_failed_start_commit_does_not_activate_or_save_half_a_resume(self):
        bot, exchange = self.live_bot()
        bot.stop_engine()
        bot.database.set_state(ENGINE_FAULT_KEY, 'old fault')
        self.install_abort("NEW.key = 'engine_status' AND NEW.value = 'RUNNING'")
        with self.assertRaises(sqlite3.IntegrityError):
            bot.start_engine(auto_cover_inventory=True)
        recovered = GridDatabase(self.path)
        self.assertEqual(recovered.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
        self.assertEqual(recovered.get_state(ENGINE_FAULT_KEY), 'old fault')
        self.assertIsNone(recovered.get_state('grid_reset'))
        self.assertIsNone(recovered.get_state('reconcile_cover_inventory'))
        self.assertFalse(bot.grid_needs_reset)
        self.assertNotEqual(recovered.fetch_transition_journal()[-1]['trigger_event'], 'START')

    def test_failed_liquidation_terminal_commit_remains_recoverable(self):
        bot, exchange = self.live_bot()
        self.install_abort("NEW.key = 'engine_status' AND NEW.value = 'HALTED'")
        exchange.price = Decimal('69')
        with self.assertRaises(sqlite3.IntegrityError):
            bot.run_cycle()
        recovered = GridDatabase(self.path)
        state = json.loads(recovered.get_state(LIQUIDATION_KEY))
        self.assertEqual(state['phase'], 'confirming')
        self.assertEqual(recovered.get_state(SAFETY_MODE_KEY), LIQUIDATING)
        self.assertIsNone(recovered.get_state(LIQUIDATION_NOTICE_KEY))
        self.assertEqual(len(recovered.fetch_trade_history()), 1)
        self.assertFalse(any(entry['new_state'] == LIQUIDATED
                             for entry in recovered.fetch_transition_journal()))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('DROP TRIGGER injected_failure')
        restarted = self.reopen(bot, exchange)
        restarted.run_cycle()
        self.assertEqual(recovered.get_state(SAFETY_MODE_KEY), LIQUIDATED)
        self.assertEqual(exchange.market_sell_calls, 1)
        self.assertEqual(len(recovered.fetch_trade_history()), 1)

    def test_failed_prepare_does_not_leave_trailing_state_without_a_run(self):
        bot, exchange = make_bot(self.path, initial_base=Decimal('0'))
        for key in ('grid_run', TRAILING_STOP_KEY, 'breakout_width_percent'):
            bot.database.clear_state(key)
        self.install_abort("NEW.key = 'breakout_width_percent'")
        with self.assertRaises(sqlite3.IntegrityError):
            bot.prepare(persist=True)
        recovered = GridDatabase(self.path)
        for key in ('grid_run', TRAILING_STOP_KEY, 'breakout_width_percent'):
            self.assertIsNone(recovered.get_state(key))

    def test_killed_process_rolls_back_orders_state_and_journal_together(self):
        database = GridDatabase(self.path)
        database.set_state(ENGINE_STATUS_KEY, ENGINE_IDLE)
        journal_before = database.fetch_transition_journal()
        environment = dict(os.environ, TRADING_ENVIRONMENT='TESTNET',
                           BINANCE_TESTNET_API_KEY='unit-test-key',
                           BINANCE_TESTNET_API_SECRET='unit-test-secret')
        for key in ('GRID_BOT_DB_PATH', 'GRID_BOT_DB_DIR'):
            environment.pop(key, None)
        crash_script = """import os, sys
from database import GridDatabase
db = GridDatabase(sys.argv[1])
with db.transaction():
    db.insert_order("crash-order", 1, "BUY", 90, 1)
    db.set_state("engine_status", "RUNNING")
    os._exit(91)
"""
        crashed = subprocess.run(
            [sys.executable, '-c', crash_script, str(self.path)],
            cwd=Path(engine.__file__).parent, env=environment,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(crashed.returncode, 91, crashed.stderr)
        recovered = GridDatabase(self.path)
        self.assertEqual(recovered.fetch_all_orders(), [])
        self.assertEqual(recovered.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
        self.assertEqual(recovered.fetch_transition_journal(), journal_before)


if __name__ == '__main__':
    unittest.main()
