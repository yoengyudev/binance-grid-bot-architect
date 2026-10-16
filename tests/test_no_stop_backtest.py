"""No-stop holding, SMA BUY pauses, and honest inventory valuation."""
from decimal import Decimal as D
import unittest
from unittest.mock import patch

import pandas as pd

from backtest_grid import Config, GridBacktest, atr_bounds
from tests.test_backtest_grid import START, fine_candles
from tests.test_daily_macro_backtest import daily_history


class NoStopBacktestTests(unittest.TestCase):
    def bot(self):
        bot = GridBacktest(Config(stop_mode='none'))
        bot.macro_sma = D(99)
        bot.start_grid(START, D(100), D(1))
        return bot

    def test_explicit_no_stop_has_no_stop_and_no_stop_distance(self):
        bot = self.bot()
        self.assertEqual(bot.c.stop_mode, 'none')
        self.assertIsNone(atr_bounds(D(100), D(1), bot.c)[2])
        self.assertIsNone(bot.stop)
        self.assertEqual(bot.stop_distance, 0)

    def test_extreme_crash_cannot_force_a_market_sale(self):
        bot = self.bot()
        with patch.object(bot, '_hard_stop', side_effect=AssertionError('Liquidation forbidden')):
            bot._segment(D(100), D(1), START, START + pd.Timedelta(minutes=5))
        self.assertEqual(bot.stop_count, 0)
        self.assertIsNone(bot.stop)
        self.assertGreater(bot.held, 0)
        self.assertTrue(all(f['side'] != 'SELL' or f['kind'] == 'LIMIT' for f in bot.fills))
        self.assertEqual(bot.mode, 'RUNNING')
        self.assertGreater(bot.max_dd, D('.4'))

    def test_pause_cancels_only_buys_without_changing_inventory_or_cost(self):
        bot = self.bot()
        sells = {key: o for key, o in bot.orders.items() if o.side == 'SELL'}
        held, cash = bot.held, bot.cash
        cost = sum(lot.cost for lot in bot.lots.values())
        bot._point(D(98), START)
        self.assertTrue(bot.buy_paused)
        self.assertEqual(bot.orders, sells)
        self.assertEqual((bot.held, bot.cash), (held, cash))
        self.assertEqual(sum(lot.cost for lot in bot.lots.values()), cost)
        self.assertEqual(bot.sma_canceled_buys, 4)
        bot._point(D(97), START + pd.Timedelta(minutes=1))
        self.assertEqual(bot.sma_canceled_buys, 4)
        self.assertEqual(bot.buy_pause_episodes, 1)

    def test_equality_stays_paused_and_above_sma_restores_waiting_buy_lanes(self):
        bot = self.bot()
        bot._point(D(98), START)
        bot._point(D(99), START + pd.Timedelta(minutes=1))
        self.assertTrue(bot.buy_paused)
        self.assertTrue(all(o.side == 'SELL' for o in bot.orders.values()))
        bot._point(D(100), START + pd.Timedelta(minutes=2))
        self.assertFalse(bot.buy_paused)
        self.assertEqual(sum(o.side == 'BUY' for o in bot.orders.values()), 4)
        self.assertEqual(bot.buy_resume_episodes, 1)

    def test_sell_limits_execute_while_paused_and_do_not_rebuy_below_sma(self):
        bot = self.bot()
        bot.macro_sma = D(110)
        bot._point(D(98), START)
        held = bot.held
        bot._segment(D(98), D(102), START, START + pd.Timedelta(minutes=5))
        self.assertTrue(bot.buy_paused)
        sells = [f for f in bot.fills if f['side'] == 'SELL']
        self.assertTrue(sells)
        self.assertTrue(all(f['kind'] == 'LIMIT' for f in sells))
        self.assertLess(bot.held, held)
        self.assertTrue(all(o.side == 'SELL' for o in bot.orders.values()))
        self.assertGreater(bot.realized, 0)

    def test_intrabar_sma_cross_cancels_deeper_buys_before_the_low_is_reached(self):
        bot = self.bot()
        buys = sorted([o.price for o in bot.orders.values() if o.side == 'BUY'], reverse=True)
        bot.macro_sma = (buys[0] + buys[1]) / 2
        bot._segment(D(100), D(97), START, START + pd.Timedelta(minutes=5))
        fills = [f for f in bot.fills if f['side'] == 'BUY' and f['kind'] == 'LIMIT']
        self.assertEqual(len(fills), 1)
        self.assertGreater(D(fills[0]['price']), bot.macro_sma)
        self.assertEqual(bot.sma_canceled_buys, 3)
        self.assertTrue(bot.buy_paused)
        bot._segment(D(97), D(100), START + pd.Timedelta(minutes=5), START + pd.Timedelta(minutes=10))
        self.assertFalse(bot.buy_paused)
        self.assertEqual(bot.buy_resume_episodes, 1)

    def test_new_daily_signal_can_pause_without_any_price_change(self):
        bot = self.bot()
        bot.macro_sma = D(101)
        bot._point(D(100), START + pd.Timedelta(days=1))
        self.assertTrue(bot.buy_paused)
        self.assertEqual(bot.sma_canceled_buys, 4)
        self.assertEqual(bot.stop_count, 0)

    def test_gap_fills_existing_orders_before_cancellation_without_forced_liquidation(self):
        bot = self.bot()
        original_held = bot.held
        frame = fine_candles([[90, 90, 89, 90, 1]])
        result = bot.run(frame, START, START + pd.Timedelta(minutes=5), trend_frame=daily_history())
        buys = [f for f in bot.fills if f['kind'] == 'LIMIT' and f['side'] == 'BUY']
        self.assertEqual(len(buys), 4)  # Conservative opening-gap race, not a new BUY deployment.
        self.assertGreater(bot.held, original_held)
        self.assertEqual(result['hard_stop_triggers'], 0)
        self.assertTrue(result['final_buy_paused'])
        self.assertTrue(all(o.side == 'SELL' for o in bot.orders.values()))

    def test_unsold_losses_reduce_ending_equity_even_with_zero_realized_loss(self):
        frame = fine_candles([[101, 101, 70, 70, 1]])
        bot = GridBacktest(Config(stop_mode='none'))
        result = bot.run(frame, START, START + pd.Timedelta(minutes=5), trend_frame=daily_history())
        self.assertEqual(result['total_realized_pnl'], 0)
        self.assertLess(result['unrealized_pnl'], 0)
        self.assertLess(result['ending_equity'], 500)
        self.assertGreater(result['max_drawdown_pct'], 0)
        self.assertEqual(result['hard_stop_triggers'], 0)
        self.assertAlmostEqual(result['ending_equity'] - 500,
                               result['total_realized_pnl'] + result['unrealized_pnl'], places=9)
        self.assertTrue(all(mark['hard_stop'] is None for mark in bot.curve))

    def test_stop_comparison_modes_keep_previous_entry_only_sma_behavior(self):
        bot = GridBacktest(Config(stop_mode='fixed'))
        bot.macro_sma = D(99)
        bot.start_grid(START, D(100), D(1))
        buys = {key: o for key, o in bot.orders.items() if o.side == 'BUY'}
        bot.macro_sma = D(110)
        bot._point(D(98), START)
        self.assertFalse(bot.buy_paused)
        self.assertEqual({key: o for key, o in bot.orders.items() if o.side == 'BUY'}, buys)
        self.assertIsNotNone(bot.stop)


if __name__ == '__main__':
    unittest.main()
