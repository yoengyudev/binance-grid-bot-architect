"""Concurrency, outage budgets, and separation from fresh trading balances."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import ccxt
import api
from fastapi import Response
from dashboard_reads import DashboardReads, DashboardReadDeferred
from exchange_handler import CircuitOpenError
from settings import ENGINE_RUNNING
from tests.fakes import make_bot


class DashboardReadTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.exchange = Mock()
        self.exchange.fetch_balance.return_value = {'BTC': {'free':'1','used':'0'},'USDT':{'free':'500'}}
        self.exchange.fetch_open_orders.return_value = []
        self.exchange.fetch_ticker.return_value = {'last':'100'}
        self.exchange.market.return_value = {'base':'BTC','quote':'USDT','spot':True}
        self.reader = DashboardReads(self.exchange, clock=lambda:self.now)

    def test_shared_snapshot_hits_ttl_and_does_not_leak_mutable_results(self):
        first = self.reader.snapshot('BTC/USDT')
        first['balances']['USDT']['free']='0'
        self.assertEqual(self.reader.snapshot('BTC/USDT')['balances']['USDT']['free'],'500')
        self.exchange.fetch_balance.assert_called_once()
        self.now += 5
        self.reader.snapshot('BTC/USDT')
        self.assertEqual(self.exchange.fetch_balance.call_count,2)

    def test_concurrent_requests_do_not_queue_or_duplicate_exchange_calls(self):
        entered, release = Event(), Event()
        original=self.exchange.fetch_balance.return_value
        def balance(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return original
        self.exchange.fetch_balance.side_effect=balance
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.reader.snapshot,'BTC/USDT')
            self.assertTrue(entered.wait(3))
            try:
                with self.assertRaises(DashboardReadDeferred):self.reader.snapshot('BTC/USDT')
                self.exchange.fetch_balance.assert_called_once()
            finally:release.set()
            first.result(timeout=3)
        self.reader.snapshot('BTC/USDT')
        self.exchange.fetch_balance.assert_called_once()

    def test_outage_has_one_attempt_and_shared_exponential_cooldown(self):
        self.exchange.fetch_balance.side_effect=ccxt.RequestTimeout('offline')
        with patch('dashboard_reads.network_backoff',side_effect=[1,2,4]):
            for delay in (5,10,20):
                with self.assertRaises(ccxt.RequestTimeout):self.reader.snapshot('BTC/USDT')
                calls=self.exchange.fetch_balance.call_count
                for _ in range(20):
                    with self.assertRaises(DashboardReadDeferred):self.reader.snapshot('BTC/USDT')
                self.assertEqual(self.exchange.fetch_balance.call_count,calls)
                self.now += delay
        self.exchange.fetch_balance.side_effect=None
        self.reader.snapshot('BTC/USDT')
        self.assertEqual(self.exchange.fetch_balance.call_count,4)
        self.assertEqual(self.reader._failures,0)

    def test_server_circuit_penalty_is_never_shortened(self):
        self.exchange.fetch_balance.side_effect=CircuitOpenError(120)
        with self.assertRaises(CircuitOpenError):self.reader.snapshot('BTC/USDT')
        self.now += 119
        with self.assertRaises(DashboardReadDeferred):self.reader.snapshot('BTC/USDT')
        self.exchange.fetch_balance.assert_called_once()

    def test_failure_does_not_serve_old_snapshot_as_current(self):
        self.reader.snapshot('BTC/USDT')
        self.now += 5
        self.exchange.fetch_open_orders.side_effect=ccxt.NetworkError('offline')
        with self.assertRaises(ccxt.NetworkError):self.reader.snapshot('BTC/USDT')
        with self.assertRaises(DashboardReadDeferred):self.reader.snapshot('BTC/USDT')

    def test_invalidation_during_refresh_discards_its_result(self):
        self.exchange.fetch_balance.side_effect=lambda *_:(self.reader.invalidate() or {})
        with self.assertRaises(DashboardReadDeferred):self.reader.snapshot('BTC/USDT')
        self.assertIsNone(self.reader._snapshot)

    def test_status_and_wallet_share_one_refresh_but_trading_reads_stay_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange=make_bot(Path(directory)/'grid_testnet.sqlite',initial_base=Decimal(0))
            exchange.fetch_balance=Mock(return_value={'BTC':{'free':'0','used':'0'},'USDT':{'free':'500'}})
            previous=api.app.state.grid_bot,api.app.state.monitor_only
            api.app.state.grid_bot,api.app.state.monitor_only=bot,False
            try:
                api._unprotected_exposure_payload(bot,bot.database,ENGINE_RUNNING)
                self.assertEqual(api.wallet_balance(Response(), 'admin'),{'available_usdt':'500'})
                exchange.fetch_balance.assert_called_once()
                exchange.fetch_balance.return_value['USDT']['free']='123'
                self.assertEqual(bot._free_balance('USDT'),Decimal('123'))
                self.assertEqual(exchange.fetch_balance.call_count,2)
            finally:api.app.state.grid_bot,api.app.state.monitor_only=previous

    def test_cooling_dashboard_does_not_block_fresh_engine_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange=make_bot(Path(directory)/'grid_testnet.sqlite')
            reader=bot.dashboard_reads()
            with patch.object(exchange,'fetch_balance',side_effect=ccxt.RequestTimeout('offline')):
                with self.assertRaises(ccxt.RequestTimeout):reader.snapshot('BTC/USDT')
            self.assertEqual(bot._free_balance('USDT'),Decimal('10000'))
            with self.assertRaises(DashboardReadDeferred):reader.snapshot('BTC/USDT')

    def test_real_engine_owns_one_separate_dashboard_client(self):
        with tempfile.TemporaryDirectory() as directory:
            bot,_=make_bot(Path(directory)/'grid_testnet.sqlite')
            bot.exchange=ccxt.binance({'enableRateLimit':False})  # Constructor performs no I/O.
            clone=Mock()
            with patch('exchange_handler.create_exchange',return_value=clone) as factory:
                first=bot.dashboard_reads()
                self.assertIs(first,bot.dashboard_reads())
                self.assertIs(first.exchange,clone)
                self.assertIsNot(first.exchange,bot.exchange)
                factory.assert_called_once()


if __name__=='__main__':unittest.main()
