"""Fee defense and nonfatal re-anchor conflicts; all exchange I/O is fake."""
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest

from backtest_grid import Config, GridBacktest, atr_bounds
from grid_geometry import defended_bounds, minimum_cycle_return, validate_grid_returns
from settings import SAFETY_MODE_KEY, LIQUIDATED
import pandas as pd
from tests.fakes import make_bot, restore_retained_floor
from tests.test_backtest_grid import START
from tests.test_price_boundaries import TickSizeExchange
from tests.fakes import OfflineGridBot as GridBot
from database import GridDatabase
import json
from unittest.mock import patch


class FeeDefenseTests(unittest.TestCase):
    def grid(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        bot, exchange = make_bot(Path(directory.name) / 'grid_testnet.sqlite')
        bot.run_cycle()
        bot.run_cycle()
        return bot, exchange

    def test_atr_floor_survives_real_ccxt_rounding_for_even_and_odd_counts(self):
        for tick in (D('.01'), D('.05'), D('.00000001')):
            exchange = TickSizeExchange(tick)
            quantize = lambda p: D(exchange.price_to_precision('BTC/USDT', str(p)))
            for levels in (2, 8, 9, 50, 100):
                nb, ns = (levels + 1) // 2, levels // 2
                center = D('81260')
                low, high = defended_bounds(center, D('20'), nb, ns, tick=tick, round_price=quantize)
                sides = []
                for bound, count in ((low, nb), (high, ns)):
                    sides.append([quantize(bound if i == count else center * (bound / center) ** (D(i) / count))
                                  for i in range(1, count + 1)])
                self.assertGreaterEqual(minimum_cycle_return(quantize(center), *sides), D('.008'))

    def test_rounded_exact_boundary_and_one_tick_violation(self):
        validate_grid_returns(D(100), [D('99.20')], [D('100.80')])
        with self.assertRaisesRegex(ValueError, 'Fee defense'):
            validate_grid_returns(D(100), [D('99.20')], [D('100.79')])

    def test_simulator_atr_widens_and_places_only_defended_pairs(self):
        config = Config(stop_mode='fixed', trend_filter=False)
        low, high, stop = atr_bounds(D('81260'), D(20), config)
        self.assertLess(stop, low)
        self.assertGreater(high - D('81260'), D(40))
        bot = GridBacktest(config)
        bot.start_grid(START, D('81260'), D(20))
        for lane in bot.lanes.values():
            self.assertGreaterEqual(lane.sell_price / lane.buy_price - 1, D('.008'))
        self.assertGreaterEqual(bot.minimum_installed_grid_return, D('.008'))

    def test_simulator_defers_floor_conflict_without_orders_cost_or_floor_changes(self):
        bot = GridBacktest(Config(trend_filter=False, stop_mode='fixed'))
        bot.start_grid(START, D(100), D(1))
        bot._point(D(105), START)
        bot.stop = D('104')  # Retained raised floor cannot fit four 0.8% BUYs.
        orders, floor, held = dict(bot.orders), bot.stop, bot.held
        cost = sum(lot.cost for lot in bot.lots.values())
        bot._breakout(D(105), START + pd.Timedelta(hours=4))
        self.assertEqual(bot.mode, 'RUNNING')
        self.assertEqual(bot.orders, orders)
        self.assertEqual((bot.stop, bot.held), (floor, held))
        self.assertEqual(sum(lot.cost for lot in bot.lots.values()), cost)
        self.assertEqual(bot.reanchor_floor_deferrals, 1)

    def test_backend_floor_conflict_does_not_queue_or_cancel_existing_orders(self):
        bot, exchange = self.grid()
        exchange.price = D(200)
        restore_retained_floor(bot, 170, 200)
        orders, floor = list(exchange.fetch_open_orders('BTC/USDT')), bot.config.stop_loss_price
        bot._request_breakout_reset(exchange.price)
        self.assertFalse(bot.grid_needs_reset)
        self.assertIsNone(bot.database.get_state('grid_reset'))
        self.assertEqual(exchange.fetch_open_orders('BTC/USDT'), orders)
        self.assertEqual(bot.config.stop_loss_price, floor)
        self.assertIsNone(bot.database.get_state('halt_reason'))
        # The deferred proposal never disables liquidation protection.
        exchange.price = floor - 1
        bot.run_cycle()
        self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)

    def test_queued_reset_rechecks_floor_before_cancelling(self):
        bot, exchange = self.grid()
        exchange.price = D(130)
        bot._request_breakout_reset(exchange.price)
        self.assertTrue(bot.grid_needs_reset)
        restore_retained_floor(bot, 125, 155)
        before = exchange.cancel_calls.copy()
        sell_ids = {o['id'] for o in exchange.fetch_open_orders('BTC/USDT') if o['side'] == 'sell'}
        self.assertFalse(bot.reset_grid())
        self.assertFalse(bot.grid_needs_reset)
        self.assertEqual({o['id'] for o in exchange.fetch_open_orders('BTC/USDT') if o['side'] == 'sell'}, sell_ids)
        self.assertIsNone(bot.database.get_state('halt_reason'))
        self.assertGreaterEqual(len(exchange.cancel_calls), len(before))  # Below-stop BUY cleanup is allowed.

    def test_manual_tight_grid_rejected_before_any_exchange_write(self):
        bot, exchange = self.grid()
        before, next_id = exchange.cancel_calls.copy(), exchange.next_id
        with self.assertRaisesRegex(ValueError, 'Fee defense'):
            bot.request_manual_recenter('100', '1', '30', '1000', 8)
        self.assertEqual(exchange.cancel_calls, before)
        self.assertEqual(exchange.next_id, next_id)
        self.assertIsNone(bot.database.get_state('grid_reset'))

    def test_undersized_carry_defers_before_cancelling_live_grid(self):
        bot, exchange = self.grid()
        exchange.price = D(130)
        before = exchange.fetch_open_orders('BTC/USDT')
        with patch.object(bot, '_bot_btc_balance', return_value=D('.001')):
            bot._request_breakout_reset(exchange.price)
        self.assertFalse(bot.grid_needs_reset)
        self.assertEqual(exchange.fetch_open_orders('BTC/USDT'), before)
        self.assertIsNone(bot.database.get_state('grid_reset'))

    def test_reanchor_funding_counts_only_own_releasable_buy_quote(self):
        bot, exchange = self.grid()
        exchange.price = D(130)
        candidate = bot._breakout_candidate(exchange.price, bot.breakout_width_percent)
        exchange.quote_free = D(0)
        self.assertTrue(bot._breakout_fundable(exchange.price, candidate))
        buy = next(o for o in exchange.fetch_open_orders('BTC/USDT') if o['side'] == 'buy')
        exchange.cancel_order(buy['id'], 'BTC/USDT')
        exchange.quote_free = D(0)  # Simulate quote withdrawn after releasing that order.
        self.assertFalse(bot._breakout_fundable(exchange.price, candidate))
        before = exchange.fetch_open_orders('BTC/USDT')
        bot._request_breakout_reset(exchange.price)
        self.assertFalse(bot.grid_needs_reset)
        self.assertEqual(exchange.fetch_open_orders('BTC/USDT'), before)

    def test_floor_race_after_cancellation_survives_restart_and_still_liquidates(self):
        bot, exchange = self.grid()
        exchange.price = D(130)
        bot._request_breakout_reset(exchange.price)
        original = exchange.cancel_order
        moved = False

        def cancel_and_raise_floor(order_id, symbol, params=None):
            nonlocal moved
            response = original(order_id, symbol, params)
            if not moved:
                moved = True
                restore_retained_floor(bot, 170, 200)
                exchange.price = D(180)
            return response

        exchange.cancel_order = cancel_and_raise_floor
        self.assertFalse(bot.reset_grid())
        pending = json.loads(bot.database.get_state('grid_reset'))
        self.assertTrue(pending['cancellation_verified'])
        self.assertTrue(bot.grid_needs_reset)
        self.assertEqual(bot.config.stop_loss_price, D(170))
        self.assertIsNone(bot.database.get_state('halt_reason'))
        recovered = GridBot(bot.config, exchange, GridDatabase(bot.database.path))
        recovered.prepare(persist=False)
        self.assertFalse(recovered.reset_grid())
        self.assertEqual(recovered.config.stop_loss_price, D(170))
        self.assertEqual(exchange.fetch_open_orders('BTC/USDT'), [])
        exchange.price = D(169)
        self.assertFalse(recovered.reset_grid())
        self.assertEqual(recovered.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)
        self.assertEqual(exchange.market_sell_calls, 1)

if __name__ == '__main__':
    unittest.main()
