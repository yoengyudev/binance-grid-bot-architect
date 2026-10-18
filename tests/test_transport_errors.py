"""Raw socket failures retry without masking local I/O or replaying writes."""
import asyncio
import errno
import json
import socket
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import ccxt
import engine
from exchange_handler import CircuitOpenError, exchange_request
from models import RunnerLeaseLost, UncertainOrderError
from settings import (ENGINE_RUNNING, ENGINE_STATUS_KEY, LIQUIDATING,
                      LIQUIDATED, LIQUIDATION_KEY, SAFETY_MODE_KEY)
from tests.fakes import make_bot


class TransportErrorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'grid_testnet.sqlite'
        self.alerts = patch.object(engine, 'schedule_telegram_alert', return_value=False)
        self.alerts.start()
        self.addCleanup(self.alerts.stop)

    def grid(self):
        bot, exchange = make_bot(self.path, initial_base=D(1))
        bot.run_cycle()
        bot.run_cycle()
        bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        bot.ready_mode = True
        return bot, exchange

    def test_transport_errors_are_normalized_with_original_cause(self):
        for error in (OSError('socket unavailable'), ConnectionResetError('reset'),
                      TimeoutError('timeout'), socket.gaierror(-2, 'DNS failure'),
                      OSError(errno.ENETUNREACH, 'no route')):
            with self.subTest(error=type(error).__name__):
                request = Mock(side_effect=error)
                with self.assertRaises(ccxt.NetworkError) as raised:
                    exchange_request(request, 'BTC/USDT')
                self.assertIs(raised.exception.__cause__, error)
                request.assert_called_once_with('BTC/USDT')

    def test_local_io_rate_limits_and_lease_errors_keep_their_identity(self):
        for error in (OSError(errno.ENOSPC, 'disk full'),
                      FileNotFoundError(errno.ENOENT, 'file missing'),
                      PermissionError(errno.EACCES, 'denied'),
                      sqlite3.OperationalError('database locked'),
                      CircuitOpenError(60), RunnerLeaseLost('lease lost'),
                      ccxt.AuthenticationError('bad credentials'),
                      ccxt.InvalidOrder('invalid quantity')):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)) as raised:
                    exchange_request(Mock(side_effect=error))
                self.assertIs(raised.exception, error)

    def test_pending_stop_retries_raw_order_reads_then_recovers_after_bounce(self):
        bot, exchange = self.grid()
        exchange.price = D(69)
        original_fetch = exchange.fetch_order
        remaining_failures = 9  # Three reads to latch, then two exhausted cycles.

        def fetch_order(*args):
            nonlocal remaining_failures
            if remaining_failures:
                remaining_failures -= 1
                raise OSError('raw socket unavailable')
            return original_fetch(*args)

        def backoff(attempt, *, cap=60):
            return (1, 2)[attempt] if cap == 2 else (4, 8)[attempt]

        def wait(delay):
            self.assertFalse(bot.exchange_lock._is_owned())
            if bot.database.get_state(SAFETY_MODE_KEY) == LIQUIDATED:
                bot.stop_controller.stop_requested.set()
                return True
            if delay in (4, 8):
                state = json.loads(bot.database.get_state(LIQUIDATION_KEY))
                self.assertEqual((state['phase'], state['trigger_price']), ('verifying', '69'))
                self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_RUNNING)
                self.assertEqual(exchange.market_sell_calls, 0)
                self.assertEqual(exchange.cancel_calls, [])
            return False

        with patch.object(exchange, 'fetch_order', fetch_order), \
                patch.object(engine, 'network_backoff', side_effect=backoff), \
                patch.object(bot.stop_controller.stop_requested, 'wait', side_effect=wait) as waits, \
                patch.object(bot, 'halt_after_fault', wraps=bot.halt_after_fault) as halt:
            with self.assertRaises(ccxt.NetworkError):
                bot.run_cycle()
            exchange.price = D(100)  # Recovery must retain the witnessed breach.
            waits.reset_mock()
            asyncio.run(bot.run(AsyncMock()))
            halt.assert_not_called()
        self.assertEqual([c.args[0] for c in waits.call_args_list][:6], [1, 2, 4, 1, 2, 8])
        self.assertEqual(exchange.market_sell_calls, 1)
        self.assertEqual(exchange.base_free, D(1))
        self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)

    def test_stop_interrupts_raw_read_backoff_without_more_exchange_requests(self):
        bot, exchange = self.grid()

        def fetch_ticker(symbol):
            raise OSError('socket unavailable')

        def stop(delay):
            bot.stop_controller.stop_requested.set()
            return True

        request = Mock(wraps=fetch_ticker)
        request.__name__ = 'fetch_ticker'
        with patch.object(bot.stop_controller.stop_requested, 'wait', side_effect=stop), \
                self.assertRaises(ccxt.NetworkError):
            bot._call(request, 'BTC/USDT')
        request.assert_called_once()

    def test_ordinary_ticker_cycle_recovers_raw_timeout_without_fault_stop(self):
        bot, exchange = self.grid()
        original_fetch = exchange.fetch_ticker
        failures = 0

        async def arm_cycle_failure(*args):
            nonlocal failures
            failures = 3  # Fail the trading loop, after startup preparation.

        def fetch_ticker(*args):
            nonlocal failures
            if failures:
                failures -= 1
                raise TimeoutError('socket timed out')
            return original_fetch(*args)

        def wait(delay):
            if failures == 0 and delay == bot.config.poll_seconds:
                bot.stop_controller.stop_requested.set()
                return True
            return False

        before = len(exchange.orders)
        notifier = AsyncMock()
        notifier.notify_startup.side_effect = arm_cycle_failure
        with patch.object(exchange, 'fetch_ticker', fetch_ticker), \
                patch.object(engine, 'network_backoff', return_value=0), \
                patch.object(bot.stop_controller.stop_requested, 'wait', side_effect=wait), \
                patch.object(bot, 'halt_after_fault', wraps=bot.halt_after_fault) as halt:
            asyncio.run(bot.run(notifier))
            halt.assert_not_called()
        self.assertEqual(len(exchange.orders), before)
        self.assertIsNone(bot.database.get_state(LIQUIDATION_KEY))
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_RUNNING)

    def test_raw_market_sell_response_loss_is_reconciled_without_duplicate_sale(self):
        bot, exchange = self.grid()
        original_sell = exchange.create_market_sell_order

        def accepted(*args):
            original_sell(*args)
            raise OSError('market sale response lost')

        exchange.price = D(69)
        with patch.object(exchange, 'create_market_sell_order', side_effect=accepted) as sell, \
                patch.object(engine.time, 'sleep'):
            bot.run_cycle()
        sell.assert_called_once()
        self.assertEqual(exchange.market_sell_calls, 1)
        self.assertEqual(exchange.base_free, D(1))
        self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)

    def test_raw_create_response_loss_recovers_exact_id_without_reposting(self):
        bot, exchange = make_bot(self.path)
        original_create = exchange.create_order

        def accepted(*args):
            original_create(*args)
            raise OSError('response lost after acceptance')

        with patch.object(exchange, 'create_order', side_effect=accepted) as create:
            order_id = bot._submit_order(1, 'BUY', D(90), D(1))
        create.assert_called_once()
        self.assertEqual(len(exchange.orders), 1)
        self.assertIsNotNone(bot.database.get_order(order_id)['exchange_order_id'])

    def test_unconfirmed_raw_create_keeps_intent_and_does_not_repost(self):
        bot, exchange = make_bot(self.path)
        with patch.object(exchange, 'create_order', side_effect=OSError('response lost')) as create:
            with self.assertRaises(UncertainOrderError):
                bot._submit_order(1, 'BUY', D(90), D(1))
        create.assert_called_once()
        self.assertEqual(len(bot.database.fetch_all_orders()), 1)

    def test_local_cycle_io_failure_still_reaches_fatal_handler(self):
        bot, exchange = self.grid()
        failure = OSError(errno.ENOSPC, 'local disk full')
        with patch.object(bot, 'run_cycle', side_effect=failure), \
                patch.object(bot, 'halt_after_fault', wraps=bot.halt_after_fault) as halt:
            with self.assertRaises(OSError) as raised:
                asyncio.run(bot.run(AsyncMock()))
        self.assertIs(raised.exception, failure)
        halt.assert_called_once_with(failure)

    def test_stop_controller_verifies_raw_cancel_timeout_without_second_cancel(self):
        bot, exchange = self.grid()
        original_cancel = exchange.cancel_order

        def accepted(*args):
            original_cancel(*args)
            raise OSError('cancel response lost')

        count = len(bot.database.fetch_active_grids())
        with patch.object(exchange, 'cancel_order', side_effect=accepted) as cancel:
            result = bot.stop_controller.request_stop()
        self.assertEqual(cancel.call_count, count)
        self.assertEqual(result.unresolved, 0)
        self.assertEqual(bot.database.fetch_active_grids(), [])
