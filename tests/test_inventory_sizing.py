"""Inventory taper math and actual-engine order/accounting regressions."""
from dataclasses import replace
from decimal import Decimal as D, ROUND_DOWN
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import engine
from allocation import tracked_position_cost, pending_buy_commitment
from budget_policy import CAP_STATE_KEY, CAP_EXHAUSTED
from database import GridDatabase
from inventory_sizing import configured_enabled, inventory_budget, size_buy
from models import TradingHalt
from settings import SAFETY_MODE_KEY, LIQUIDATED
from tests.fakes import make_bot, OfflineGridBot, FakeSpotExchange


def round_down(value):
    return value.quantize(D('.001'), rounding=ROUND_DOWN)


class InventoryPolicyTests(unittest.TestCase):
    def test_taper_boundaries(self):
        for cost, factor in (('0','1'),('50','1'),('60','.6'),('70','.2'),('75','0'),('90','0')):
            with self.subTest(cost=cost):
                plan=size_buy(D(1),D(10),D(100),D(cost),D(0),round_down)
                self.assertEqual(plan.factor,D(factor))
                self.assertEqual(plan.amount,D(factor))

    def test_pending_orders_reserve_headroom_and_rounding_never_increases_quantity(self):
        plan=size_buy(D(1),D(10),D(100),D(60),D('14.001'),round_down)
        self.assertEqual(plan.amount,D('.099'))
        self.assertLessEqual(plan.amount*10,D('.999'))
        self.assertEqual(size_buy(D(1),D(10),D(100),D(60),D(15),round_down).amount,0)
        with self.assertRaisesRegex(ValueError,'rounding'):
            size_buy(D(1),D(10),D(100),D(60),D(14),lambda value:D(1))

    def test_cost_not_price_appreciation_or_wallet_balance_controls_the_policy(self):
        # No ticker or account-wallet input exists in this policy.
        budget=inventory_budget(D(100),D(60),D(5))
        self.assertEqual((budget['factor'],budget['room']),(D('.6'),D(10)))

    def test_invalid_inputs_fail_closed(self):
        for allocation,held,pending in ((D(0),D(0),D(0)),(D(100),D(-1),D(0)),
                                        (D(100),D(0),D('NaN')),(100,D(0),D(0))):
            with self.assertRaises(ValueError):
                inventory_budget(allocation,held,pending)
        for quantity,price in ((D(0),D(1)),(D(1),D('Infinity'))):
            with self.assertRaises(ValueError):
                size_buy(quantity,price,D(100),D(0),D(0),round_down)

    def test_flag_is_off_by_default_and_strict(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertFalse(configured_enabled())
        for value in ('true','1'):
            with patch.dict(os.environ,{'GRID_INVENTORY_SIZING_ENABLED':value}):
                self.assertTrue(configured_enabled())
        with patch.dict(os.environ,{'GRID_INVENTORY_SIZING_ENABLED':'yes'}):
            with self.assertRaises(ValueError):
                configured_enabled()


class InventoryEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'grid_testnet.sqlite'
        self.bot,self.exchange=make_bot(self.path)
        self.bot._inventory_sizing_enabled=True
        alerts=patch.object(engine,'schedule_telegram_alert',return_value=False)
        alerts.start();self.addCleanup(alerts.stop)

    def seed(self,cost='500'):
        cost=D(cost)
        self.assertIsNotNone(self.bot._submit_order(0,'BUY',D(100),cost/100,
                                                   order_type='MARKET',quote_cost=cost))

    def total(self):
        return tracked_position_cost(self.bot.database)+pending_buy_commitment(self.bot.database)

    def test_flag_constructs_opt_in_and_mainnet_activation_is_rejected(self):
        with patch.dict(os.environ,{'GRID_INVENTORY_SIZING_ENABLED':'true'}):
            opted=OfflineGridBot(self.bot.config,self.exchange,self.bot.database)
            self.assertTrue(opted._inventory_sizing_enabled)
            with patch.object(engine,'TRADING_SETTINGS',SimpleNamespace(sandbox=False)):
                with self.assertRaisesRegex(ValueError,'restricted to Testnet'):
                    OfflineGridBot(self.bot.config,self.exchange,self.bot.database)
            with self.assertRaisesRegex(ValueError,'Initial inventory'):
                OfflineGridBot(replace(self.bot.config,initial_inventory_percent=D(80)),
                               self.exchange,self.bot.database)

    def test_disabled_keeps_original_buy_sizes_without_reading_inventory(self):
        self.bot._inventory_sizing_enabled=False
        self.bot.run_cycle(); self.bot.run_cycle()
        buys=[row for row in self.bot.database.fetch_active_grids() if row['side']=='BUY']
        self.assertEqual(len(buys),len(self.bot.levels))
        with patch.object(self.bot,'_inventory_budget',side_effect=AssertionError('Disabled')):
            self.assertEqual(self.bot.inventory_sizing_status(),{'enabled':False})

    def test_initial_pending_orders_are_capped_sequentially_including_seed(self):
        self.bot.run_cycle(); self.bot.run_cycle()
        self.assertLessEqual(self.total(),D(750))
        buys=[row for row in self.bot.database.fetch_active_grids() if row['side']=='BUY']
        self.assertEqual(len(buys),1)  # Second nominal lane cannot consume a rounding remainder.
        self.assertIsNone(self.bot.database.get_state(CAP_STATE_KEY))
        self.assertIsNone(self.bot.database.get_state(SAFETY_MODE_KEY))
        self.assertTrue(any(row['side']=='SELL' for row in self.bot.database.fetch_active_grids()))

    def test_higher_held_cost_reduces_new_buy_quantity(self):
        amounts=[]
        for index,cost in enumerate(('500','600','700')):
            folder=Path(self.temp.name)/str(index);folder.mkdir()
            bot,exchange=make_bot(folder/'grid_testnet.sqlite')
            bot._inventory_sizing_enabled=True
            bot._submit_order(0,'BUY',D(100),D(cost)/100,order_type='MARKET',quote_cost=D(cost))
            bot._place_buy(1,None)
            buys=[row for row in bot.database.fetch_active_grids() if row['side']=='BUY' and row['level']>0]
            self.assertEqual(len(buys),1)
            amounts.append(D(buys[0]['amount']))
            self.assertLessEqual(tracked_position_cost(bot.database)+pending_buy_commitment(bot.database),D(750))
        self.assertGreater(amounts[0],amounts[1]);self.assertGreater(amounts[1],amounts[2])

    def test_below_minimum_defers_only_lane_and_does_not_latch_or_pause_engine(self):
        self.seed('745')
        for _ in range(3):self.bot._place_buy(1,None)
        self.assertEqual(len(self.exchange.orders),1)
        self.assertIsNone(self.bot.database.get_state(CAP_STATE_KEY))
        self.assertIsNone(self.bot.database.get_state(SAFETY_MODE_KEY))
        decision=json.loads(self.bot.database.get_state('inventory_buy_sizing:1'))
        self.assertEqual(decision['reason'],'BELOW_ORDER_MINIMUM')
        events=[e for e in self.bot.database.fetch_transition_journal() if e['trigger_event']=='INVENTORY_BUY_SIZING']
        self.assertEqual(len(events),1)
        self.bot.run_cycle()
        self.assertTrue(any(row['side']=='SELL' for row in self.bot.database.fetch_active_grids()))

    def test_final_submission_guard_rejects_bypass_and_market_buy_without_budget(self):
        self.seed('700')
        before=len(self.bot.database.fetch_all_orders())
        self.assertIsNone(self.bot._submit_order(1,'BUY',D(90),D(1)))
        self.assertEqual(len(self.bot.database.fetch_all_orders()),before)
        with self.assertRaisesRegex(TradingHalt,'explicit quote budget'):
            self.bot._submit_order(0,'BUY',D(100),D(1),order_type='MARKET')

    def test_unknown_exposure_defers_new_buys_and_status_does_not_fetch_exchange(self):
        self.seed()
        with patch.object(self.bot,'_inventory_budget',side_effect=TradingHalt('Unknown')):
            self.bot._place_buy(1,None)
            self.assertIsNone(self.bot._submit_order(1,'BUY',D(90),D(1)))
            self.assertFalse(self.bot.inventory_sizing_status()['available'])
        with patch.object(self.bot,'_call',side_effect=AssertionError('No exchange read')):
            self.assertEqual(self.bot.inventory_sizing_status()['position_cost'],'500.00')
        self.assertEqual(len(self.exchange.orders),1)

    def test_reserved_wallet_btc_and_ticker_changes_do_not_change_purchase_cost(self):
        self.seed()
        before=self.bot.inventory_sizing_status()
        self.exchange.base_free+=D(100)
        self.exchange.price=D(10000)
        self.assertEqual(self.bot.inventory_sizing_status(),before)
        self.assertEqual(D(before['position_cost']),D(500))

    def test_sell_fill_releases_cost_but_does_not_clear_existing_cap_latch(self):
        self.bot.run_cycle();self.bot.run_cycle()
        sell=next(row for row in self.bot.database.fetch_active_grids() if row['side']=='SELL')
        self.bot.database.set_state(CAP_STATE_KEY,CAP_EXHAUSTED)
        count=sum(o['side']=='buy' for o in self.exchange.orders.values())
        self.exchange.fill(sell['client_order_id']);self.bot.run_cycle()
        self.assertEqual(sum(o['side']=='buy' for o in self.exchange.orders.values()),count)
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)

    def test_normal_sell_release_allows_existing_lane_replacement_within_ceiling(self):
        self.bot.run_cycle();self.bot.run_cycle()
        sell=next(row for row in self.bot.database.fetch_active_grids() if row['side']=='SELL')
        before=tracked_position_cost(self.bot.database)
        self.exchange.fill(sell['client_order_id']);self.bot.run_cycle()
        self.assertLess(tracked_position_cost(self.bot.database),before)
        self.assertLessEqual(self.total(),D(750))
        replacement=next(row for row in self.bot.database.fetch_active_grids()
                         if row['parent_order_id']==sell['order_id'])
        self.assertLessEqual(D(replacement['amount']),D(sell['amount']))

    def test_macro_and_regime_gates_cannot_be_overridden_by_available_budget(self):
        self.bot._macro_trend_provider=lambda:False
        self.bot.run_cycle();self.assertEqual(self.exchange.orders,{})
        self.bot._macro_trend_provider=lambda:True
        monitor=Mock()
        monitor.buys_allowed.return_value=False
        monitor.status.return_value={'reason_code':'DOWNTREND'}
        self.bot._regime_monitor=monitor
        self.bot.run_cycle();self.assertEqual(self.exchange.orders,{})

    def test_hard_stop_has_priority_over_failed_inventory_projection(self):
        self.bot.run_cycle();self.bot.run_cycle()
        with patch.object(self.bot,'_inventory_budget',side_effect=AssertionError('Stop first')):
            self.exchange.price=D(69);self.bot.run_cycle()
        self.assertEqual(self.exchange.market_sell_calls,1)
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY),LIQUIDATED)

    def test_enabling_on_old_grid_cancels_excess_buys_preserving_sells_and_recovers(self):
        self.bot._inventory_sizing_enabled=False
        self.bot.run_cycle();self.bot.run_cycle()
        sells={r['order_id'] for r in self.bot.database.fetch_active_grids() if r['side']=='SELL'}
        old_buys={r['order_id'] for r in self.bot.database.fetch_active_grids() if r['side']=='BUY'}
        self.assertGreater(self.total(),D(750))
        self.bot._inventory_sizing_enabled=True
        self.bot.run_cycle()
        self.assertLessEqual(self.total(),D(750))
        self.assertTrue(all(self.bot.database.get_order(i)['status']=='CANCELED' for i in old_buys))
        self.assertTrue(sells.issubset({r['order_id'] for r in self.bot.database.fetch_active_grids()}))
        with patch.dict(os.environ,{'GRID_INVENTORY_SIZING_ENABLED':'true'}):
            recovered=OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path))
        recovered.prepare(persist=False);recovered.run_cycle()
        count=len(self.exchange.orders);recovered.run_cycle()
        self.assertEqual(len(self.exchange.orders),count)

    def test_fill_wins_inventory_cancel_race_and_gets_one_protective_sell(self):
        self.bot._inventory_sizing_enabled=False
        self.bot.run_cycle();self.bot.run_cycle()
        self.bot._inventory_sizing_enabled=True
        self.exchange.cancel_not_found_once=True
        self.bot.run_cycle();self.bot.run_cycle()
        fills=[r for r in self.bot.database.fetch_all_orders()
               if r['side']=='BUY' and r['level']>0 and r['status']=='FILLED']
        self.assertEqual(len(fills),1)
        exits=[r for r in self.bot.database.fetch_active_grids()
               if r['side']=='SELL' and r['parent_order_id']==fills[0]['order_id']]
        self.assertEqual(len(exits),1)
        self.assertLessEqual(self.total(),D(750))

    def test_failed_excess_cancellation_does_not_assume_budget_was_released(self):
        self.bot._inventory_sizing_enabled=False
        self.bot.run_cycle();self.bot.run_cycle()
        self.bot._inventory_sizing_enabled=True
        before=len(self.exchange.orders)
        self.exchange.cancel_timeout='before'
        self.bot.run_cycle()
        self.assertEqual(len(self.exchange.orders),before)
        self.bot.run_cycle()
        self.assertLessEqual(self.total(),D(750))

    def test_hundred_usdt_exact_five_lane_grid_can_leave_unfundable_lanes_waiting(self):
        folder=Path(self.temp.name)/'small';folder.mkdir()
        exchange=FakeSpotExchange();exchange.price=D(82000)
        exchange.market=lambda symbol:dict(spot=True,active=True,base='BTC',quote='USDT',
            limits={'amount':{'min':.00001},'cost':{'min':7}})
        exchange.amount_to_precision=lambda symbol,value:str(D(value).quantize(D('.00001'),rounding=ROUND_DOWN))
        config=replace(self.bot.config,investment_quote=D(100),lower_price=D(80000),
            upper_price=D(84000),stop_loss_price=D(68000),spacing_percent=D('.4'),
            buy_grid_levels=3,sell_grid_levels=2)
        with patch.dict(os.environ,{'GRID_INVENTORY_SIZING_ENABLED':'true'}):
            bot=OfflineGridBot(config,exchange,GridDatabase(folder/'grid_testnet.sqlite'))
        bot.prepare(persist=True);bot.run_cycle();bot.run_cycle()
        self.assertLessEqual(tracked_position_cost(bot.database)+pending_buy_commitment(bot.database),D(75))
        active=bot.database.fetch_active_grids()
        self.assertEqual(len([r for r in active if r['side']=='SELL']),2)
        self.assertEqual(len([r for r in active if r['side']=='BUY']),2)
        self.assertIsNone(bot.database.get_state(SAFETY_MODE_KEY))
        self.assertIsNone(bot.database.get_state(CAP_STATE_KEY))

    def test_reviewed_reset_completes_with_intentionally_deferred_buy_lanes(self):
        self.bot.request_manual_recenter('100','20','30','1000',8)
        self.assertTrue(self.bot.reset_grid())
        self.assertIsNone(self.bot.database.get_state('grid_reset'))
        active=self.bot.database.fetch_active_grids()
        self.assertEqual(len([row for row in active if row['side']=='SELL']),4)
        self.assertLess(len([row for row in active if row['side']=='BUY']),4)
        self.assertLessEqual(self.total(),D(750))

    def test_cash_preflight_does_not_demand_money_reserved_outside_inventory_ceiling(self):
        self.seed()
        self.exchange.quote_free=D(250)  # Enough for this policy, not nominal 500-USDT BUYs.
        self.bot.run_cycle()
        self.assertLessEqual(self.total(),D(750))
        self.assertTrue(any(row['side']=='BUY' and row['level']>0
                            for row in self.bot.database.fetch_active_grids()))

    def test_unknown_inventory_cannot_certify_or_start_reviewed_reset(self):
        self.bot.request_manual_recenter('100','20','30','1000',8)
        with patch.object(self.bot,'_inventory_budget',side_effect=TradingHalt('Unknown cost')):
            self.assertFalse(self.bot.reset_grid())
        self.assertIsNotNone(self.bot.database.get_state('grid_reset'))
        self.assertEqual(self.exchange.orders,{})


if __name__=='__main__':unittest.main()
