"""Realistic maker rejection and marketable exit execution; no exchange access."""
import asyncio
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

import ccxt

import engine
import main
from database import GridDatabase
from models import GridConfig, UncertainOrderError
from models import TradingHalt
from portfolio import _portfolio_wallet
from tests.fakes import FakeSpotExchange, OfflineGridBot
from settings import ENGINE_STATUS_KEY, ENGINE_RUNNING, ENGINE_FAULT_KEY, ENGINE_STOP_CLEANUP_KEY, SAFETY_MODE_KEY, LIQUIDATION_KEY
from types import SimpleNamespace


class ProcessLost(BaseException):
    pass


class CrossingSellExchange(FakeSpotExchange):
    """Reject maker-only crossed exits; match GTC exits at the current bid.

    Orders reserve their full quantity, partial executions retain the remainder,
    and private trade history supplies cumulative execution prices/commissions.
    """
    fee_rate = D('.001')

    def __init__(self):
        super().__init__()
        self.has = {'fetchOrderTrades': True}
        self.match_sells = False
        self.fill_fraction = D(1)
        self.failure = None
        self.submissions = []

    def create_order(self, symbol, order_type, side, amount, price, params):
        self.submissions.append((side, order_type, dict(params)))
        crossing = (self.match_sells and order_type == 'limit' and side == 'sell'
                    and D(str(price)) <= self.price)
        if crossing and params.get('timeInForce') == 'PO':
            raise ccxt.OrderImmediatelyFillable('Order would immediately match and take.')
        failure = self.failure if crossing else None
        if crossing:
            self.failure = None
        if failure == 'before':
            raise ccxt.RequestTimeout('Connection lost before acceptance')
        result = super().create_order(symbol, order_type, side, amount, price, params)
        order = self.orders[params['newClientOrderId']]
        order['remaining'] = str(D(order['amount'])-D(order['filled']))
        if side == 'buy' and order['status'] == 'closed':
            self._buy_fee(order)
        if crossing:
            quantity = D(order['amount'])
            filled = D(self.amount_to_precision(symbol, str(quantity*self.fill_fraction)))
            self.execute_sell(order['clientOrderId'], filled)
        if failure == 'after':
            raise ccxt.RequestTimeout('Connection lost after acceptance')
        if failure == 'crash':
            raise ProcessLost('Process lost after acceptance, before SQLite acknowledgment')
        return dict(order)

    def _buy_fee(self, order):
        fee = D(order['cost'])*self.fee_rate
        order['fees'] = [{'currency': 'USDT', 'cost': str(fee)}]
        self.quote_free -= fee

    def fill(self, client_id):
        order = self.orders[client_id]
        if order['side'] == 'sell':
            self.execute_sell(client_id, D(order['amount'])-D(order['filled']))
        else:
            super().fill(client_id)
            order['remaining'] = '0'
            self._buy_fee(order)

    def execute_sell(self, client_id, quantity):
        order = self.orders[client_id]
        previous = D(order['filled'])
        total = previous+quantity
        cost = (D(order['cost']) if previous else D(0)) + quantity*self.price
        order.update(filled=str(total), remaining=str(D(order['amount'])-total),
                     status='closed' if total == D(order['amount']) else 'open',
                     cost=str(cost), average=str(cost/total),
                     fees=[{'currency': 'USDT', 'cost': str(cost*self.fee_rate)}])
        self.quote_free += quantity*self.price*(1-self.fee_rate)

    def fetch_order_trades(self, order_id, symbol, since=None, limit=None, params=None):
        order = next(order for order in self.orders.values() if order['id'] == order_id)
        if D(order['filled']) == 0:
            return []
        return [{'id': order_id, 'order': order_id, 'symbol': symbol,
                 'info': {'qty': order['filled'], 'quoteQty': order['cost'],
                          'commission': str(D(order['cost'])*self.fee_rate),
                          'commissionAsset': 'USDT'}}]


class MarketableSellExitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)/'grid_testnet.sqlite'
        self.exchange = CrossingSellExchange()
        config = GridConfig('BTC/USDT', D(1000), D(80), D(120), D(10), D(50), D(70), 2,
                            buy_grid_levels=3, sell_grid_levels=2)
        self.bot = OfflineGridBot(config, self.exchange, GridDatabase(self.path))
        alerts = patch.object(engine, 'schedule_telegram_alert', return_value=False)
        alerts.start()
        self.addCleanup(alerts.stop)
        self.bot.prepare(persist=True)
        self.bot.run_cycle()
        self.bot.run_cycle()

    def buy(self, level=1):
        return self.bot.database.fetch_latest_orders_by_level()[level]

    def rebound(self, buys):
        for buy in buys:
            self.exchange.fill(buy['client_order_id'])
        self.exchange.price = D(101)
        self.exchange.match_sells = True

    def exits(self, buy):
        return [row for row in self.bot.database.fetch_all_orders()
                if row['side'] == 'SELL' and row['parent_order_id'] == buy['order_id']]

    def test_fast_rebound_after_all_three_buys_executes_exits_and_records_actual_prices_once(self):
        buys = [self.buy(level) for level in (1, 2, 3)]
        self.rebound(buys)
        self.bot.run_cycle()
        self.bot.run_cycle()
        self.bot.run_cycle()
        trades = self.bot.database.fetch_trade_history()
        self.assertEqual(len(trades), 3)
        for buy in buys:
            exits = self.exits(buy)
            self.assertEqual(len(exits), 1)
            sell = exits[0]
            self.assertEqual(sell['status'], 'FILLED')
            actual = self.exchange.orders[sell['client_order_id']]
            self.assertEqual(actual['params']['timeInForce'], 'GTC')
            self.assertGreaterEqual(D(actual['average']), D(sell['price']))
            trade = self.bot.database.get_trade_by_sell_order_id(sell['order_id'])
            self.assertEqual(D(trade['sell_price']), D(101))
            expected = (D(actual['cost'])*(1-self.exchange.fee_rate)
                        - D(buy['price'])*D(buy['amount'])*(1+self.exchange.fee_rate))
            self.assertEqual(D(trade['profit']), expected)
            self.assertEqual(trade['fee_basis'], 'exchange')
        self.assertTrue(all(params['timeInForce']=='PO' for side, kind, params in
                            self.exchange.submissions if side=='buy' and kind=='limit'))
        self.assertEqual(self.bot.baseline_base, D(1))
        self.assertEqual(self.exchange.base_free, D(1))
        self.assertEqual(D(str(_portfolio_wallet(self.bot.database)['btc_held'])), D(5))
        self.assertEqual(self.exchange.market_sell_calls, 0)
        self.assertEqual(sum(kind=='market' and side=='buy'
                             for side, kind, _ in self.exchange.submissions), 1)

    def test_uncrossed_gtc_sell_rests_at_original_target(self):
        buy = self.buy()
        self.exchange.fill(buy['client_order_id'])
        self.exchange.price = D(95)
        self.exchange.match_sells = True
        self.bot.run_cycle()
        sell = self.exits(buy)[0]
        self.assertEqual((sell['status'], D(sell['price'])), ('OPEN', D(100)))
        self.assertEqual(self.exchange.orders[sell['client_order_id']]['filled'], '0')

    def test_equal_bid_and_limit_can_execute_without_maker_rejection(self):
        buy = self.buy()
        self.rebound([buy])
        self.exchange.price = D(100)
        self.bot.run_cycle()
        self.bot.run_cycle()
        self.assertEqual(self.exits(buy)[0]['status'], 'FILLED')

    def test_partial_immediate_exit_keeps_remainder_and_completes_without_duplicate(self):
        buy = self.buy()
        self.rebound([buy])
        self.exchange.fill_fraction = D('.30')
        self.bot.run_cycle()
        self.bot.run_cycle()
        sell = self.exits(buy)[0]
        self.assertEqual(self.bot.database.get_order(sell['order_id'])['status'], 'PARTIALLY_FILLED')
        self.assertIsNone(self.bot.database.get_trade_by_sell_order_id(sell['order_id']))
        actual = self.exchange.orders[sell['client_order_id']]
        self.assertGreater(D(actual['remaining']), 0)
        self.bot.run_cycle()
        self.assertEqual(len(self.exits(buy)), 1)
        self.exchange.price = D(102)
        self.exchange.fill(sell['client_order_id'])
        self.bot.run_cycle()
        self.bot.run_cycle()
        trade = self.bot.database.get_trade_by_sell_order_id(sell['order_id'])
        self.assertIsNotNone(trade)
        self.assertEqual(D(trade['sold_base']), D(buy['amount']))
        self.assertEqual(D(trade['sell_price']), D(actual['average']))
        self.assertEqual(len(self.exits(buy)), 1)
        self.assertEqual(len(self.bot.database.fetch_trade_history()), 1)

    def test_timeout_after_immediate_execution_recovers_same_id_without_second_sell(self):
        buy = self.buy()
        self.rebound([buy])
        self.exchange.failure = 'after'
        self.bot.run_cycle()
        self.bot.run_cycle()
        self.bot.run_cycle()
        self.assertEqual(len(self.exits(buy)), 1)
        self.assertEqual(len(self.bot.database.fetch_trade_history()), 1)
        self.assertEqual(sum(side=='sell' and params.get('timeInForce')=='GTC'
                             for side, _, params in self.exchange.submissions), 3)

    def test_timeout_without_confirmed_acceptance_retains_exit_intent(self):
        buy = self.buy()
        self.rebound([buy])
        self.exchange.failure = 'before'
        with self.assertRaises(UncertainOrderError):
            self.bot.run_cycle()
        sell = self.exits(buy)[0]
        self.assertIsNone(sell['exchange_order_id'])
        self.assertEqual(sell['status'], 'OPEN')
        self.assertEqual(len(self.exits(buy)), 1)
        self.assertEqual(len(self.bot.database.fetch_trade_history()), 0)

    def test_process_crash_after_execution_reconciles_and_accounts_once_on_restart(self):
        buy = self.buy()
        self.rebound([buy])
        self.exchange.failure = 'crash'
        with self.assertRaises(ProcessLost):
            self.bot.run_cycle()
        self.assertIsNone(self.exits(buy)[0]['exchange_order_id'])
        recovered = OfflineGridBot(self.bot.config, self.exchange, GridDatabase(self.path))
        recovered.prepare(persist=False)
        recovered._reconcile_state()
        recovered.run_cycle()
        recovered.run_cycle()
        self.assertEqual(len(self.exits(buy)), 1)
        self.assertEqual(len(recovered.database.fetch_trade_history()), 1)
        self.assertEqual(recovered.baseline_base, D(1))
        self.assertEqual(self.exchange.base_free, D(1))

    def test_macro_pause_does_not_block_marketable_sell_exit(self):
        buy = self.buy()
        self.rebound([buy])
        self.bot._macro_trend_provider = lambda: False
        self.bot.run_cycle()
        self.bot.run_cycle()
        self.assertEqual(len(self.bot.database.fetch_trade_history()), 1)
        self.assertTrue(all(row['side']=='SELL' for row in self.bot.database.fetch_active_grids()))

    def test_unexpected_maker_error_for_gtc_sell_keeps_uncertain_intent(self):
        buy = self.buy()
        self.rebound([buy])
        with patch.object(self.exchange, 'create_order', side_effect=ccxt.OrderImmediatelyFillable(
                'Order would immediately match and take.')):
            with self.assertRaises(UncertainOrderError):
                self.bot.run_cycle()
        self.assertEqual(len(self.exits(buy)), 1)
        self.assertIsNone(self.exits(buy)[0]['exchange_order_id'])

    def test_hard_stop_takes_priority_over_normal_exit_placement(self):
        buy = self.buy()
        self.rebound([buy])
        self.exchange.price = D(69)
        self.bot.run_cycle()
        self.assertEqual(self.exchange.market_sell_calls, 1)
        self.assertEqual(self.exchange.base_free, D(1))
        self.assertEqual(self.exits(buy), [])

    def test_reviewed_testnet_resume_preserves_working_orders_and_paired_exit_targets(self):
        buys = [self.buy(level) for level in (2, 3)]
        self.rebound(buys)
        self.bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        existing = {order['id'] for order in self.exchange.fetch_open_orders('BTC/USDT')}
        submitted = len(self.exchange.submissions)
        asyncio.run(main._reconcile_reviewed_testnet_resume(self.bot))
        self.assertEqual(len(self.exchange.submissions), submitted)
        self.assertEqual(self.exchange.cancel_calls, [])
        self.assertIsNone(self.bot.database.get_state('grid_reset'))
        self.assertEqual({order['id'] for order in self.exchange.fetch_open_orders('BTC/USDT')}, existing)
        self.bot.run_cycle()
        self.bot.run_cycle()
        self.assertEqual(len(self.bot.database.fetch_trade_history()), 2)
        for buy in buys:
            sell = self.exits(buy)[0]
            self.assertEqual(D(sell['price']), self.bot._paired_sell_price(buy))
        self.assertEqual(self.bot.baseline_base, D(1))

    def test_reviewed_resume_rejects_idle_halted_fault_pause_cleanup_and_reset(self):
        cases = [(ENGINE_STATUS_KEY, 'IDLE'), (ENGINE_STATUS_KEY, 'HALTED'),
                 (ENGINE_FAULT_KEY, 'test'), (ENGINE_STOP_CLEANUP_KEY, '1'),
                 (SAFETY_MODE_KEY, 'LIQUIDATED'), (LIQUIDATION_KEY, '{}'),
                 ('grid_reset', '{}'), ('halt_reason', 'test')]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
                self.bot.database.set_state(key, value)
                with self.assertRaises(TradingHalt):
                    asyncio.run(main._reconcile_reviewed_testnet_resume(self.bot))
                self.bot.database.clear_state(key)
        self.assertEqual(self.exchange.cancel_calls, [])

    def test_reviewed_resume_is_forbidden_on_mainnet(self):
        self.bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        with patch.object(main, 'TRADING_SETTINGS', SimpleNamespace(sandbox=False)):
            with self.assertRaisesRegex(TradingHalt, 'TESTNET-only'):
                asyncio.run(main._reconcile_reviewed_testnet_resume(self.bot))

    def test_reviewed_resume_rejects_unexplained_free_wallet_btc(self):
        self.bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        self.exchange.base_free += D(1)
        with self.assertRaisesRegex(TradingHalt, 'Unexplained wallet BTC'):
            asyncio.run(main._reconcile_reviewed_testnet_resume(self.bot))
        self.assertEqual(self.bot.baseline_base, D(1))

    def test_reviewed_resume_cli_requires_ready_mode(self):
        with patch('sys.argv', ['main.py', '--execute', '--resume-reviewed']):
            with self.assertRaises(SystemExit) as raised:
                main.main()
        self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
