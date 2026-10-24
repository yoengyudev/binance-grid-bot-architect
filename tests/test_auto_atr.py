"""Closed-hour ATR and actual-engine BUY adaptation with offline SQLite fixtures."""
from dataclasses import replace
from decimal import Decimal as D, ROUND_DOWN
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import auto_atr as policy
import engine
from database import GridDatabase
from budget_policy import CAP_STATE_KEY, CAP_EXHAUSTED
from models import TradingHalt
from settings import SAFETY_MODE_KEY, LIQUIDATED, PAUSED_MANUAL, ENGINE_STATUS_KEY
from tests.fakes import make_bot, OfflineGridBot, FakeSpotExchange
from tests.test_campaign_risk import make_risk_bot

END = 20000 * policy.HOUR_MS


def candles():
    return [[END - i * policy.HOUR_MS, 100, 101, 99, 100, 1] for i in range(15, 0, -1)]


def monitor_for(source):
    return policy.ATRMonitor(lambda: source['snapshot'], clock=lambda: source['clock'],
        wall_clock=lambda: source['hour'] / 1000 + 10, launch=lambda job: job())


class AutoATRPolicyTests(unittest.TestCase):
    def test_flag_is_default_off_and_strict(self):
        with patch.dict(os.environ, {}, clear=True):self.assertFalse(policy.configured_enabled())
        for flag in ('true', '1'):
            with patch.dict(os.environ, {'GRID_AUTO_ATR_ENABLED': flag}):self.assertTrue(policy.configured_enabled())
        with patch.dict(os.environ, {'GRID_AUTO_ATR_ENABLED': 'yes'}):
            with self.assertRaises(ValueError):policy.configured_enabled()

    def test_closed_hour_atr_ignores_forming_price_and_requires_consecutive_history(self):
        expected=policy.calculate_atr(candles(), END)
        self.assertEqual(expected.atr, D(2))
        self.assertEqual(policy.calculate_atr(candles()+[[END,100,9000,1,5000,1]],END),expected)
        with self.assertRaises(ValueError):policy.calculate_atr(candles()[1:],END)
        with self.assertRaises(ValueError):policy.calculate_atr(candles(),END+policy.HOUR_MS)

    def test_geometric_buy_net_and_paired_exits_defend_fees_after_rounding(self):
        round_price=lambda value:value.quantize(D('.01'),rounding=ROUND_DOWN)
        plan=policy.make_plan(D(100),D('.1'),[1,2,3,-1,-2],D(70),D('.01'),round_price)
        self.assertEqual(D(plan['gap']),D('.004'))
        prices=[D(lane['buy']) for lane in plan['lanes'].values()]
        self.assertEqual(prices,sorted(prices,reverse=True))
        for lane in plan['lanes'].values():
            self.assertGreaterEqual(D(lane['sell'])/D(lane['buy'])-1,D('.004'))
        with self.assertRaises(ValueError):
            policy.make_plan(D(100),D(50),[1,2,3,-1,-2],D(70),D('.01'),round_price)

    def test_cooldown_and_meaningful_change_are_both_required(self):
        previous=dict(center='100',atr='2',gap='.01',closed_at_ms=END)
        self.assertFalse(policy.needs_update(previous,policy.ATRSnapshot(END+policy.HOUR_MS,D(5),D(100)),D(120)))
        current=policy.ATRSnapshot(END+4*policy.HOUR_MS,D(2),D(100))
        self.assertFalse(policy.needs_update(previous,current,D('100.5')))
        self.assertTrue(policy.needs_update(previous,current,D(101)))
        self.assertTrue(policy.needs_update(previous,replace(current,atr=D('2.5')),D(100)))

    def test_monitor_stales_at_hour_rollover_and_failed_refresh_invalidates_cache(self):
        source=dict(snapshot=policy.ATRSnapshot(END,D(2),D(100)),clock=0,hour=END)
        monitor=monitor_for(source)
        self.assertIsNone(monitor.read())
        self.assertIsNotNone(monitor.refresh())
        source.update(clock=60,snapshot=None)
        self.assertIsNone(monitor.refresh())
        self.assertFalse(monitor.status()['available'])
        source.update(clock=90,snapshot=policy.ATRSnapshot(END,D(2),D(100)))
        self.assertIsNotNone(monitor.refresh())
        source['hour']+=policy.HOUR_MS
        self.assertIsNone(monitor.read())

    def test_worker_single_flight_status_is_read_only_and_worker_failure_blocks(self):
        jobs=[]
        fetch=Mock(return_value=policy.ATRSnapshot(END,D(2),D(100)))
        monitor=policy.ATRMonitor(fetch,clock=lambda:0,wall_clock=lambda:END/1000,launch=jobs.append)
        self.assertIsNone(monitor.refresh());self.assertIsNone(monitor.refresh())
        self.assertEqual(len(jobs),1);fetch.assert_not_called()
        self.assertFalse(monitor.status()['available']);fetch.assert_not_called()
        jobs.pop()();self.assertTrue(monitor.status()['available'])
        broken=policy.ATRMonitor(fetch,launch=Mock(side_effect=RuntimeError()))
        self.assertIsNone(broken.refresh())
        self.assertEqual(broken.status()['error'],'WorkerStartFailed')


class AutoATREngineTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'grid_testnet.sqlite'
        alert=patch.object(engine,'schedule_telegram_alert',return_value=False)
        alert.start();self.addCleanup(alert.stop)
        self.bot,self.exchange=make_bot(self.path)
        self.bot.run_cycle();self.bot.run_cycle()
        self.regime=Mock()
        self.regime.buys_allowed.return_value=True
        self.regime.status.return_value=dict(enabled=True,available=True,buy_allowed=True,reason_code='RANGE')
        self.bot._regime_monitor=self.regime
        self.source=dict(snapshot=policy.ATRSnapshot(END,D(2),D(100)),clock=0,hour=END)
        self.monitor=monitor_for(self.source)
        self.bot._auto_atr_monitor=self.monitor

    def apply(self):
        self.bot.run_cycle()
        self.assertIsNotNone(self.bot._auto_atr_state()['pending'])
        self.bot.run_cycle()
        self.assertIsNone(self.bot._auto_atr_state()['pending'])
        self.assertIsNotNone(self.bot._auto_atr_state()['active'])

    def sells(self):
        return {r['order_id']:(r['price'],r['amount']) for r in self.bot.database.fetch_active_grids() if r['side']=='SELL'}

    def test_adapts_buy_net_without_touching_sells_btc_stop_or_settings(self):
        sells=self.sells();config=self.bot.config
        before=self.exchange.base_free
        self.apply()
        self.assertEqual(self.sells(),sells)
        self.assertEqual(self.exchange.base_free,before)
        self.assertEqual(self.bot.config,config)
        plan=self.bot._auto_atr_state()['active']
        buys=[r for r in self.bot.database.fetch_active_grids() if r['side']=='BUY']
        self.assertEqual(len(buys),len(self.bot.levels))
        for buy in buys:
            lane=plan['lanes'][str(buy['level'])]
            self.assertEqual(D(buy['price']),D(lane['buy']))
            self.assertEqual(self.bot.database.get_state(policy.EXIT_PREFIX+buy['order_id']),lane['sell'])
        self.assertEqual(self.exchange.market_sell_calls,0)

    def test_one_evaluation_per_hour_and_no_repeated_cancellation_in_cooldown(self):
        self.apply();calls=len(self.exchange.cancel_calls)
        for _ in range(3):self.bot.run_cycle()
        self.assertEqual(len(self.exchange.cancel_calls),calls)
        self.source.update(hour=END+policy.HOUR_MS,clock=60,
                           snapshot=policy.ATRSnapshot(END+policy.HOUR_MS,D(3),D(100)))
        self.bot.run_cycle()
        self.assertEqual(len(self.exchange.cancel_calls),calls)
        self.assertIsNone(self.bot._auto_atr_state()['pending'])

    def test_restart_preserves_active_plan_and_cannot_disable_in_place(self):
        self.apply();state=self.bot.database.get_state(policy.STATE_KEY)
        restored=OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path),
                                regime_monitor=self.regime,atr_monitor=self.monitor)
        restored.prepare(persist=False)
        self.assertEqual(restored.database.get_state(policy.STATE_KEY),state)
        self.assertEqual(restored._auto_atr_state()['active'],self.bot._auto_atr_state()['active'])
        with self.assertRaisesRegex(TradingHalt,'disable'):
            OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path))

    def test_pending_cancellation_resumes_after_restart_without_duplicate_buys(self):
        self.bot.run_cycle()
        self.exchange.cancel_timeout='before'
        with patch.object(engine.time,'sleep',return_value=None):self.bot.run_cycle()
        restored=OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path),
                                regime_monitor=self.regime,atr_monitor=self.monitor)
        restored.prepare(persist=False);restored.run_cycle();restored.run_cycle()
        buys=[r for r in restored.database.fetch_active_grids() if r['side']=='BUY']
        self.assertEqual(len({r['level'] for r in buys}),len(buys))
        self.assertIsNotNone(restored._auto_atr_state()['active'])

    def test_cancel_timeout_after_accept_waits_for_confirmation_before_new_buys(self):
        self.bot.run_cycle()
        before=set(self.exchange.orders)
        self.exchange.cancel_timeout='after'
        with patch.object(engine.time,'sleep',return_value=None):self.bot.run_cycle()
        self.assertIsNotNone(self.bot._auto_atr_state()['pending'])
        self.assertEqual(set(self.exchange.orders),before)
        self.bot.run_cycle()
        self.assertIsNotNone(self.bot._auto_atr_state()['active'])
        active=[r for r in self.bot.database.fetch_active_grids() if r['side']=='BUY']
        self.assertEqual(len(active),len({r['level'] for r in active}))

    def test_partial_fill_on_canceled_buy_keeps_old_exit_and_exact_quantity(self):
        old=next(r for r in self.bot.database.fetch_active_grids() if r['side']=='BUY')
        target=self.bot._paired_sell_price(old)
        self.bot.run_cycle()
        order=self.exchange.orders[old['order_id']]
        partial=D('0.2')
        order.update(filled=str(partial),cost=str(partial*D(old['price'])))
        self.exchange.base_free+=partial
        self.bot.run_cycle();self.bot.run_cycle()
        children=[r for r in self.bot.database.fetch_active_grids() if r['parent_order_id']==old['order_id']]
        self.assertEqual(len(children),1)
        self.assertEqual(D(children[0]['price']),target)
        self.assertEqual(D(children[0]['amount']),D('.199'))  # Existing fee reserve, rounded down.
        self.assertLessEqual(D(children[0]['amount']),partial)

    def test_market_drift_withdraws_candidate_and_does_not_move_sell_targets(self):
        self.bot.run_cycle();sells=self.sells()
        self.exchange.price=D(102)
        self.bot.run_cycle()
        state=self.bot._auto_atr_state()
        self.assertIsNone(state['active']);self.assertIsNone(state['pending'])
        self.assertEqual(self.sells(),sells)
        self.assertEqual(self.bot.database.get_state('auto_atr_decision'),'CANDIDATE_DRIFTED')

    def test_cap_exhaustion_is_never_cleared_by_automatic_adaptation(self):
        self.bot.database.set_state(CAP_STATE_KEY,CAP_EXHAUSTED)
        self.bot.run_cycle()
        self.assertIsNone(self.bot._auto_atr_state()['pending'])
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)

    def test_damaged_plan_fails_closed_but_does_not_block_price_stop(self):
        self.apply()
        state=self.bot._auto_atr_state();state['active']['lanes']['1']['sell']='NaN'
        self.bot.database.set_state(policy.STATE_KEY,json.dumps(state))
        self.assertFalse(self.bot.auto_atr_status()['available'])
        with self.assertRaises(TradingHalt):self.bot._place_buy(1,None)
        self.exchange.price=D(69);self.bot.run_cycle()
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY),LIQUIDATED)

    def test_missing_frozen_exit_is_not_replaced_with_current_geometry(self):
        self.apply()
        row=next(r for r in self.bot.database.fetch_active_grids() if r['side']=='BUY')
        self.bot.database.clear_state(policy.EXIT_PREFIX+row['order_id'])
        with self.assertRaisesRegex(TradingHalt,'missing its frozen'):
            self.bot._paired_sell_price(row)

    def test_explicit_manual_reset_takes_priority_over_automatic_candidate(self):
        self.bot.run_cycle()
        candidate=dict(phase='canceling',source='manual_recenter',lower='80',upper='120',spacing='10')
        self.bot.database.set_state('grid_reset',json.dumps(candidate))
        self.bot.grid_needs_reset=True
        self.bot.run_cycle()
        self.assertIsNone(self.bot._auto_atr_state()['active'])
        self.assertEqual(json.loads(self.bot.database.get_state('grid_reset')),candidate)

    def test_late_fill_during_cancel_gets_original_exit_not_new_plan_target(self):
        old=next(r for r in self.bot.database.fetch_active_grids() if r['side']=='BUY')
        target=self.bot._paired_sell_price(old)
        self.bot.run_cycle()  # Persist original targets and pending new geometry.
        self.exchange.fill(old['client_order_id'])
        self.bot.run_cycle();self.bot.run_cycle()
        sell=next(r for r in self.bot.database.fetch_active_grids() if r['parent_order_id']==old['order_id'])
        self.assertEqual(D(sell['price']),target)
        self.assertEqual(self.exchange.market_sell_calls,0)

    def test_new_buy_fill_gets_its_frozen_exit_even_after_another_adaptation(self):
        self.apply()
        buy=next(r for r in self.bot.database.fetch_active_grids() if r['side']=='BUY')
        frozen=D(self.bot.database.get_state(policy.EXIT_PREFIX+buy['order_id']))
        self.exchange.fill(buy['client_order_id'])
        self.bot.run_cycle()
        sell=next(r for r in self.bot.database.fetch_active_grids() if r['parent_order_id']==buy['order_id'])
        self.assertEqual(D(sell['price']),frozen)
        self.source.update(hour=END+4*policy.HOUR_MS,clock=60,
                           snapshot=policy.ATRSnapshot(END+4*policy.HOUR_MS,D(3),D(100)))
        self.apply()
        self.assertEqual(D(self.bot.database.get_order(sell['order_id'])['price']),frozen)

    def test_stale_data_blocks_direct_buys_cancels_entries_and_keeps_sells(self):
        self.apply();sells=self.sells()
        self.source.update(clock=60,snapshot=None)
        self.bot.run_cycle()
        self.assertEqual(self.sells(),sells)
        self.assertFalse(any(r['side']=='BUY' for r in self.bot.database.fetch_active_grids()))
        self.assertIsNone(self.bot._submit_order(1,'BUY',D(90),D(1)))
        self.assertEqual(self.exchange.market_sell_calls,0)

    def test_pauses_bad_regime_cap_latch_and_manual_reset_do_not_get_overridden(self):
        self.regime.buys_allowed.return_value=False
        self.regime.status.return_value['reason_code']='DOWNTREND'
        self.bot.run_cycle();self.assertIsNone(self.bot._auto_atr_state()['pending'])
        self.regime.buys_allowed.return_value=True
        self.bot.database.set_state(SAFETY_MODE_KEY,PAUSED_MANUAL);self.bot.is_paused=True
        self.bot.run_cycle();self.assertIsNone(self.bot._auto_atr_state()['pending'])

    def test_unfundable_or_stop_conflicting_plan_leaves_existing_buy_ids_alone(self):
        before={r['order_id'] for r in self.bot.database.fetch_active_grids()}
        self.source['snapshot']=policy.ATRSnapshot(END,D(50),D(100))
        self.bot.run_cycle()
        self.assertEqual({r['order_id'] for r in self.bot.database.fetch_active_grids()},before)
        self.assertEqual(self.bot.database.get_state('auto_atr_decision'),'UNFUNDABLE_OR_STOP_CONFLICT')

    def test_price_hard_stop_precedes_atr_worker_even_if_worker_is_broken(self):
        self.exchange.price=D(69)
        self.monitor.refresh=Mock(side_effect=AssertionError('Stop must run first'))
        self.bot.run_cycle()
        self.monitor.refresh.assert_not_called()
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY),LIQUIDATED)
        self.assertEqual(self.exchange.base_free,D(1))

    def test_constructor_requires_range_gate_and_rejects_mainnet(self):
        other=self.path.parent/'other';other.mkdir()
        with self.assertRaisesRegex(ValueError,'requires'):
            OfflineGridBot(self.bot.config,self.exchange,GridDatabase(other/'grid_testnet.sqlite'),atr_monitor=self.monitor)
        with patch.object(engine,'TRADING_SETTINGS',SimpleNamespace(sandbox=False)):
            with self.assertRaisesRegex(ValueError,'Testnet'):
                OfflineGridBot(self.bot.config,self.exchange,self.bot.database,
                               regime_monitor=self.regime,atr_monitor=self.monitor)

    def test_api_projection_does_not_fetch_or_change_orders(self):
        self.apply()
        self.monitor.refresh=Mock(side_effect=AssertionError('No refresh from status'))
        before=self.bot.database.fetch_all_orders()
        self.assertTrue(self.bot.auto_atr_status()['available'])
        self.assertEqual(self.bot.database.fetch_all_orders(),before)

    def test_dollar_exit_and_inventory_ceiling_continue_to_apply(self):
        other=self.path.parent/'risk';other.mkdir()
        bot,exchange=make_risk_bot(other/'grid_testnet.sqlite')
        bot._inventory_sizing_enabled=True
        bot._regime_monitor=self.regime;bot._auto_atr_monitor=self.monitor
        for _ in range(4):bot.run_cycle()
        self.assertLessEqual(bot._campaign_risk_view()['modeled_risk'],D(20))
        budget=bot._inventory_budget()
        self.assertLessEqual(budget['position_cost']+budget['pending_cost'],budget['ceiling'])
        buy=next(r for r in bot.database.fetch_active_grids() if r['side']=='BUY')
        exchange.fill(buy['client_order_id']);exchange.price=D(74)
        bot.run_cycle()
        self.assertEqual(bot.database.get_state('halt_reason'),'campaign_risk_limit')
        self.assertEqual(exchange.base_free,D(1))


if __name__=='__main__':unittest.main()
