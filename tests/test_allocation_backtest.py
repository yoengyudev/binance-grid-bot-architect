"""Differential cap policy and simulator lifecycle checks, with no exchange I/O."""
from contextlib import redirect_stdout
from decimal import Decimal as D
import io
import json
from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from allocation import require_total_cap
from backtest_grid import (AllocationCapError, Config, GridBacktest, Lane, Lot, Order,
                           atr_bounds, floor_step, main, round_tick)
from engine import GridBot, configured_grid_levels
from models import GridConfig, GridSizingError, TradingHalt
from tests.test_backtest_grid import START, fine_candles


class AllocationBacktestTests(unittest.TestCase):
    def bot(self, **kwargs):
        bot = GridBacktest(Config(levels=5, trend_filter=False, **kwargs))
        bot.start_grid(START, D(80000), D(2000))
        return bot

    def test_default_and_explicit_legacy_are_independent_of_macro_policy(self):
        self.assertEqual(Config().allocation_policy, 'total_cost_cap')
        self.assertEqual(Config(buy_trend_policy='legacy').allocation_policy, 'total_cost_cap')
        with self.assertRaises(ValueError):
            Config(allocation_policy='guess')

    def test_validator_matches_live_cap_at_tolerance_and_invalid_boundaries(self):
        bot = GridBacktest(Config())
        for cost, buys in [(D(0), D(500)), (D(400), D(100)), (D(400), D('100.0000009')),
                           (D(400), D('100.000001')), (D(400), D('100.0000011')),
                           (D(400), D(248)), (D(-1), D(10)), (D('NaN'), D(0)),
                           (D(1), D('Infinity'))]:
            with self.subTest(cost=cost, buys=buys):
                live_ok = sim_ok = True
                try:
                    require_total_cap(SimpleNamespace(investment_quote=D(500)), cost, buys)
                except (GridSizingError, TradingHalt):
                    live_ok = False
                try:
                    bot._require_total_cap(cost, buys)
                except ValueError:
                    sim_ok = False
                self.assertEqual(sim_ok, live_ok)

    def test_seed_and_reanchor_proposals_match_actual_production_planner(self):
        for levels in (5, 8):
            sim = GridBacktest(Config(levels=levels, trend_filter=False))
            center = D(80000)
            lower, upper, stop = atr_bounds(center, D(2000), sim.c)
            config = GridConfig('BTC/USDT', D(500), lower, upper, D('2.5'), D(50), stop, 10,
                                buy_grid_levels=(levels+1)//2, sell_grid_levels=levels//2)
            # Invoke production's actual planning code with pure precision dependencies.
            live = SimpleNamespace(config=config,
                _price=lambda value: round_tick(value, D('.01')),
                _amount=lambda value: floor_step(value, D('.00001')),
                _check_order_size=lambda price, quantity: None)
            live._required_buy_limit_quote = MethodType(GridBot._required_buy_limit_quote, live)
            buys, _ = configured_grid_levels(center, config)
            commitment = live._required_buy_limit_quote(config, buys, stop)
            for cost in (D(0), D(100), D(250), D(400)):
                with self.subTest(levels=levels, cost=cost):
                    live_ok = sim_ok = True
                    try:
                        GridBot._validate_total_grid_allocation(live, config, center, cost)
                    except GridSizingError:
                        live_ok = False
                    try:
                        plan = sim._plan(center, lower, upper, stop, D(500), D('.005'),
                                         position_cost=cost, seed_quote=D(250) if cost == 0 else D(0))
                    except AllocationCapError:
                        sim_ok = False
                    self.assertEqual(sim_ok, live_ok)
                    if sim_ok:
                        self.assertEqual(sum((sim._quantity_for_budget(plan[3], p)*p for p in plan[0]), D(0)), commitment)

    def test_prior_counterexample_is_rejected_before_mutating_the_campaign(self):
        bot = self.bot()
        bot.cash, bot.realized = D(300), D(200)
        next(iter(bot.lots.values())).cost = D(400)
        before = (dict(bot.orders), bot.anchor, bot.stop, bot.cash, bot.held)
        with self.assertRaises(AllocationCapError):
            bot._plan(bot.anchor, bot.lower, bot.upper, bot.stop, D(500), bot.held)
        self.assertEqual(before, (bot.orders, bot.anchor, bot.stop, bot.cash, bot.held))
        bot.mark(START, D(80000))  # State still satisfies the accounting identity.

    def test_rejected_new_buy_cancels_buys_but_preserves_sells_cost_and_stop(self):
        bot = self.bot()
        bot.cash, bot.realized = D(300), D(200)
        next(iter(bot.lots.values())).cost = D(400)
        sells = {key: order for key, order in bot.orders.items() if order.side == 'SELL'}
        before = bot.cash, bot.held, bot.position_cost(), bot.stop
        del bot.orders[1]
        bot._maintain(bot.anchor, START)
        self.assertEqual(bot.mode, 'SIZING_PAUSED')
        self.assertEqual(bot.orders, sells)
        self.assertEqual((bot.cash, bot.held, bot.position_cost(), bot.stop), before)
        self.assertEqual(bot.allocation_cap_buy_pauses, 1)
        bot._point(bot.stop-D('.01'), START)
        self.assertEqual(bot.stop_count, 1)
        self.assertTrue(bot.mode.startswith('HALTED'))

    def test_each_new_buy_includes_other_orders_added_in_the_same_pass(self):
        bot = self.bot()
        bot.cash, bot.realized = D(400), D(200)
        next(iter(bot.lots.values())).cost = D(300)
        bot.orders.clear()
        with patch.object(bot, '_require_total_cap', wraps=bot._require_total_cap) as check:
            bot._maintain(bot.anchor, START)
        commitments = [call.args[1] for call in check.call_args_list]
        self.assertEqual(len(commitments), 3)
        self.assertLess(commitments[0], commitments[1])
        self.assertLess(commitments[1], commitments[2])
        self.assertGreater(commitments[-1]+D(300), D(500))
        self.assertEqual(bot.mode, 'SIZING_PAUSED')
        self.assertEqual(sum(order.side == 'BUY' for order in bot.orders.values()), 0)
        self.assertEqual(sum(order.side == 'SELL' for order in bot.orders.values()), 2)

    def test_blocked_reanchor_defers_without_moving_stop_or_cancelling_old_orders(self):
        bot = self.bot()
        bot.cash, bot.realized = D(300), D(200)
        next(iter(bot.lots.values())).cost = D(400)
        old = dict(bot.orders), bot.anchor, bot.stop, bot.held, bot.cash
        bot.above_since = START
        bot._breakout(D(90000), START+pd.Timedelta(hours=4))
        self.assertEqual((bot.orders, bot.anchor, bot.stop, bot.held, bot.cash), old)
        self.assertEqual(bot.mode, 'RUNNING')
        self.assertEqual(bot.breakout_count, 0)
        self.assertEqual(bot.allocation_cap_reanchor_deferrals, 1)
        self.assertEqual(bot.events[-1]['event'], 'REANCHOR_ALLOCATION_CAP_DEFERRED')

    def test_fundable_under_cap_reanchor_can_still_execute(self):
        bot = self.bot()
        bot.cash, bot.realized = D(600), D(200)
        next(iter(bot.lots.values())).cost = D(100)
        old_stop = bot.stop
        bot.above_since = START
        bot._point(D(90000), START+pd.Timedelta(hours=4))
        self.assertEqual(bot.breakout_count, 1)
        self.assertGreaterEqual(bot.stop, old_stop)
        self.assertEqual(bot.position_cost(), D(100))
        self.assertLessEqual(bot.position_cost()+bot.buy_commitment(), D(500))

    def test_future_quote_fees_not_reserved_but_accepted_fill_cost_is_not_discarded(self):
        bot = GridBacktest(Config(levels=5, trend_filter=False, buy_fee_asset='quote'))
        bot.cash, bot.realized = D('200.2'), D('.2')
        bot.lots = {1: Lot(D(3), D(300))}
        bot.next_lot = 2
        bot.lanes[1] = Lane(1, D(100), D(101), D(200))
        bot.orders[1] = Order(1, 'BUY', D(100), D(2), None)
        bot._require_total_cap(bot.position_cost(), bot.buy_commitment())
        bot._fill(bot.orders[1], START)
        self.assertEqual(bot.position_cost(), D('500.2'))
        self.assertEqual(bot.held, D(5))
        self.assertEqual(bot.fills[-1]['side'], 'BUY')
        bot.mark(START, D(100))
        with self.assertRaises(AllocationCapError):
            bot._require_total_cap(bot.position_cost(), D(0))

    def test_outside_reserves_excluded_and_cash_funding_remains_independent(self):
        bot = self.bot()
        bot.reserves.append(Lot(D(1), D(80000)))
        bot._require_total_cap(bot.position_cost(), bot.buy_commitment())
        bot.cash = D(0)
        del bot.orders[1]
        before = dict(bot.orders)
        bot._maintain(bot.anchor, START)
        self.assertEqual(bot.mode, 'RUNNING')
        self.assertEqual(bot.orders, before)
        self.assertEqual(bot.allocation_cap_buy_pauses, 0)

    def test_legacy_cash_policy_preserves_the_old_counterexample(self):
        bot = self.bot(allocation_policy='legacy_cash')
        bot.cash, bot.realized = D(300), D(200)
        next(iter(bot.lots.values())).cost = D(400)
        bot._plan(bot.anchor, bot.lower, bot.upper, bot.stop, D(500), bot.held)
        del bot.orders[1]
        bot._maintain(bot.anchor, START)
        self.assertEqual(bot.mode, 'RUNNING')
        self.assertGreater(bot.position_cost()+bot.buy_commitment(), D(500))

    def test_explicit_research_restart_caps_remaining_cash_and_excludes_old_dust(self):
        bot = self.bot(restart_after_stop=True)
        bot._hard_stop(bot.stop-D(1), START)
        remaining_cash = bot.cash
        bot._restart(START+pd.Timedelta(hours=8), D(80000), D(2000))
        self.assertEqual(bot.mode, 'RUNNING')
        self.assertEqual(bot.allocation, remaining_cash)
        self.assertEqual(sum(fill['kind'] == 'SEED' for fill in bot.fills), 2)
        self.assertLessEqual(bot.position_cost()+bot.buy_commitment(), remaining_cash+D('.000001'))
        self.assertTrue(bot.reserves)
        bot.mark(START+pd.Timedelta(hours=8), D(80000))

    def test_cli_records_both_policies_and_cost_metrics_without_network(self):
        for policy in ('total_cost_cap', 'legacy_cash'):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fine_candles([[101,101,101,101,1]]).to_csv(root/'input.csv', index_label='timestamp')
                args = ['--csv', str(root/'input.csv'), '--no-trend-filter', '--levels', '5',
                        '--allocation-policy', policy, '--start', START.isoformat(),
                        '--end', (START+pd.Timedelta(minutes=5)).isoformat(),
                        '--output', str(root/'out')]
                with patch('backtest_grid.fetch_candles', side_effect=AssertionError('Network forbidden')), redirect_stdout(io.StringIO()):
                    self.assertEqual(main(args), 0)
                result = json.loads((root/'out/summary.json').read_text())
                self.assertEqual(result['config']['allocation_policy'], policy)
                for row in result['results']:
                    self.assertEqual(row['allocation_policy'], policy)
                    self.assertIn('ending_position_plus_buy_cost', row)
                    self.assertEqual(row['allocation_cap_buy_pauses'], 0)
                    self.assertEqual(row['allocation_cap_reanchor_deferrals'], 0)


if __name__ == '__main__':
    unittest.main()
