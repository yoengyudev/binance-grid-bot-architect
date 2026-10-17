"""Regression coverage for exchange-tick bounds; all exchange activity is fake."""

import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import ccxt

from database import GridDatabase
from tests.fakes import OfflineGridBot as GridBot
from models import GridConfig, TradingHalt
from settings import BREAKOUT_TIMER_KEY
from tests.fakes import FakeSpotExchange


class TickSizeExchange(FakeSpotExchange):
    """Use the actual CCXT Binance price formatter with offline market metadata."""

    def __init__(self, tick_size=Decimal('0.01')):
        super().__init__()
        self.formatter = ccxt.binance()
        self.formatter.set_markets([{
            'id': 'BTCUSDT', 'symbol': 'BTC/USDT', 'base': 'BTC', 'quote': 'USDT',
            'baseId': 'BTC', 'quoteId': 'USDT', 'type': 'spot', 'spot': True,
            'active': True, 'precision': {'price': float(tick_size), 'amount': 0.00001},
            'limits': {'amount': {'min': 0.00001}, 'cost': {'min': 5}},
            'info': {'filters': [{'filterType': 'PRICE_FILTER', 'tickSize': str(tick_size)}]},
        }])

    def market(self, symbol):
        return self.formatter.market(symbol)

    def price_to_precision(self, symbol, price):
        return self.formatter.price_to_precision(symbol, price)

    def amount_to_precision(self, _symbol, amount):
        return str(Decimal(amount).quantize(Decimal('0.00001'), rounding=ROUND_DOWN))


class PriceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.exchange = TickSizeExchange()
        self.exchange.price = Decimal('85067.41')
        self.exchange.base_free = Decimal(0)
        config = GridConfig(
            'BTC/USDT', Decimal('100'), Decimal('84733.450000000008206436784'),
            Decimal('85401.369999999991793563216'), Decimal('2.5'), Decimal('50'),
            Decimal('84300'), 2, buy_grid_levels=4, sell_grid_levels=4,
        )
        self.bot = GridBot(config, self.exchange,
                           GridDatabase(Path(self.directory.name) / 'grid_testnet.sqlite'))

    def filled_upper_buy(self):
        self.bot.prepare(persist=True)
        self.bot.run_cycle()
        self.bot.run_cycle()
        sell = self.bot.database.fetch_latest_orders_by_level()[-4]
        self.assertEqual(Decimal(sell['price']), Decimal('85401.37'))
        self.exchange.fill(sell['client_order_id'])
        self.bot.run_cycle()
        buy = self.bot.database.fetch_latest_orders_by_level()[-4]
        self.assertEqual(buy['side'], 'BUY')
        self.assertEqual(Decimal(buy['price']), Decimal('85317.76'))
        self.exchange.fill(buy['client_order_id'])
        return buy

    def test_production_sell_buy_sell_cycle_accepts_rounded_ceiling(self):
        self.filled_upper_buy()
        self.bot.run_cycle()
        sell = self.bot.database.fetch_latest_orders_by_level()[-4]
        self.assertEqual(sell['side'], 'SELL')
        self.assertEqual(sell['status'], 'OPEN')
        self.assertEqual(Decimal(sell['price']), Decimal('85401.37'))
        self.assertFalse(self.bot.stop_controller.stop_requested.is_set())

    def test_sell_one_tick_above_ceiling_still_halts_without_submission(self):
        buy = self.filled_upper_buy()
        self.bot.upper_levels[-1] = Decimal('85401.38')
        next_id = self.exchange.next_id
        with self.assertRaisesRegex(TradingHalt, 'above the upper grid bound'):
            self.bot._place_sell(buy)
        self.assertEqual(self.exchange.next_id, next_id)
        self.assertEqual(self.bot.database.fetch_latest_orders_by_level()[-4]['side'], 'BUY')

    def test_buy_one_tick_below_floor_still_halts_without_submission(self):
        self.bot.prepare(persist=True)
        self.bot.levels[-1] = Decimal('84733.44')
        with self.assertRaisesRegex(TradingHalt, 'below the lower grid bound'):
            self.bot._place_buy(4, None)
        self.assertEqual(self.exchange.orders, {})
        self.assertEqual(self.bot.database.fetch_all_orders(), [])

    def test_seed_sell_one_tick_above_ceiling_still_halts(self):
        self.bot.prepare(persist=True)
        self.bot.run_cycle()
        seed = self.bot.database.fetch_latest_orders_by_level()[0]
        self.bot.upper_levels[-1] = Decimal('85401.38')
        next_id = self.exchange.next_id
        with self.assertRaisesRegex(TradingHalt, 'above the upper grid bound'):
            self.bot._place_seed_upper_sell(-4, seed)
        self.assertEqual(self.exchange.next_id, next_id)

    def test_bounds_and_order_prices_use_each_markets_tick_size(self):
        for tick in (Decimal('0.01'), Decimal('0.05'), Decimal('0.00000001')):
            with self.subTest(tick=tick):
                self.bot.exchange = TickSizeExchange(tick)
                self.bot.config = replace(self.bot.config,
                    lower_price=Decimal('80.0000000000001'),
                    upper_price=Decimal('119.9999999999999'))
                for side in ('BUY', 'SELL'):
                    self.assertEqual(self.bot._grid_order_price(Decimal('80'), side), Decimal('80'))
                    self.assertEqual(self.bot._grid_order_price(Decimal('120'), side), Decimal('120'))
                    with self.assertRaises(TradingHalt):
                        self.bot._grid_order_price(Decimal('80') - tick, side)
                    with self.assertRaises(TradingHalt):
                        self.bot._grid_order_price(Decimal('120') + tick, side)
                self.assertEqual(self.bot._price(Decimal('100') + tick * Decimal('0.6')),
                                 Decimal('100') + tick)

    def test_inclusive_and_strict_checks_agree_at_rounded_bounds(self):
        for price in (Decimal('84733.45'), Decimal('85401.37')):
            self.assertTrue(self.bot._within_grid_bounds(price))
            self.assertFalse(self.bot._within_grid_bounds(price, strict=True))
        self.assertFalse(self.bot._within_grid_bounds(Decimal('84733.44')))
        self.assertFalse(self.bot._within_grid_bounds(Decimal('85401.38')))
        self.assertTrue(self.bot._within_grid_bounds(Decimal('85067.41'), strict=True))

    def test_ceiling_float_artifact_does_not_start_breakout_timer(self):
        self.bot.prepare(persist=True)
        self.bot.run_cycle()  # Hold seed BTC so breakout tracking is enabled.
        self.assertFalse(self.bot._observe_upper_breakout(Decimal('85401.37'), now=1))
        self.assertIsNone(self.bot.database.get_state(BREAKOUT_TIMER_KEY))
        self.assertFalse(self.bot._observe_upper_breakout(Decimal('85401.38'), now=2))
        self.assertIsNotNone(self.bot.database.get_state(BREAKOUT_TIMER_KEY))


if __name__ == '__main__':
    unittest.main()
