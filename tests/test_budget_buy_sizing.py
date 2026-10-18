"""Research replacement sizing: historical reproduction and protective invariants."""
from contextlib import redirect_stdout
from decimal import Decimal as D
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from allocation import require_total_cap
from backtest_grid import Config, GridBacktest, Lane, Lot, Order, main
from tests.test_backtest_grid import START, fine_candles


class BudgetBuySizingTests(unittest.TestCase):
    def bot(self, **kwargs):
        return GridBacktest(Config(levels=5, trend_filter=False,
                                  replacement_buy_policy='budget_aware', **kwargs))

    def test_policy_is_opt_in_and_requires_the_cap(self):
        self.assertEqual(Config().replacement_buy_policy, 'fixed')
        for kwargs in ({'replacement_buy_policy': 'unknown'},
                       {'replacement_buy_policy': 'budget_aware', 'allocation_policy': 'legacy_cash'}):
            with self.assertRaises(ValueError):
                Config(**kwargs)

    def test_historical_first_pause_fits_with_one_fewer_quantity_increment(self):
        bot = self.bot()
        bot.cash = D('88.1729258481')
        cost = D('417.1818897541321321321321321')
        bot.lots[1] = Lot(D('.01'), cost)
        requested, price = D('.00201'), D('41284.67')
        quantity = bot._budget_buy_quantity(requested, price, D('41614.99'))
        self.assertEqual(quantity, D('.00200'))
        require_total_cap(SimpleNamespace(investment_quote=D(500)), cost, quantity*price)
        self.assertGreater(cost + requested*price, D(500))

    def test_cash_pending_orders_and_existing_cost_all_limit_new_orders(self):
        for fee_asset in ('base', 'quote'):
            for cost in ('0', '250', '490', '500', '501'):
                for cash in ('0', '8', '100', '500'):
                    with self.subTest(fee_asset=fee_asset, cost=cost, cash=cash):
                        bot = self.bot(buy_fee_asset=fee_asset)
                        bot.lots[1] = Lot(D(1), D(cost))
                        bot.cash = D(cash) + D(10)*(D('1.001') if fee_asset == 'quote' else D(1))
                        bot.orders[1] = Order(1, 'BUY', D(100), D('.1'), None)
                        q = bot._budget_buy_quantity(D(2), D(100), D(101))
                        factor = D('1.001') if fee_asset == 'quote' else D(1)
                        self.assertGreaterEqual(q, 0)
                        self.assertLessEqual(q, D(2))
                        self.assertEqual(q % bot.c.amount_step, 0)
                        if q:
                            self.assertLessEqual(q*D(100)*factor, bot.free_quote())
                            self.assertLessEqual(bot.position_cost()+(bot.buy_commitment()+q*D(100))*factor, D(500))
                            require_total_cap(SimpleNamespace(investment_quote=D(500)),
                                              bot.position_cost(), bot.buy_commitment()+q*D(100))

    def test_reverse_sell_minimum_is_checked_after_base_fee_and_rounding(self):
        bot = self.bot(amount_step=D('.01'))
        bot.cash = D(7)
        self.assertTrue(bot._valid_order(D('.07'), D(100)))
        self.assertEqual(bot._budget_buy_quantity(D(1), D(100), D(101)), 0)
        bot.cash = D(8)
        self.assertEqual(bot._budget_buy_quantity(D(1), D(100), D(101)), D('.08'))

    def active(self):
        bot = self.bot()
        bot.start_grid(START, D(80000), D(2000))
        bot.cash, bot.realized = D(300), D(200)
        next(iter(bot.lots.values())).cost = D(400)
        bot.orders.clear()
        return bot

    def test_sequential_buys_share_headroom_and_never_pause_or_cancel_sells(self):
        bot = self.active()
        bot._maintain(bot.anchor, START)
        self.assertEqual(bot.mode, 'RUNNING')
        self.assertEqual(sum(o.side == 'SELL' for o in bot.orders.values()), 2)
        self.assertEqual(sum(o.side == 'BUY' for o in bot.orders.values()), 2)
        self.assertEqual(bot.budget_buy_resizes, 1)
        self.assertEqual(bot.budget_buy_deferrals, 1)
        self.assertEqual(bot.allocation_cap_buy_pauses, 0)
        self.assertLessEqual(bot.position_cost()+bot.buy_commitment(), D(500))
        before = len(bot.events)
        bot._maintain(bot.anchor, START)
        self.assertEqual(len(bot.events), before, 'Repeated blocked polls must not flood the journal.')

    def test_deferred_lane_retries_after_a_sell_releases_cost_and_cash(self):
        bot = self.active()
        bot._maintain(bot.anchor, START)
        sell = next(o for o in bot.orders.values() if o.side == 'SELL')
        bot._fill(sell, START)
        bot._maintain(bot.anchor, START)
        self.assertEqual(bot.mode, 'RUNNING')
        self.assertEqual(bot.budget_deferred_lanes, set())
        self.assertTrue(any(e['event'] == 'BUY_BUDGET_RESUMED' for e in bot.events))
        self.assertLessEqual(bot.position_cost()+bot.buy_commitment(), D(500))

    def test_deferred_buy_does_not_block_a_later_protective_sell(self):
        bot = self.active()
        next(iter(bot.lots.values())).cost = D(500)
        bot.lanes[-1].next_side = 'BUY'
        bot._maintain(bot.anchor-D(1), START)
        self.assertEqual(bot.mode, 'RUNNING')
        self.assertIn(-2, bot.orders)
        self.assertEqual(bot.orders[-2].side, 'SELL')
        self.assertTrue(bot.budget_deferred_lanes)

    def test_macro_pause_cannot_be_overridden_by_budget_room(self):
        bot = self.active()
        bot.c = Config(levels=5, replacement_buy_policy='budget_aware')
        bot._point(bot.anchor, START)
        self.assertTrue(bot.buy_paused)
        self.assertFalse(any(o.side == 'BUY' for o in bot.orders.values()))
        self.assertEqual(sum(o.side == 'SELL' for o in bot.orders.values()), 2)

    def test_hard_stop_still_exits_and_does_not_automatically_restart(self):
        bot = self.active()
        bot._maintain(bot.anchor, START)
        bot._point(bot.stop-D('.01'), START)
        self.assertEqual(bot.stop_count, 1)
        self.assertTrue(bot.mode.startswith('HALTED'))
        self.assertFalse(bot.orders)
        bot._restart(START, D(80000), D(2000))
        self.assertTrue(bot.mode.startswith('HALTED'))

    def test_initial_and_reanchor_planners_keep_their_existing_cap_rule(self):
        bot = self.active()
        with self.assertRaises(ValueError):
            bot._plan(bot.anchor, bot.lower, bot.upper, bot.stop, D(500), bot.held)

    def test_upper_lane_rebuy_quantity_can_shrink_but_never_grow(self):
        bot = self.active()
        bot.orders.clear()
        bot.lanes = {-1: Lane(-1, bot.anchor, bot.upper, None, 'BUY', D('.01'), None)}
        bot._maintain(bot.anchor+D(1), START)
        self.assertEqual(bot.orders[-1].quantity, bot._quantity_for_budget(D(100), bot.anchor))
        self.assertEqual(bot.budget_buy_resizes, 1)

    def test_cli_records_research_policy_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fine_candles([[101,101,101,101,1]]).to_csv(root/'input.csv', index_label='timestamp')
            args = ['--csv', str(root/'input.csv'), '--no-trend-filter', '--levels', '5',
                    '--replacement-buy-policy', 'budget_aware', '--start', START.isoformat(),
                    '--end', (START+pd.Timedelta(minutes=5)).isoformat(),
                    '--output', str(root/'out')]
            with patch('backtest_grid.fetch_candles', side_effect=AssertionError('Network forbidden')), redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            summary = json.loads((root/'out/summary.json').read_text())
            self.assertEqual(summary['config']['replacement_buy_policy'], 'budget_aware')
            self.assertTrue(all(r['replacement_buy_policy']=='budget_aware' for r in summary['results']))


if __name__ == '__main__':
    unittest.main()
