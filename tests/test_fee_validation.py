"""Net-edge arithmetic and real-engine fee gates using only offline fixtures."""
from decimal import Decimal as D, ROUND_DOWN
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import fee_validation as policy
import engine
from database import GridDatabase
from models import GridConfig, GridSizingError, TradingHalt
from settings import SAFETY_MODE_KEY, LIQUIDATED
from tests.fakes import OfflineGridBot, FakeSpotExchange
from tests.test_auto_atr import END, monitor_for
from auto_atr import ATRSnapshot

FEE_ENV = dict(GRID_FEE_VALIDATION_ENABLED='true', GRID_BUY_FEE_PERCENT='0.10',
    GRID_SELL_FEE_PERCENT='0.10', GRID_ENTRY_SLIPPAGE_PERCENT='0.05',
    GRID_EXIT_SLIPPAGE_PERCENT='0.05', GRID_MIN_NET_RETURN_PERCENT='0.05')


def fee_bot(path, *, prepare=True, env=None, **kwargs):
    config=GridConfig('BTC/USDT',D(1000),D(80),D(120),D(10),D(50),D(70),2)
    exchange=kwargs.pop('exchange',None) or FakeSpotExchange()
    with patch.dict(os.environ,dict(FEE_ENV,**(env or {}))):
        bot=OfflineGridBot(config,exchange,GridDatabase(path),**kwargs)
    if prepare:bot.prepare(persist=True)
    return bot,exchange


class FeePolicyTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ,FEE_ENV):self.policy=policy.configured_policy()

    def test_default_off_strict_flag_and_explicit_parameters(self):
        with patch.dict(os.environ,{},clear=True):self.assertIsNone(policy.configured_policy())
        with patch.dict(os.environ,{'GRID_FEE_VALIDATION_ENABLED':'true'},clear=True):
            with self.assertRaises(ValueError):policy.configured_policy()
        for key in policy.INPUTS.values():
            for value in ('NaN','Infinity','-0.01','100','abc'):
                with patch.dict(os.environ,dict(FEE_ENV,**{key:value})):
                    with self.assertRaises(ValueError):policy.configured_policy()
        with patch.dict(os.environ,dict(FEE_ENV,GRID_MIN_NET_RETURN_PERCENT='0')):
            with self.assertRaises(ValueError):policy.configured_policy()
        with patch.dict(os.environ,{'GRID_FEE_VALIDATION_ENABLED':'yes'}):
            with self.assertRaises(ValueError):policy.configured_policy()

    def test_exact_net_return_and_required_gross_include_both_fees_and_buffers(self):
        p=self.policy
        expected=D('100.4')*(1-D('.0005'))*(1-D('.001'))*(1-D('.001'))/(100*(1+D('.0005')))-1
        self.assertEqual(p.net_return(D(100),D('100.4')),expected)
        self.assertGreater(p.net_return(D(100),D('100.4')),p.minimum_net_return)
        self.assertLess(p.net_return(D(100),D('100.3')),p.minimum_net_return)
        floor=p.required_gross_return()
        self.assertAlmostEqual(p.net_return(D(100),D(100)*(1+floor)),p.minimum_net_return)

    def test_rounding_dust_keeps_its_cost_and_is_not_a_realized_loss(self):
        rounded=lambda value:value.quantize(D('.01'),rounding=ROUND_DOWN)
        view=self.policy.project(D(100),D(101),D('.101'),rounded)
        self.assertGreater(view['retained_model_base'],0)
        self.assertGreater(view['net_profit_quote'],0)
        self.assertEqual(view['net_return'],self.policy.net_return(D(100),D(101)))
        self.assertLess(view['allocated_cost'],D('.101')*100)
        with self.assertRaises(ValueError):
            self.policy.project(D(100),D(101),D('.101'),lambda value:D(2))

    def test_known_acquisition_cost_is_not_charged_another_entry_fee_or_slippage(self):
        edge=self.policy.existing_exit(D('100.1'),D(1),D(1),D(101))
        expected=D(101)*(1-self.policy.exit_slippage)*(1-self.policy.sell_fee)/D('100.1')-1
        self.assertEqual(edge,expected)
        self.assertGreater(edge,self.policy.net_return(D('100.1'),D(101)))


class FeeEngineTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'grid_testnet.sqlite'
        alerts=patch.object(engine,'schedule_telegram_alert',return_value=False)
        alerts.start();self.addCleanup(alerts.stop)
        self.bot,self.exchange=fee_bot(self.path)

    def working(self):self.bot.run_cycle();self.bot.run_cycle()

    def sells(self):
        return {r['order_id']:(r['price'],r['amount']) for r in self.bot.database.fetch_active_grids() if r['side']=='SELL'}

    def test_valid_seed_and_buys_store_their_exit_before_http(self):
        self.working()
        seed=self.bot.database.fetch_latest_orders_by_level()[0]
        self.assertIsNotNone(self.bot.database.get_state('fee_validation_seed_exits:'+seed['order_id']))
        for buy in self.bot.database.fetch_active_grids():
            if buy['side']=='BUY':
                self.assertEqual(D(self.bot.database.get_state('fee_validation_exit:'+buy['order_id'])),
                                 self.bot._paired_sell_price(buy))
        self.assertTrue(self.bot.fee_validation_status()['buy_allowed'])

    def test_weak_net_edge_blocks_direct_bypass_without_order_or_cap_mutation(self):
        before=self.bot.database.fetch_all_orders()
        self.assertIsNone(self.bot._submit_order(1,'BUY',D('99.9'),D(1)))
        self.assertEqual(self.bot.database.fetch_all_orders(),before)
        self.assertIsNone(self.bot.database.get_state('allocation_buy_state'))
        self.assertEqual(self.exchange.orders,{})

    def test_market_seed_requires_explicit_quote_budget_and_funds_real_sell_minimums(self):
        with self.assertRaisesRegex(TradingHalt,'explicit quote budget'):
            self.bot._submit_order(0,'BUY',D(100),D(5),order_type='MARKET')
        self.assertIsNone(self.bot._submit_order(0,'BUY',D(100),D('.01'),
                                                order_type='MARKET',quote_cost=D(1)))
        self.assertEqual(self.exchange.orders,{})

    def test_unprofitable_geometry_is_rejected_before_seed_purchase(self):
        other=self.path.parent/'strict';other.mkdir()
        with self.assertRaisesRegex(GridSizingError,'net return'):
            fee_bot(other/'grid_testnet.sqlite',env={'GRID_MIN_NET_RETURN_PERCENT':'20'})
        self.assertEqual(GridDatabase(other/'grid_testnet.sqlite').fetch_all_orders(),[])

    def test_unfundable_precision_never_increases_order_to_force_an_entry(self):
        with patch.object(self.bot,'_amount',return_value=D(0)):
            self.assertIsNone(self.bot._submit_order(1,'BUY',D(90),D('.1')))
        self.assertEqual(self.exchange.orders,{})

    def test_unvalued_positive_third_asset_fee_blocks_buys_but_keeps_exits(self):
        self.working();sells=self.sells()
        seed=self.bot.database.fetch_latest_orders_by_level()[0]
        key='fill_snapshot:'+seed['order_id']
        snap=json.loads(self.bot.database.get_state(key))
        snap['fees']=[{'currency':'BNB','cost':'0.001'}]
        self.bot.database.set_state(key,json.dumps(snap))
        self.assertEqual(self.bot.fee_validation_status()['audit']['reason_code'],'UNVALUED_COMMISSION_ASSET')
        self.bot._sync_fee_validation()
        self.assertEqual(self.sells(),sells)
        self.assertFalse(any(r['side']=='BUY' for r in self.bot.database.fetch_active_grids()))
        self.assertIsNone(self.bot._submit_order(2,'BUY',D(81),D(1)))

    def test_fee_overrun_protects_newly_filled_lot_and_preserves_older_sell_prices(self):
        self.working();sells=self.sells()
        buy=next(r for r in self.bot.database.fetch_active_grids() if r['side']=='BUY')
        order=self.exchange.orders[buy['order_id']]
        order['fees']=[{'currency':'BTC','cost':'0.02'}]
        self.exchange.fill(buy['client_order_id']);self.exchange.base_free-=D('.02')
        self.bot.run_cycle()
        self.assertEqual(self.bot.fee_validation_status()['audit']['reason_code'],'COMMISSION_EXCEEDS_MODEL')
        active=self.bot.database.fetch_active_grids()
        for identity,values in sells.items():
            row=self.bot.database.get_order(identity)
            self.assertEqual((row['price'],row['amount']),values)
        self.assertTrue(any(r['side']=='SELL' and r['parent_order_id']==buy['order_id'] for r in active))
        self.assertFalse(any(r['side']=='BUY' for r in active))

    def test_existing_low_edge_exit_is_warned_but_never_blocked_or_moved(self):
        self.working()
        seed=self.bot.database.fetch_latest_orders_by_level()[0]
        self.bot._cancel_bot_orders(side='sell',marker_prefix='test_exit_change')
        identity=self.bot._submit_order(99,'SELL',D(90),D(1),parent_order_id=seed['order_id'])
        self.assertIsNotNone(identity)
        audit=self.bot._fee_audit()
        self.assertTrue(audit['allowed']);self.assertEqual(audit['held_exits_below_model'],1)
        self.assertEqual(D(self.bot.database.get_order(identity)['price']),D(90))

    def test_cancel_racing_fill_gets_its_frozen_exit_while_new_entries_stay_blocked(self):
        self.working();old=self.sells()
        buy=next(r for r in self.bot.database.fetch_active_grids() if r['side']=='BUY')
        target=self.bot._paired_sell_price(buy)
        seed=self.bot.database.fetch_latest_orders_by_level()[0]
        key='fill_snapshot:'+seed['order_id'];snap=json.loads(self.bot.database.get_state(key))
        snap['fees']=[{'currency':'BNB','cost':'0.001'}]
        self.bot.database.set_state(key,json.dumps(snap))
        self.exchange.cancel_not_found_once=True
        self.bot.run_cycle()
        child=next(r for r in self.bot.database.fetch_active_grids() if r['parent_order_id']==buy['order_id'])
        self.assertEqual(D(child['price']),target)
        for identity,values in old.items():
            row=self.bot.database.get_order(identity);self.assertEqual((row['price'],row['amount']),values)
        self.assertFalse(any(r['side']=='BUY' for r in self.bot.database.fetch_active_grids()))

    def test_cancel_response_timeout_keeps_durable_markers_and_retries_without_new_buys(self):
        self.working();sells=self.sells()
        seed=self.bot.database.fetch_latest_orders_by_level()[0]
        key='fill_snapshot:'+seed['order_id'];snap=json.loads(self.bot.database.get_state(key))
        snap['fees']=[{'currency':'BNB','cost':'0.001'}];self.bot.database.set_state(key,json.dumps(snap))
        self.exchange.cancel_timeout='after'
        with patch.object(engine.time,'sleep',return_value=None):self.bot._sync_fee_validation()
        self.bot.run_cycle()
        self.assertEqual(self.sells(),sells)
        self.assertFalse(any(r['side']=='BUY' for r in self.bot.database.fetch_active_grids()))

    def test_malformed_commission_snapshot_blocks_new_orders(self):
        self.working()
        seed=self.bot.database.fetch_latest_orders_by_level()[0]
        key='fill_snapshot:'+seed['order_id'];snap=json.loads(self.bot.database.get_state(key))
        snap['quote_fee']='NaN';self.bot.database.set_state(key,json.dumps(snap))
        before=len(self.exchange.orders)
        self.assertFalse(self.bot.fee_validation_status()['buy_allowed'])
        self.assertIsNone(self.bot._submit_order(2,'BUY',D(81),D(1)))
        self.assertEqual(len(self.exchange.orders),before)

    def test_fee_rejected_atr_candidate_keeps_original_geometry_and_working_exits(self):
        other=self.path.parent/'atr';other.mkdir()
        regime=Mock();regime.buys_allowed.return_value=True
        regime.status.return_value=dict(enabled=True,available=True,buy_allowed=True,reason_code='RANGE')
        source=dict(snapshot=ATRSnapshot(END,D(2),D(100)),clock=0,hour=END)
        bot,exchange=fee_bot(other/'grid_testnet.sqlite',env={'GRID_MIN_NET_RETURN_PERCENT':'1'},
                            regime_monitor=regime,atr_monitor=monitor_for(source))
        for _ in range(4):bot.run_cycle()
        self.assertIsNone(bot._auto_atr_state()['active'])
        self.assertEqual(bot.database.get_state('auto_atr_decision'),'FEE_NET_EDGE_TOO_SMALL')
        self.assertTrue(any(r['side']=='SELL' for r in bot.database.fetch_active_grids()))
        self.assertEqual(bot.config.lower_price,D(80));self.assertEqual(bot.config.upper_price,D(120))

    def test_missing_trade_commissions_block_entries_without_reading_exchange(self):
        self.working();self.exchange.has={'fetchOrderTrades':True}
        with patch.object(self.exchange,'fetch_order',side_effect=AssertionError('Audit is local')):
            status=self.bot.fee_validation_status()
        self.assertFalse(status['buy_allowed'])
        self.assertEqual(status['audit']['reason_code'],'UNVERIFIED_COMMISSION')
        self.assertIsNone(self.bot._submit_order(1,'BUY',D(90),D(1)))

    def test_zero_verified_fees_are_valid_not_recharged_as_full_planning_fees(self):
        self.working()
        seed=self.bot.database.fetch_latest_orders_by_level()[0]
        key='fill_snapshot:'+seed['order_id']
        snap=json.loads(self.bot.database.get_state(key));snap['fee_source']='trades'
        snap['fees']=[{'currency':'BTC','cost':'0'}]
        self.bot.database.set_state(key,json.dumps(snap))
        audit=self.bot._fee_audit()
        self.assertTrue(audit['allowed']);self.assertGreater(audit['verified_orders'],0)
        self.assertGreater(audit['worst_held_exit_net_return'],D('.09'))

    def test_forecast_and_status_do_not_add_profit_to_trade_history(self):
        self.working();before=self.bot.database.fetch_trade_history()
        for _ in range(3):self.bot.fee_validation_status()
        self.assertEqual(self.bot.database.fetch_trade_history(),before)
        self.assertEqual(self.bot.database.realized_pnl_summary()['total'],0)

    def test_round_trip_books_once_using_existing_commission_accounting(self):
        self.working()
        buy=next(r for r in self.bot.database.fetch_active_grids() if r['side']=='BUY')
        self.exchange.fill(buy['client_order_id']);self.bot.run_cycle()
        sale=next(r for r in self.bot.database.fetch_active_grids() if r['parent_order_id']==buy['order_id'])
        self.exchange.fill(sale['client_order_id']);self.bot.run_cycle();self.bot.run_cycle()
        trades=self.bot.database.fetch_trade_history()
        self.assertEqual(len(trades),1)
        expected=(D(sale['price'])*(1-D('.001'))-D(buy['price'])*(1+D('.001')))*D(sale['amount'])
        self.assertEqual(D(trades[0]['profit']),expected)

    def test_restart_preserves_profile_and_rejects_changed_costs_or_disable(self):
        self.working();state=self.bot.database.get_state(policy.STATE_KEY)
        with patch.dict(os.environ,FEE_ENV):
            restored=OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path))
        restored.prepare(persist=False)
        self.assertEqual(restored.database.get_state(policy.STATE_KEY),state)
        for env in (dict(FEE_ENV,GRID_BUY_FEE_PERCENT='0.2'),{'GRID_FEE_VALIDATION_ENABLED':'false'}):
            with patch.dict(os.environ,env):
                with self.assertRaises(TradingHalt):
                    OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path))

    def test_mainnet_activation_is_rejected(self):
        with patch.dict(os.environ,FEE_ENV),patch.object(engine,'TRADING_SETTINGS',SimpleNamespace(sandbox=False)):
            with self.assertRaisesRegex(ValueError,'Testnet'):
                OfflineGridBot(self.bot.config,self.exchange,self.bot.database)

    def test_hard_stop_ignores_fee_filter_and_sells_only_bot_btc(self):
        self.working();self.exchange.price=D(69)
        with patch.object(self.bot,'_fee_audit',side_effect=AssertionError('Stop must run first')):
            self.bot.run_cycle()
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY),LIQUIDATED)
        self.assertEqual(self.exchange.base_free,D(1))

    def test_dollar_exit_ignores_invalid_fee_data(self):
        other=self.path.parent/'risk';other.mkdir()
        bot,exchange=fee_bot(other/'grid_testnet.sqlite',env={
            'GRID_RISK_BUDGET_ENABLED':'true','GRID_RISK_BUDGET_USDT':'20'})
        bot.run_cycle();bot.run_cycle()
        buy=next(r for r in bot.database.fetch_active_grids() if r['side']=='BUY')
        exchange.fill(buy['client_order_id']);exchange.price=D(74)
        with patch.object(bot,'_fee_audit',side_effect=AssertionError('Dollar exit first')):
            bot.run_cycle()
        self.assertEqual(bot.database.get_state('halt_reason'),'campaign_risk_limit')
        self.assertEqual(exchange.base_free,D(1))

    def test_all_six_features_recycle_without_changing_risk_or_frozen_targets(self):
        other=self.path.parent/'combined';other.mkdir()
        regime=Mock();regime.buys_allowed.return_value=True
        regime.status.return_value=dict(enabled=True,available=True,buy_allowed=True,reason_code='RANGE')
        source=dict(snapshot=ATRSnapshot(END,D(2),D(100)),clock=0,hour=END)
        bot,exchange=fee_bot(other/'grid_testnet.sqlite',env={
            'GRID_INVENTORY_SIZING_ENABLED':'true','GRID_RISK_BUDGET_ENABLED':'true',
            'GRID_RISK_BUDGET_USDT':'400','GRID_CAPITAL_RECYCLING_ENABLED':'true'},
            regime_monitor=regime,atr_monitor=monitor_for(source))
        for _ in range(4):bot.run_cycle()
        self.assertIsNotNone(bot._auto_atr_state()['active'])
        bot._cancel_bot_orders(side='buy',marker_prefix='macro_pause_buy')
        bot._exhaust_cap(None,'Test cap')
        sale=next(r for r in bot.database.fetch_active_grids() if r['side']=='SELL')
        exchange.fill(sale['client_order_id']);bot._record_filled_sell(sale)
        risk=bot.database.get_state('campaign_risk_budget')
        identity=bot._try_recycle_cap(D(100))
        self.assertIsNotNone(identity)
        self.assertEqual(bot.database.get_state('auto_atr_exit:'+identity),
                         bot.database.get_state('fee_validation_exit:'+identity))
        self.assertEqual(bot.database.get_state('campaign_risk_budget'),risk)
        self.assertLessEqual(bot._campaign_risk_view()['modeled_risk'],D(400))
        self.assertTrue(bot.fee_validation_status()['buy_allowed'])


if __name__=='__main__':unittest.main()
