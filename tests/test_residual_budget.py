"""Final residual BUY commits with a durable latch; it never resumes on SELLs."""
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from fastapi import Response
import api
from allocation import pending_buy_commitment, tracked_position_cost
from backtest_grid import Config, GridBacktest, Lot, Lane, Order
from budget_policy import CAP_EXHAUSTED, CAP_STATE_KEY, CAP_ORDER_KEY, residual_quantity
from database import GridDatabase
from models import TradingHalt, UncertainOrderError
from tests.fakes import make_bot, OfflineGridBot
from tests.test_backtest_grid import START


class ResidualLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'grid_testnet.sqlite'
        self.bot,self.exchange = make_bot(self.path)
        self.bot.database.set_state('total_allocation_cap','1')

    def seed(self, cost='917.2'):
        cost = D(cost)
        self.bot._submit_order(0,'BUY',D(100),cost/D(100),order_type='MARKET',quote_cost=cost)

    def test_exact_remaining_is_rounded_down_and_final_order_survives_pause(self):
        self.seed()
        self.bot.run_cycle()
        final_id = self.bot.database.get_state(CAP_ORDER_KEY)
        self.assertIsNotNone(final_id)
        row = self.bot.database.get_order(final_id)
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)
        self.assertLessEqual(D(row['price'])*D(row['amount']),D('82.82'))
        self.assertGreaterEqual(D(row['price'])*D(row['amount']),D(10))
        self.assertLessEqual(tracked_position_cost(self.bot.database)+pending_buy_commitment(self.bot.database),D(1000))
        before = len(self.exchange.orders)
        self.bot.run_cycle()
        self.assertEqual(len(self.exchange.orders),before)
        self.assertEqual(self.exchange.orders[final_id]['status'],'open')

    def test_final_fill_gets_sell_protection_and_later_sell_cannot_reopen_buys(self):
        self.seed()
        self.bot.run_cycle()
        final_id = self.bot.database.get_state(CAP_ORDER_KEY)
        self.exchange.fill(final_id)
        self.bot.run_cycle()
        sell = next(r for r in self.bot.database.fetch_active_grids()
                    if r['side']=='SELL' and r['parent_order_id']==final_id)
        before = sum(o['side']=='buy' for o in self.exchange.orders.values())
        self.exchange.fill(sell['client_order_id'])
        self.bot.run_cycle()
        self.assertEqual(sum(o['side']=='buy' for o in self.exchange.orders.values()),before)
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)

    def test_restart_preserves_latch_and_market_seed_guard(self):
        self.seed()
        self.bot.run_cycle()
        reopened = OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path))
        reopened.prepare(persist=False)
        before = len(self.exchange.orders)
        reopened.run_cycle()
        self.assertIsNone(reopened._submit_order(0,'BUY',D(100),D(1),order_type='MARKET',quote_cost=D(100)))
        self.assertEqual(len(self.exchange.orders),before)

    def test_below_minimum_latches_without_order_and_still_checks_stop(self):
        self.seed('995')
        self.bot._place_buy(1,None)
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)
        self.assertIsNone(self.bot.database.get_state(CAP_ORDER_KEY))
        self.exchange.price = D(69)
        self.bot.run_cycle()
        self.assertEqual(self.exchange.market_sell_calls,1)

    def test_latch_and_intent_rollback_together_before_exchange_io(self):
        self.seed()
        before = len(self.exchange.orders)
        original = self.bot.database.set_state
        def failing(key,*args,**kwargs):
            if key == CAP_ORDER_KEY:
                raise RuntimeError('Simulated latch commit failure')
            return original(key,*args,**kwargs)
        with patch.object(self.bot.database,'set_state',side_effect=failing):
            with self.assertRaisesRegex(RuntimeError,'latch commit'):
                self.bot._place_buy(1,None)
        self.assertEqual(len(self.exchange.orders),before)
        self.assertIsNone(self.bot.database.get_state(CAP_STATE_KEY))
        self.assertEqual(len(self.bot.database.fetch_all_orders()),1)

    def test_uncertain_write_keeps_exact_intent_and_latch_no_second_buy(self):
        self.seed()
        self.exchange.fail_create = True
        with self.assertRaises(UncertainOrderError):
            self.bot._place_buy(1,None)
        final_id = self.bot.database.get_state(CAP_ORDER_KEY)
        self.assertIsNotNone(self.bot.database.get_order(final_id))
        self.exchange.fail_create = False
        before = len(self.exchange.orders)
        self.assertIsNone(self.bot._submit_order(2,'BUY',D(90),D('.1')))
        self.assertEqual(len(self.exchange.orders),before)

    def test_definite_post_only_rejection_is_fail_closed_until_review(self):
        self.seed()
        self.exchange.reject_post_only_once = True
        self.bot._place_buy(1,None)
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)
        before = len(self.exchange.orders)
        self.bot._place_buy(1,None)
        self.assertEqual(len(self.exchange.orders),before)

    def test_unknown_cost_fails_closed_and_cap_validator_stays_strict(self):
        self.seed()
        with patch('allocation._portfolio_wallet',return_value={'btc_held':None}):
            with self.assertRaises(TradingHalt):
                self.bot._place_buy(1,None)
        with self.assertRaises(ValueError):
            residual_quantity(D(1),D(100),D(5),lambda value:D(1))

    def test_api_exposes_latch_without_hiding_other_safety_states(self):
        self.bot._exhaust_cap(None,'Review new grid')
        with patch.object(api.app.state,'grid_bot',self.bot),patch.object(api.app.state,'monitor_only',False):
            payload = api._bot_status_payload(Response())
        self.assertEqual(payload['allocation_state'],CAP_EXHAUSTED)
        self.assertEqual(payload['pause_mode'],CAP_EXHAUSTED)
        self.assertEqual(payload['trading_state'],CAP_EXHAUSTED)
        self.bot.database.set_state('safety_mode','PAUSED_MANUAL')
        with patch.object(api.app.state,'grid_bot',self.bot),patch.object(api.app.state,'monitor_only',False):
            payload = api._bot_status_payload(Response())
        self.assertEqual(payload['pause_mode'],'PAUSED_MANUAL')
        self.assertEqual(payload['allocation_state'],CAP_EXHAUSTED)

    def test_stop_start_and_macro_recovery_cannot_release_latch(self):
        self.seed()
        self.bot.run_cycle()
        self.bot.stop_engine()
        with self.assertRaisesRegex(TradingHalt,'CAP_EXHAUSTED'):
            self.bot.start_engine(auto_cover_inventory=True)
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)
        before = len(self.exchange.orders)
        self.bot._macro_trend_provider = lambda: True
        self.bot._place_buy(1,None)
        self.assertEqual(len(self.exchange.orders),before)

    def test_manual_reanchor_clears_latch_only_after_a_valid_grid_installs(self):
        self.seed()
        self.bot.run_cycle()
        self.bot.request_manual_recenter('100','20','30','2500',4)
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)
        self.assertTrue(self.bot.reset_grid())
        self.assertIsNone(self.bot.database.get_state(CAP_STATE_KEY))

    def test_macro_pause_cancels_final_buy_without_automatic_rearming(self):
        self.seed()
        self.bot.run_cycle()
        final_id = self.bot.database.get_state(CAP_ORDER_KEY)
        self.bot._macro_trend_provider = lambda: False
        self.bot.run_cycle()
        self.assertEqual(self.exchange.orders[final_id]['status'],'canceled')
        self.bot._macro_trend_provider = lambda: True
        before = len(self.exchange.orders)
        self.bot.run_cycle()
        self.assertEqual(len(self.exchange.orders),before)
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)

    def test_partial_final_fill_is_sold_without_rearming_the_unfilled_budget(self):
        self.seed()
        self.bot.run_cycle()
        final_id = self.bot.database.get_state(CAP_ORDER_KEY)
        price = D(self.exchange.orders[final_id]['price'])
        self.exchange.orders[final_id].update(filled='0.3',cost=str(price*D('.3')))
        self.exchange.base_free += D('.3')
        self.bot.run_cycle()
        self.assertEqual(self.exchange.orders[final_id]['status'],'canceled')
        self.assertTrue(any(r['side']=='SELL' and r['parent_order_id']==final_id
                            for r in self.bot.database.fetch_active_grids()))
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)


class ResidualSimulatorTests(unittest.TestCase):
    def bot(self):
        bot = GridBacktest(Config(levels=5,trend_filter=False,replacement_buy_policy='residual'))
        bot.start_grid(START,D(80000),D(2000))
        bot.cash,bot.realized = D(300),D(200)
        next(iter(bot.lots.values())).cost = D(400)
        bot.orders.clear()
        return bot

    def test_one_final_buy_then_sell_releases_cannot_restart_it(self):
        bot = self.bot()
        bot._maintain(bot.anchor,START)
        self.assertEqual(bot.mode,CAP_EXHAUSTED)
        self.assertEqual(bot.residual_buy_count,1)
        self.assertEqual(sum(o.side=='BUY' for o in bot.orders.values()),2)
        self.assertLessEqual(bot.position_cost()+bot.buy_commitment(),D(500))
        for order in list(bot.orders.values()):
            if order.side=='BUY':
                bot._fill(order,START)
        bot._maintain(bot.anchor,START)
        self.assertTrue(all(o.side=='SELL' for o in bot.orders.values()))
        for order in list(bot.orders.values()):
            bot._fill(order,START)
        bot._point(bot.anchor,START)
        self.assertFalse(bot.orders)
        self.assertEqual(bot.mode,CAP_EXHAUSTED)
        self.assertEqual(bot.residual_buy_count,1)

    def test_cap_pause_keeps_hard_stop_and_cannot_auto_reanchor(self):
        bot = self.bot()
        bot._maintain(bot.anchor,START)
        bot._point(D(90000),START)
        bot._point(D(90000),START+pd.Timedelta(hours=5))
        self.assertEqual(bot.breakout_count,0)
        bot._point(bot.stop-D('.01'),START)
        self.assertEqual(bot.stop_count,1)
        with self.assertRaises(ValueError):
            Config(replacement_buy_policy='residual',restart_after_stop=True)

    def test_historical_rounded_quantity_matches_pure_execution_policy(self):
        expected = residual_quantity(D('.00201'),D('41284.67'),D('82.8181102458678678678678679'),
                                     lambda amount:(amount//D('.00001'))*D('.00001'))
        self.assertEqual(expected,D('.00200'))


if __name__=='__main__':
    unittest.main()
