"""Deterministic crash, retry, and partial-execution regressions; no network."""
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import Mock, patch

import ccxt

import engine
from database import GridDatabase
from execution_history import ExecutionHistoryPending, verified_execution_order
from exchange_handler import CircuitOpenError, network_backoff
from models import TradingHalt, UncertainOrderError
from portfolio import _portfolio_wallet
from settings import ENGINE_FAULT_KEY, FILL_SNAPSHOT_PREFIX
from tests.fakes import FakeSpotExchange, OfflineGridBot, make_bot


class HistoryExchange(FakeSpotExchange):
    def __init__(self):
        super().__init__()
        self.has = {'fetchOrderTrades': True}
        self.history_calls = []

    def fetch_order_trades(self, order_id, symbol, since=None, limit=None, params=None):
        self.history_calls.append(order_id)
        order = next(o for o in self.orders.values() if o['id'] == order_id)
        qty = D(order['filled'])
        if not qty:
            return []
        price = D(order['average'])
        return [{'id': order_id, 'order': order_id, 'symbol': symbol,
                 'amount': str(qty), 'cost': str(qty * price),
                 'info': {'qty': str(qty), 'quoteQty': str(qty * price),
                          'commission': '0', 'commissionAsset': 'USDT'}}]


class ExecutionRecoveryTests(unittest.TestCase):
    def test_liquidation_verifies_zero_commission_and_appears_once_in_recent_fills(self):
        bot, exchange = self.live(history=True)
        exchange.price = D('69')
        bot.run_cycle()
        liquidation = json.loads(bot.database.get_state('hard_stop_liquidation'))
        self.assertEqual(liquidation['phase'], 'complete')
        row = bot.database.get_trade_by_sell_order_id(liquidation['client_order_id'])
        expected = D(liquidation['proceeds_quote']) - D(liquidation['cost_basis_quote'])
        self.assertAlmostEqual(D(row['profit']), expected, places=20)
        self.assertEqual(row['fee_basis'], 'mixed')
        snapshot = json.loads(bot.database.get_state(FILL_SNAPSHOT_PREFIX + liquidation['client_order_id']))
        self.assertEqual(snapshot['fee_source'], 'trades')
        self.assertEqual(snapshot['fees'], [{'currency': 'USDT', 'cost': '0'}])
        recent = bot.database.fetch_recent_filled_orders()
        self.assertEqual(sum(r['order_id'] == row['sell_order_id'] for r in recent), 1)
        self.assertEqual(next(r for r in recent if r['order_id'] == row['sell_order_id'])['side'], 'SELL')
        before = len(bot.database.fetch_trade_history())
        bot.run_cycle()
        self.assertEqual(len(bot.database.fetch_trade_history()), before)
        self.assertEqual(exchange.market_sell_calls, 1)

    def test_liquidation_history_lag_retries_confirmation_without_another_sale(self):
        bot, exchange = self.live(history=True)
        original = exchange.fetch_order_trades
        state = {'lagged': False}
        def lag_once(order_id, *args, **kwargs):
            order = next(o for o in exchange.orders.values() if o['id'] == order_id)
            if order['type'] == 'market' and order['side'] == 'sell' and not state['lagged']:
                state['lagged'] = True
                return []
            return original(order_id, *args, **kwargs)
        exchange.price = D('69')
        with patch.object(exchange, 'fetch_order_trades', side_effect=lag_once), patch.object(engine.time, 'sleep'):
            bot.run_cycle()
        self.assertTrue(state['lagged'])
        self.assertEqual(exchange.market_sell_calls, 1)
        self.assertEqual(json.loads(bot.database.get_state('hard_stop_liquidation'))['phase'], 'complete')

    def test_verified_zero_buy_commission_is_not_estimated_again(self):
        bot, exchange = self.live(history=True)
        buy = bot.database.fetch_latest_orders_by_level()[1]
        exchange.fill(buy['client_order_id'])
        bot.run_cycle()
        sell = bot.database.fetch_latest_orders_by_level()[1]
        exchange.fill(sell['client_order_id'])
        bot.run_cycle()
        recorded = bot.database.get_trade_by_sell_order_id(sell['order_id'])
        quantity = D(sell['amount'])
        expected = quantity * (D(sell['price']) - D(buy['price']))
        self.assertEqual(D(recorded['profit']), expected)
        self.assertEqual(recorded['fee_basis'], 'exchange')
        before = bot.database.realized_pnl_summary()['total']
        bot._record_filled_sell(bot.database.get_order(sell['order_id']))
        self.assertEqual(bot.database.realized_pnl_summary()['total'], before)
        snapshot = json.loads(bot.database.get_state(FILL_SNAPSHOT_PREFIX + buy['order_id']))
        self.assertEqual(snapshot['fees'], [{'currency': 'USDT', 'cost': '0'}])

    def test_legacy_verified_zero_snapshot_is_upgraded_from_private_trades(self):
        bot, exchange = self.live(history=True)
        buy = bot.database.fetch_latest_orders_by_level()[1]
        exchange.fill(buy['client_order_id'])
        bot.run_cycle()
        key = FILL_SNAPSHOT_PREFIX + buy['order_id']
        old = json.loads(bot.database.get_state(key))
        old.pop('fees', None)
        bot.database.set_state(key, json.dumps(old))
        sell = bot.database.fetch_latest_orders_by_level()[1]
        exchange.fill(sell['client_order_id'])
        bot.run_cycle()
        self.assertEqual(bot.database.get_trade_by_sell_order_id(sell['order_id'])['fee_basis'], 'exchange')
        self.assertEqual(json.loads(bot.database.get_state(key))['fees'][0]['cost'], '0')

    def test_third_asset_buy_commission_remains_estimated_not_free(self):
        bot, exchange = self.live(history=True)
        original = exchange.fetch_order_trades
        def third_asset(*args, **kwargs):
            trades = original(*args, **kwargs)
            for trade in trades:
                trade['info'].update(commission='0.01', commissionAsset='BNB')
            return trades
        buy = bot.database.fetch_latest_orders_by_level()[1]
        exchange.fill(buy['client_order_id'])
        with patch.object(exchange, 'fetch_order_trades', side_effect=third_asset):
            bot.run_cycle()
        sell = bot.database.fetch_latest_orders_by_level()[1]
        exchange.fill(sell['client_order_id'])
        bot.run_cycle()
        self.assertEqual(bot.database.get_trade_by_sell_order_id(sell['order_id'])['fee_basis'], 'mixed')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'grid_testnet.sqlite'
        alerts = patch.object(engine, 'schedule_telegram_alert', return_value=False)
        alerts.start()
        self.addCleanup(alerts.stop)

    def live(self, history=False):
        bot, exchange = make_bot(self.path, initial_base=D(0),
                                exchange=HistoryExchange() if history else None)
        bot.run_cycle()
        bot.run_cycle()
        return bot, exchange

    def partial(self, exchange, row, fraction):
        order = exchange.orders[row['client_order_id']]
        qty = D(order['amount']) * fraction
        order.update(filled=str(qty), cost=str(qty * D(order['average'])))
        if row['side'] == 'BUY':
            exchange.base_free += qty
        else:
            exchange.quote_free += D(order['cost'])
        return qty

    def test_startup_recovers_ghost_buy_and_realized_sell_exactly_once(self):
        bot, exchange = self.live(history=True)
        buy = bot.database.fetch_latest_orders_by_level()[1]
        exchange.fill(buy['client_order_id'])  # Crash before SQLite sees execution.
        reopened = OfflineGridBot(bot.config, exchange, GridDatabase(self.path))
        reopened.prepare(persist=False)
        reopened._reconcile_state()
        self.assertEqual(reopened.database.get_order(buy['order_id'])['status'], 'FILLED')
        snapshot = json.loads(reopened.database.get_state(FILL_SNAPSHOT_PREFIX + buy['order_id']))
        self.assertEqual(snapshot['fee_source'], 'trades')
        reopened.run_cycle()
        sell = reopened.database.fetch_latest_orders_by_level()[1]
        self.assertEqual(sell['side'], 'SELL')
        exchange.fill(sell['client_order_id'])  # Crash before realized accounting.
        resumed = OfflineGridBot(bot.config, exchange, GridDatabase(self.path))
        resumed.prepare(persist=False)
        before = len(exchange.orders)
        resumed._reconcile_state()
        first = resumed.database.get_trade_by_sell_order_id(sell['order_id'])
        self.assertIsNotNone(first)
        resumed._reconcile_state()
        self.assertEqual(resumed.database.get_trade_by_sell_order_id(sell['order_id']), first)
        self.assertEqual(len(exchange.orders), before)

    def test_incomplete_startup_trade_history_blocks_start_and_preserves_ledger(self):
        bot, exchange = self.live(history=True)
        buy = bot.database.fetch_latest_orders_by_level()[1]
        exchange.fill(buy['client_order_id'])
        exchange.fetch_order_trades = Mock(return_value=[])
        before = len(exchange.orders)
        with self.assertRaisesRegex(TradingHalt, 'reconciliation failed'):
            bot.start_engine()
        self.assertTrue(bot.database.get_state(ENGINE_FAULT_KEY))
        self.assertEqual(len(exchange.orders), before)

    def test_reads_retry_exponentially_without_holding_exchange_lock(self):
        bot, exchange = make_bot(self.path)
        calls = 0
        def fetch_ticker(symbol):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ccxt.RequestTimeout('offline')
            return {'last': '100'}
        def wait(delay):
            self.assertFalse(bot.exchange_lock._is_owned())
            return False
        with patch.object(bot.stop_controller.stop_requested, 'wait', side_effect=wait) as waits, \
                patch.object(engine, 'network_backoff', side_effect=[1, 2]):
            self.assertEqual(bot._call(fetch_ticker, 'BTC/USDT'), {'last': '100'})
        self.assertEqual([call.args[0] for call in waits.call_args_list], [1, 2])

    def test_read_retry_is_bounded_and_stop_interrupts_backoff(self):
        bot, _ = make_bot(self.path)
        def fetch_ticker(symbol):
            raise ccxt.NetworkError('offline')
        with patch.object(bot.stop_controller.stop_requested, 'wait', return_value=False) as wait:
            with self.assertRaises(ccxt.NetworkError):
                bot._call(fetch_ticker, 'BTC/USDT')
            self.assertEqual(wait.call_count, 2)
        with patch.object(bot.stop_controller.stop_requested, 'wait', return_value=True) as wait:
            with self.assertRaises(ccxt.NetworkError):
                bot._call(fetch_ticker, 'BTC/USDT')
            self.assertEqual(wait.call_count, 1)

    def test_local_circuit_rejection_is_not_retried_inside_read(self):
        bot, _ = make_bot(self.path)
        def fetch_ticker(symbol):
            raise CircuitOpenError(60)
        with patch.object(bot.stop_controller.stop_requested, 'wait') as wait:
            with self.assertRaises(CircuitOpenError):
                bot._call(fetch_ticker, 'BTC/USDT')
            wait.assert_not_called()

    def test_accepted_create_timeout_recovers_same_id_without_duplicate_post(self):
        bot, exchange = make_bot(self.path)
        create = exchange.create_order
        def accepted(*args):
            create(*args)
            raise ccxt.RequestTimeout('response lost')
        with patch.object(exchange, 'create_order', side_effect=accepted) as post:
            client_id = bot._submit_order(1, 'BUY', D(90), D(1))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(exchange.orders), 1)
        self.assertIsNotNone(bot.database.get_order(client_id)['exchange_order_id'])

    def test_unknown_create_timeout_retains_intent_without_resubmitting(self):
        bot, exchange = make_bot(self.path)
        with patch.object(exchange, 'create_order', side_effect=ccxt.RequestTimeout('unknown')) as post:
            with self.assertRaises(UncertainOrderError):
                bot._submit_order(1, 'BUY', D(90), D(1))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(bot.database.fetch_active_grids()), 1)

    def test_process_dies_after_acceptance_before_id_commit_then_restart_attaches_original(self):
        bot, exchange = make_bot(self.path,initial_base=D(0),exchange=HistoryExchange())
        create = exchange.create_order
        before = len(exchange.orders)
        def accepted_then_killed(*args):
            create(*args)
            raise SystemExit('Injected process death after Binance acceptance')
        with patch.object(exchange,'create_order',side_effect=accepted_then_killed):
            with self.assertRaises(SystemExit):
                bot._submit_order(1,'BUY',D('90'),D('1'))
        intent = bot.database.fetch_latest_orders_by_level()[1]
        self.assertIsNone(intent['exchange_order_id'])
        reopened = OfflineGridBot(bot.config,exchange,GridDatabase(self.path))
        reopened.prepare(persist=False)
        reopened._reconcile_state()
        attached = reopened.database.get_order(intent['order_id'])
        self.assertEqual(attached['exchange_order_id'],exchange.orders[intent['client_order_id']]['id'])
        self.assertEqual(len(exchange.orders),before+1)

    def test_fill_during_disconnection_is_recovered_before_replacement_sell(self):
        bot, exchange = self.live(history=True)
        buy = bot.database.fetch_latest_orders_by_level()[1]
        before = len(exchange.orders)
        with patch.object(exchange,'fetch_ticker',side_effect=ccxt.NetworkError('Disconnected')):
            with self.assertRaises(ccxt.NetworkError):bot.run_cycle()
        exchange.fill(buy['client_order_id'])
        self.assertEqual(bot.database.get_order(buy['order_id'])['status'],'OPEN')
        self.assertEqual(len(exchange.orders),before)
        bot.run_cycle()
        sell=bot.database.fetch_latest_orders_by_level()[1]
        self.assertEqual(sell['side'],'SELL')
        self.assertEqual(sell['parent_order_id'],buy['order_id'])
        count=len(exchange.orders)
        bot.run_cycle()
        self.assertEqual(len(exchange.orders),count)

    def test_sellable_thirty_percent_buy_cancels_remainder_then_places_one_sell(self):
        bot, exchange = self.live()
        buy = bot.database.fetch_latest_orders_by_level()[1]
        qty = self.partial(exchange, buy, D('.30'))
        bot.run_cycle()
        sell = bot.database.fetch_latest_orders_by_level()[1]
        self.assertEqual(bot.database.get_order(buy['order_id'])['status'], 'CANCELED')
        self.assertEqual(sell['side'], 'SELL')
        self.assertLessEqual(D(sell['amount']), qty)
        count = len(exchange.orders)
        bot.run_cycle()
        self.assertEqual(len(exchange.orders), count)

    def test_partial_buy_cancel_timeout_recovers_on_next_cycle_without_extra_sell(self):
        bot, exchange = self.live()
        buy = bot.database.fetch_latest_orders_by_level()[1]
        self.partial(exchange, buy, D('.30'))
        exchange.cancel_timeout = 'after'
        with self.assertRaises(ccxt.RequestTimeout):
            bot.run_cycle()
        bot.run_cycle()
        self.assertEqual(bot.database.fetch_latest_orders_by_level()[1]['side'], 'SELL')

    def test_restart_after_partial_cancel_timeout_recovers_filled_quantity(self):
        bot, exchange = self.live(history=True)
        buy = bot.database.fetch_latest_orders_by_level()[1]
        qty = self.partial(exchange, buy, D('.30'))
        exchange.cancel_timeout = 'after'
        with self.assertRaises(ccxt.RequestTimeout):
            bot.run_cycle()
        reopened = OfflineGridBot(bot.config, exchange, GridDatabase(self.path))
        reopened.prepare(persist=False)
        reopened._reconcile_state()
        reopened.run_cycle()
        sell = reopened.database.fetch_latest_orders_by_level()[1]
        self.assertEqual(sell['side'], 'SELL')
        self.assertEqual(D(sell['amount']), reopened._amount(qty))

    def test_fill_winning_partial_cancel_race_uses_final_quantity(self):
        bot, exchange = self.live(history=True)
        buy = bot.database.fetch_latest_orders_by_level()[1]
        self.partial(exchange, buy, D('.30'))
        cancel = exchange.cancel_order
        def fill_before_cancel(order_id, symbol, params=None):
            order = exchange.orders[buy['client_order_id']]
            remaining = D(order['amount']) - D(order['filled'])
            exchange.base_free += remaining
            order.update(status='closed', filled=order['amount'],
                         cost=str(D(order['amount']) * D(order['average'])))
            return cancel(order_id, symbol, params)
        with patch.object(exchange, 'cancel_order', side_effect=fill_before_cancel):
            bot.run_cycle()
        sell = bot.database.fetch_latest_orders_by_level()[1]
        self.assertEqual(bot.database.get_order(buy['order_id'])['status'], 'FILLED')
        self.assertEqual(D(sell['amount']), D(buy['amount']))

    def test_below_minimum_partial_stays_open_and_other_lane_completes(self):
        bot, exchange = self.live()
        latest = bot.database.fetch_latest_orders_by_level()
        self.partial(exchange, latest[1], D('.001'))
        exchange.fill(latest[2]['client_order_id'])
        bot.run_cycle()
        current = bot.database.fetch_latest_orders_by_level()
        self.assertEqual(current[1]['status'], 'PARTIALLY_FILLED')
        self.assertEqual(current[2]['side'], 'SELL')
        self.assertNotIn(latest[1]['client_order_id'], exchange.cancel_calls)

    def test_partial_sell_stays_working_then_completes_and_rearms_buy(self):
        bot, exchange = self.live()
        sell = bot.database.fetch_latest_orders_by_level()[-1]
        self.partial(exchange, sell, D('.30'))
        bot.run_cycle()
        self.assertEqual(bot.database.get_order(sell['order_id'])['status'], 'PARTIALLY_FILLED')
        self.assertNotIn(sell['client_order_id'], exchange.cancel_calls)
        order = exchange.orders[sell['client_order_id']]
        order.update(status='closed', filled=order['amount'],
                     cost=str(D(order['amount']) * D(order['average'])))
        bot.run_cycle()
        self.assertIsNotNone(bot.database.get_trade_by_sell_order_id(sell['order_id']))
        self.assertEqual(bot.database.fetch_latest_orders_by_level()[-1]['side'], 'BUY')

    def test_fill_snapshot_never_regresses_on_stale_exchange_response(self):
        bot, exchange = self.live()
        buy = bot.database.fetch_latest_orders_by_level()[1]
        self.partial(exchange, buy, D('.30'))
        order = bot._fetch_order(buy)
        before = bot.database.get_state(FILL_SNAPSHOT_PREFIX + buy['order_id'])
        with self.assertRaisesRegex(TradingHalt, 'regressed'):
            bot._persist_order_response(buy, dict(order, filled='0'))
        self.assertEqual(bot.database.get_state(FILL_SNAPSHOT_PREFIX + buy['order_id']), before)

    def test_trade_history_pagination_is_complete_and_deduplicated(self):
        def trade(i):
            return {'id': str(i), 'order': '7', 'amount': '.001', 'cost': '.1',
                    'fee': {'currency': 'USDT', 'cost': '.0001'}}
        exchange = Mock(has={'fetchOrderTrades': True})
        call = Mock(side_effect=[[trade(i) for i in range(500)], [trade(499), trade(500)]])
        result = verified_execution_order(call, exchange, 'BTC/USDT',
                                         {'id': '7', 'filled': '.501'})
        self.assertEqual(D(result['cost']), D('50.1'))
        self.assertEqual(D(result['fees'][0]['cost']), D('.0501'))
        self.assertEqual(call.call_args_list[0].args[-1], {'fromId': 0})
        self.assertEqual(call.call_args.args[-1], {'fromId': 500})

    def test_numeric_format_changes_do_not_erase_verified_commissions(self):
        bot, exchange = make_bot(self.path)
        client_id = bot._submit_order(1, 'BUY', D(90), D(1))
        exchange.fill(client_id)
        order = exchange.fetch_order('1', 'BTC/USDT')
        bot._save_fill_snapshot(client_id, dict(order, filled='1.00000000'),
                                verified_fees=(D('.001'), D(0)))
        bot._save_fill_snapshot(client_id, dict(order, filled=1.0, fees=[]))
        snapshot = json.loads(bot.database.get_state(FILL_SNAPSHOT_PREFIX + client_id))
        self.assertEqual(snapshot['fee_source'], 'trades')
        self.assertEqual(D(snapshot['base_fee']), D('.001'))

    def test_unrelated_or_incomplete_executions_cannot_be_adopted(self):
        exchange = Mock(has={'fetchOrderTrades': True})
        for trades in ([], [{'order': 'other', 'amount': '1'}]):
            with self.subTest(trades=trades), self.assertRaises(ExecutionHistoryPending):
                verified_execution_order(Mock(return_value=trades), exchange, 'BTC/USDT',
                                         {'id': '7', 'filled': '1'})

    def test_backoff_stays_capped(self):
        for attempt in (0, 1, 8, 100):
            value = network_backoff(attempt)
            self.assertGreaterEqual(value, min(60, 2 ** min(attempt, 10)) / 2)
            self.assertLessEqual(value, 60)


if __name__ == '__main__':
    unittest.main()
