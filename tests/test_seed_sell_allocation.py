"""Seed SELL allocation consumes all tradable BTC without reusing sold inventory."""
import tempfile
import unittest
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from unittest.mock import patch

import engine
from database import GridDatabase
from tests.fakes import OfflineGridBot as GridBot
from models import GridConfig, TradingHalt
from settings import ENGINE_RUNNING, ENGINE_STATUS_KEY, LIQUIDATION_KEY
from tests.fakes import FakeSpotExchange


class BinanceStepExchange(FakeSpotExchange):
    def __init__(self, fee=Decimal(0)):
        super().__init__()
        self.price = Decimal('85372.01')
        self.base_free = Decimal('0.00001')  # An old dust reserve outside this grid.
        self.has = {'fetchOrderTrades': True}
        self.buy_fee = fee

    def market(self, symbol):
        market = super().market(symbol)
        market['limits'] = {'amount': {'min': '0.00001'}, 'cost': {'min': '5'}}
        return market

    def amount_to_precision(self, symbol, amount):
        return str(Decimal(amount).quantize(Decimal('0.00001'), rounding=ROUND_DOWN))

    def create_order(self, symbol, order_type, side, amount, price, params):
        order = super().create_order(symbol, order_type, side, amount, price, params)
        if order_type == 'market' and side == 'buy':
            self.base_free -= self.buy_fee
            self.orders[order['clientOrderId']]['fees'] = [{'currency': 'BTC', 'cost': str(self.buy_fee)}]
            order = dict(self.orders[order['clientOrderId']])
        return order

    def fetch_order_trades(self, order_id, symbol):
        order = next(order for order in self.orders.values() if order['id'] == order_id)
        fee = sum((Decimal(item['cost']) for item in order['fees']
                   if item['currency'] == 'BTC'), Decimal(0))
        return [{'order': order_id, 'amount': order['filled'],
                 'fee': {'currency': 'BTC', 'cost': str(fee)},
                 'info': {'qty': order['filled'], 'commission': str(fee), 'commissionAsset': 'BTC'}}]


class SeedSellAllocationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'grid_testnet.sqlite'
        alerts = patch.object(engine, 'schedule_telegram_alert', return_value=False)
        alerts.start()
        self.addCleanup(alerts.stop)

    def grid(self, *, fee=Decimal(0)):
        exchange = BinanceStepExchange(fee)
        config = GridConfig(
            'BTC/USDT', Decimal('50'), Decimal('84000'), Decimal('85839.88'),
            Decimal('1'), Decimal('50'), Decimal('83000'), 2,
            buy_grid_levels=2, sell_grid_levels=2,
        )
        bot = GridBot(config, exchange, GridDatabase(self.path))
        bot.prepare(persist=True)
        bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        bot.run_cycle()
        return bot, exchange, bot.database.fetch_latest_orders_by_level()[0]

    @staticmethod
    def seed_sells(bot, seed):
        return [row for row in bot.database.fetch_all_orders()
                if row['side'] == 'SELL' and row['parent_order_id'] == seed['order_id']]

    def test_reported_seed_sells_every_tick_and_remains_active_when_flat(self):
        bot, exchange, seed = self.grid()
        self.assertEqual(Decimal(seed['amount']), Decimal('0.00029'))
        bot.run_cycle()
        sells = sorted(self.seed_sells(bot, seed), key=lambda row: -row['level'])
        self.assertEqual([Decimal(row['amount']) for row in sells],
                         [Decimal('0.00014'), Decimal('0.00015')])
        buys = {row['order_id'] for row in bot.database.fetch_active_grids() if row['side'] == 'BUY'}
        bot.advance_trailing_stop(Decimal('90000'))
        for sell in sells:
            exchange.fill(sell['client_order_id'])
        exchange.price = Decimal('86000')  # Below the raised stop, above the grid.
        bot.run_cycle()
        self.assertEqual(bot._bot_btc_balance(refresh=False), 0)
        self.assertEqual(exchange.base_free, Decimal('0.00001'))
        self.assertEqual({row['order_id'] for row in bot.database.fetch_active_grids()
                          if row['side'] == 'BUY'}, buys)
        self.assertEqual(exchange.market_sell_calls, 0)
        self.assertIsNone(bot.database.get_state(LIQUIDATION_KEY))
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_RUNNING)

    def test_more_lanes_share_remainders_and_nondecimal_step_sizes(self):
        bot, exchange, _ = self.grid()
        amounts = bot._split_sell_amounts(Decimal('0.00029'), [-1, -2, -3])
        self.assertEqual(list(amounts.values()), [Decimal('0.00009'), Decimal('0.00010'), Decimal('0.00010')])
        self.assertEqual(sum(amounts.values()), Decimal('0.00029'))
        def stepped(symbol, amount):
            value = Decimal(amount)
            return str((value / Decimal('0.00005')).to_integral_value(rounding=ROUND_DOWN)
                       * Decimal('0.00005'))
        with patch.object(exchange, 'amount_to_precision', side_effect=stepped):
            amounts = bot._split_sell_amounts(Decimal('0.00035'), [-1, -2, -3])
        self.assertEqual(list(amounts.values()), [Decimal('0.00010'), Decimal('0.00010'), Decimal('0.00015')])
        self.assertEqual(sum(amounts.values()), Decimal('0.00035'))

    def test_actual_btc_commission_is_deducted_before_allocating(self):
        fee = Decimal('0.00000029')
        bot, exchange, seed = self.grid(fee=fee)
        bot.run_cycle()
        sells = self.seed_sells(bot, seed)
        total = sum((Decimal(row['amount']) for row in sells), Decimal(0))
        net = Decimal(seed['amount']) - fee
        self.assertEqual(total, Decimal('0.00028'))
        self.assertLessEqual(total, net)
        self.assertEqual(net - total, Decimal('0.00000971'))  # Below one exchange step.
        self.assertGreaterEqual(exchange.base_free, bot.baseline_base)

    def test_restart_after_first_submission_allocates_only_the_unsold_share(self):
        bot, exchange, seed = self.grid()
        original = bot._place_seed_upper_sell
        def crash_after_submission(level, seed_row):
            original(level, seed_row)
            raise KeyboardInterrupt('Crash after the first durable SELL')
        with patch.object(bot, '_place_seed_upper_sell', side_effect=crash_after_submission):
            with self.assertRaises(KeyboardInterrupt):
                bot.run_cycle()
        self.assertEqual(len(self.seed_sells(bot, seed)), 1)
        reopened = GridBot(bot.config, exchange, GridDatabase(self.path))
        reopened.prepare(persist=False)
        reopened._reconcile_state()
        reopened.run_cycle()
        sells = self.seed_sells(reopened, seed)
        self.assertEqual(len(sells), 2)
        self.assertEqual(sum((Decimal(row['amount']) for row in sells), Decimal(0)), Decimal('0.00029'))
        self.assertEqual(sum(order['side'] == 'sell' and order['type'] == 'limit'
                             for order in exchange.orders.values()), 2)
        self.assertGreaterEqual(exchange.base_free, bot.baseline_base)

    def test_a_filled_seed_sell_still_counts_as_allocated_after_restart(self):
        bot, exchange, seed = self.grid()
        bot._place_seed_upper_sell(-1, seed)
        first = self.seed_sells(bot, seed)[0]
        exchange.fill(first['client_order_id'])
        bot._fetch_order(first)
        allocations = bot._seed_upper_sell_allocations(seed)
        self.assertEqual(allocations, {-1: Decimal('0.00014'), -2: Decimal('0.00015')})
        bot.run_cycle()
        self.assertEqual(sum((Decimal(row['amount']) for row in self.seed_sells(bot, seed)), Decimal(0)), Decimal('0.00029'))

    def test_legacy_partial_batch_and_overallocated_intents_are_handled_safely(self):
        bot, exchange, seed = self.grid()
        # An older release could already have placed the final, smaller lane.
        bot._submit_order(-2, 'SELL', bot._price(bot.upper_levels[1]), Decimal('0.00014'),
                          parent_order_id=seed['order_id'])
        bot.run_cycle()
        sells = self.seed_sells(bot, seed)
        self.assertEqual({row['level']: Decimal(row['amount']) for row in sells},
                         {-2: Decimal('0.00014'), -1: Decimal('0.00015')})
        before = len(exchange.orders)
        bot.database.insert_order('extra-seed-sell', -1, 'SELL', '85500', '0.00015',
                                  parent_order_id=seed['order_id'])
        with self.assertRaisesRegex(TradingHalt, 'duplicated'):
            bot._seed_upper_sell_allocations(seed)
        self.assertEqual(len(exchange.orders), before)

    def test_rounding_up_inventory_is_rejected_before_any_seed_sell(self):
        bot, exchange, seed = self.grid()
        before = len(exchange.orders)
        with patch.object(exchange, 'amount_to_precision', return_value='0.00030'):
            with self.assertRaisesRegex(TradingHalt, 'above net received'):
                bot._seed_upper_sell_allocations(seed)
        self.assertEqual(len(exchange.orders), before)


if __name__ == '__main__':
    unittest.main()
