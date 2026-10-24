"""Dollar risk math, persistent budget and real-engine exit regressions."""
import asyncio
from dataclasses import replace
from decimal import Decimal as D, ROUND_DOWN
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import campaign_risk as risk
import engine
from budget_policy import CAP_EXHAUSTED, CAP_STATE_KEY
from database import GridDatabase
from models import GridConfig, GridSizingError, TradingHalt
from notifier import TelegramNotifier
from portfolio import _portfolio_wallet
from settings import SAFETY_MODE_KEY, LIQUIDATED, LIQUIDATION_KEY
from tests.fakes import OfflineGridBot, FakeSpotExchange


def make_risk_bot(path, budget='20', *, config=None, prepare=True, exchange=None):
    config=config or GridConfig('BTC/USDT',D(1000),D(80),D(120),D(10),D(50),D(70),2)
    exchange=exchange or FakeSpotExchange()
    with patch.dict(os.environ,{'GRID_RISK_BUDGET_ENABLED':'true','GRID_RISK_BUDGET_USDT':budget}):
        bot=OfflineGridBot(config,exchange,GridDatabase(path))
    if prepare:bot.prepare(persist=True)
    return bot,exchange


class RiskPolicyTests(unittest.TestCase):
    def test_explicit_budget_required_and_flag_is_off_by_default(self):
        with patch.dict(os.environ,{},clear=True):self.assertIsNone(risk.configured_budget())
        for value in ('', '0', '-1', 'NaN', 'Infinity', 'not-money'):
            with patch.dict(os.environ,{'GRID_RISK_BUDGET_ENABLED':'true','GRID_RISK_BUDGET_USDT':value}):
                with self.assertRaises(ValueError):risk.configured_budget()
        with patch.dict(os.environ,{'GRID_RISK_BUDGET_ENABLED':'true'},clear=True):
            with self.assertRaises(ValueError):risk.configured_budget()
        with patch.dict(os.environ,{'GRID_RISK_BUDGET_ENABLED':'yes'}):
            with self.assertRaises(ValueError):risk.configured_budget()

    def test_projection_reserves_held_pending_and_realized_losses(self):
        view=risk.projection(D(20),D(-2),D(50),D('.5'),D(80),D(100),[(D(90),D('.2'))])
        expected_held=D(50)*(1+risk.FEE)-D('.5')*risk.exit_value(D(80))
        expected_pending=D('.2')*risk.unit_loss(D(90),D(80))
        self.assertEqual(view['modeled_risk'],D(2)+expected_held+expected_pending)
        self.assertEqual(view['headroom'],D(20)-view['modeled_risk'])
        self.assertEqual(view['exit_trigger_usdt'],D(16))

    def test_profits_do_not_expand_entry_budget_and_bad_inputs_are_rejected(self):
        empty=lambda realized:risk.projection(D(20),D(realized),D(0),D(0),D(80),D(100),[])
        self.assertEqual(empty(200)['headroom'],D(20))
        self.assertEqual(empty(-5)['headroom'],D(15))
        with self.assertRaises(ValueError):
            risk.projection(D(20),D(0),D(50),D(0),D(80),D(100),[])
        with self.assertRaises(ValueError):
            risk.projection(D('NaN'),D(0),D(0),D(0),D(80),D(100),[])

    def test_quantity_and_seed_fit_exact_risk_budget_without_rounding_up(self):
        rounded=lambda value:value.quantize(D('.001'),rounding=ROUND_DOWN)
        quantity=risk.limit_quantity(D(10),D(100),D(80),D(2),rounded)
        self.assertLessEqual(quantity*risk.unit_loss(D(100),D(80)),D(2))
        self.assertLessEqual(quantity,D(10))
        quote=risk.seed_quote(D(50),D(100),D(80),D(2),D('.5'))
        self.assertLessEqual(quote/100*risk.unit_loss(D(100),D(80)),D(1))
        with self.assertRaises(ValueError):
            risk.limit_quantity(D(10),D(100),D(80),D(2),lambda value:D(10))


class CampaignRiskEngineTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.path=Path(folder.name)/'grid_testnet.sqlite'
        alerts=patch.object(engine,'schedule_telegram_alert',return_value=False)
        alerts.start();self.addCleanup(alerts.stop)
        self.bot,self.exchange=make_risk_bot(self.path)

    def working(self):
        self.bot.run_cycle();self.bot.run_cycle()
        self.assertLessEqual(self.bot._campaign_risk_view()['modeled_risk'],D(20))

    def fill_buy(self):
        buy=next(r for r in self.bot.database.fetch_active_grids() if r['side']=='BUY')
        self.exchange.fill(buy['client_order_id'])
        return buy

    def test_seed_and_pending_buys_fit_dollars_not_just_allocation(self):
        self.working()
        seed=self.bot.database.fetch_latest_orders_by_level()[0]
        snapshot=json.loads(self.bot.database.get_state('fill_snapshot:'+seed['order_id']))
        self.assertLess(D(snapshot['filled_quote']),D(500))
        self.assertGreater(D(snapshot['filled_quote']),D(10))
        self.assertIsNone(self.bot.database.get_state(CAP_STATE_KEY))
        self.assertIsNone(self.bot.database.get_state(SAFETY_MODE_KEY))

    def test_final_guard_cannot_bypass_risk_with_limit_or_market_buy(self):
        before=len(self.exchange.orders)
        self.assertIsNone(self.bot._submit_order(1,'BUY',D(90),D(10)))
        self.assertIsNone(self.bot._submit_order(0,'BUY',D(100),D(5),order_type='MARKET',quote_cost=D(500)))
        with self.assertRaisesRegex(TradingHalt,'explicit quote budget'):
            self.bot._submit_order(0,'BUY',D(100),D(1),order_type='MARKET')
        with self.assertRaisesRegex(TradingHalt,'LIMIT BUY'):
            self.bot._submit_order(1,'BUY',D(90),D(10),quote_cost=D(1))
        self.assertEqual(len(self.exchange.orders),before)

    def test_all_three_features_combine_and_risk_exit_ignores_entry_gates(self):
        self.bot._inventory_sizing_enabled=True
        monitor=Mock()
        monitor.buys_allowed.return_value=False
        monitor.status.return_value=dict(enabled=True,available=True,buy_allowed=False,reason_code='DOWNTREND')
        self.bot._regime_monitor=monitor
        self.bot.run_cycle()
        self.assertEqual(self.exchange.orders,{})
        monitor.buys_allowed.return_value=True
        self.working()
        inventory=self.bot._inventory_budget()
        self.assertLessEqual(inventory['position_cost']+inventory['pending_cost'],inventory['ceiling'])
        self.fill_buy()
        monitor.buys_allowed.return_value=False
        self.bot._macro_trend_provider=lambda:False
        self.exchange.price=D(74)
        self.bot.run_cycle()
        self.assertEqual(self.exchange.market_sell_calls,1)
        self.assertEqual(self.bot.database.get_state('halt_reason'),'campaign_risk_limit')

    def test_partial_buy_transfers_pending_risk_to_held_and_keeps_remaining_reserved(self):
        self.working()
        row=next(r for r in self.bot.database.fetch_active_grids() if r['side']=='BUY')
        before=self.bot._campaign_risk_view()
        partial=(D(row['amount'])/2).quantize(D('.001'),rounding=ROUND_DOWN)
        order=self.exchange.orders[row['order_id']]
        order.update(filled=str(partial),cost=str(partial*D(row['price'])))
        self.bot._fetch_order(row)
        after=self.bot._campaign_risk_view()
        self.assertGreater(after['held_stop_risk'],before['held_stop_risk'])
        self.assertLess(after['pending_stop_risk'],before['pending_stop_risk'])
        # This fake reports zero entry commission, releasing only the extra
        # acquisition-fee allowance, not the partial fill's stop risk.
        released=partial*D(row['price'])*risk.FEE*(1+risk.FEE)
        self.assertAlmostEqual(after['headroom']-before['headroom'],released)
        self.assertEqual(after['pending_stop_risk'],
                         (D(row['amount'])-partial)*risk.unit_loss(D(row['price']),D(70)))

    def test_cancel_failure_keeps_remaining_buy_reserved_and_retries(self):
        self.working()
        self.bot.database.record_trade('100','95','-5',sell_order_id='loss-reservation')
        self.exchange.cancel_timeout='before'
        with patch.object(engine.time,'sleep',return_value=None):
            allowed,_=self.bot._sync_campaign_risk_buys()
        self.assertFalse(allowed)
        self.bot._sync_campaign_risk_buys()
        self.assertFalse(any(r['side']=='BUY' for r in self.bot.database.fetch_active_grids()))
        self.assertTrue(any(r['side']=='SELL' for r in self.bot.database.fetch_active_grids()))
        self.assertLessEqual(self.bot._campaign_risk_view()['modeled_risk'],D(20))

    def test_corrupt_fill_blocks_new_buys_and_reports_unavailable(self):
        self.working()
        seed=self.bot.database.fetch_latest_orders_by_level()[0]
        self.bot.database.set_state('fill_snapshot:'+seed['order_id'],'{}')
        self.assertFalse(self.bot.campaign_risk_status()['available'])
        before=len(self.exchange.orders)
        self.assertIsNone(self.bot._submit_order(1,'BUY',D(90),D('.1')))
        self.assertEqual(len(self.exchange.orders),before)

    def test_notifier_labels_budget_exit_without_price_stop_language(self):
        with patch('notifier.schedule_telegram_alert',return_value=False) as send:
            asyncio.run(TelegramNotifier().notify_campaign_risk(True))
        message=send.call_args.args[0]
        self.assertIn('CAMPAIGN DOLLAR RISK LIMIT',message)
        self.assertNotIn('HARD STOP',message)

    def test_dollar_exit_happens_above_existing_hard_stop_and_preserves_wallet_btc(self):
        self.working();self.fill_buy()
        self.exchange.price=D(74)
        self.assertGreater(self.exchange.price,self.bot.config.stop_loss_price)
        self.bot.run_cycle()
        state=json.loads(self.bot.database.get_state(LIQUIDATION_KEY))
        self.assertEqual(state['trigger_event'],risk.EXIT_EVENT)
        self.assertEqual(state['phase'],'complete')
        self.assertEqual(self.bot.database.get_state('halt_reason'),'campaign_risk_limit')
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY),LIQUIDATED)
        self.assertEqual(self.exchange.market_sell_calls,1)
        self.assertEqual(self.exchange.base_free,D(1))
        self.assertEqual(self.exchange.fetch_open_orders(),[])
        self.assertTrue(json.loads(self.bot.database.get_state(risk.STATE_KEY))['latched'])
        with self.assertRaisesRegex(TradingHalt,'Campaign dollar risk'):
            self.bot.start_engine()
        with self.assertRaises(TradingHalt):self.bot._assert_liquidation_reset_ready()

    def test_price_stop_remains_first_and_does_not_depend_on_budget_projection(self):
        self.working()
        self.exchange.price=D(69)
        with patch.object(self.bot,'_campaign_risk_view',side_effect=AssertionError('Hard stop first')):
            self.bot.run_cycle()
        self.assertEqual(self.exchange.market_sell_calls,1)
        self.assertEqual(json.loads(self.bot.database.get_state(LIQUIDATION_KEY))['trigger_event'],'HARD_STOP')

    def test_flat_realized_loss_budget_cancels_and_halts_without_a_market_sale(self):
        self.bot.database.record_trade('100','83','-17',sell_order_id='closed-test-trade')
        self.bot.run_cycle()
        self.assertEqual(self.exchange.market_sell_calls,0)
        self.assertEqual(self.exchange.orders,{})
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY),LIQUIDATED)

    def test_restart_keeps_budget_and_rejects_disable_increase_or_symbol_change(self):
        state=self.bot.database.get_state(risk.STATE_KEY)
        with patch.dict(os.environ,{'GRID_RISK_BUDGET_ENABLED':'true','GRID_RISK_BUDGET_USDT':'20'}):
            recovered=OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path))
        self.assertEqual(recovered.database.get_state(risk.STATE_KEY),state)
        for flag,budget in (('false','20'),('true','21'),('true','10')):
            with patch.dict(os.environ,{'GRID_RISK_BUDGET_ENABLED':flag,'GRID_RISK_BUDGET_USDT':budget}):
                with self.assertRaises(TradingHalt):
                    OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path))

    def test_existing_trade_history_cannot_be_rebased_as_a_fresh_campaign(self):
        other=self.path.parent/'old';other.mkdir()
        database=GridDatabase(other/'grid_testnet.sqlite')
        database.record_trade('100','90','-10',sell_order_id='old-history')
        with patch.dict(os.environ,{'GRID_RISK_BUDGET_ENABLED':'true','GRID_RISK_BUDGET_USDT':'20'}):
            with self.assertRaisesRegex(TradingHalt,'fresh isolated ledger'):
                OfflineGridBot(self.bot.config,FakeSpotExchange(),database)

    def test_mainnet_and_budget_larger_than_allocation_are_rejected(self):
        other=self.path.parent/'main';other.mkdir()
        with patch.dict(os.environ,{'GRID_RISK_BUDGET_ENABLED':'true','GRID_RISK_BUDGET_USDT':'20'}):
            with patch.object(engine,'TRADING_SETTINGS',SimpleNamespace(sandbox=False)):
                with self.assertRaisesRegex(ValueError,'Testnet'):
                    OfflineGridBot(self.bot.config,FakeSpotExchange(),GridDatabase(other/'grid_testnet.sqlite'))
        with patch.dict(os.environ,{'GRID_RISK_BUDGET_ENABLED':'true','GRID_RISK_BUDGET_USDT':'1000'}):
            with self.assertRaises(ValueError):
                OfflineGridBot(self.bot.config,self.exchange,self.bot.database)

    def test_factory_reset_cannot_erase_risk_consumption(self):
        with self.assertRaisesRegex(TradingHalt,'risk history'):self.bot.prepare_factory_reset()
        with self.assertRaisesRegex(ValueError,'risk'):self.bot.database.factory_reset()
        self.assertIsNotNone(self.bot.database.get_state(risk.STATE_KEY))

    def test_unbooked_partial_sell_losses_are_reserved_before_trade_history(self):
        self.working()
        seed=self.bot.database.fetch_latest_orders_by_level()[0]
        upper=next(r for r in self.bot.database.fetch_active_grids() if r['side']=='SELL')
        # Release an initial SELL so its owned quantity can be sold by another
        # tracked order in this deterministic loss-accounting scenario.
        self.bot._cancel_bot_orders(side='sell',marker_prefix='test_cancel')
        identity=self.bot._submit_order(99,'SELL',D(90),D('.2'),parent_order_id=seed['order_id'])
        order=self.exchange.orders[identity]
        order.update(filled='0.1',cost='9')
        self.exchange.quote_free+=D(9)
        row=self.bot.database.get_order(identity);self.bot._fetch_order(row)
        self.assertEqual(self.bot.database.fetch_trade_history(),[])
        view=self.bot._campaign_risk_view(mark=D(100))
        self.assertLess(view['unbooked_loss_reserve'],0)
        self.assertLess(view['realized_pnl'],0)

    def test_unknown_risk_blocks_new_buys_without_moving_sell_targets(self):
        self.working()
        sells={r['order_id']:r['price'] for r in self.bot.database.fetch_active_grids() if r['side']=='SELL'}
        with patch.object(self.bot,'_campaign_risk_view',side_effect=TradingHalt('Unknown')):
            self.bot._sync_campaign_risk_buys()
            self.assertIsNone(self.bot._submit_order(1,'BUY',D(90),D('.1')))
        self.assertEqual({r['order_id']:r['price'] for r in self.bot.database.fetch_active_grids()},sells)

    def test_crash_after_durable_trip_resumes_same_exit_even_after_price_recovers(self):
        self.working();self.fill_buy();self.exchange.price=D(74)
        with patch.object(self.bot,'_liquidate_locked',side_effect=KeyboardInterrupt('crash')):
            with self.assertRaises(KeyboardInterrupt):self.bot.run_cycle()
        self.assertTrue(json.loads(self.bot.database.get_state(risk.STATE_KEY))['latched'])
        self.exchange.price=D(100)
        with patch.dict(os.environ,{'GRID_RISK_BUDGET_ENABLED':'true','GRID_RISK_BUDGET_USDT':'20'}):
            recovered=OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path))
        recovered.prepare(persist=False);recovered.run_cycle()
        self.assertEqual(self.exchange.market_sell_calls,1)
        self.assertEqual(recovered.database.get_state(SAFETY_MODE_KEY),LIQUIDATED)

    def test_risk_exit_uses_existing_exact_id_recovery_for_lost_sale_response(self):
        self.working();self.fill_buy();self.exchange.price=D(74)
        self.exchange.market_sell_timeout='after'
        with patch.object(engine.time,'sleep',return_value=None):self.bot.run_cycle()
        self.assertEqual(self.exchange.market_sell_calls,1)
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY),LIQUIDATED)

    def test_reviewed_reanchor_does_not_reset_dollars_or_realized_drawdown(self):
        state=self.bot.database.get_state(risk.STATE_KEY)
        self.bot.database.record_trade('100','98','-2',sell_order_id='booked-test-loss')
        self.bot.request_manual_recenter('100','20','30','1000',2)
        self.assertTrue(self.bot.reset_grid())
        self.assertEqual(self.bot.database.get_state(risk.STATE_KEY),state)
        self.assertEqual(self.bot._campaign_risk_view()['booked_realized_pnl'],D(-2))

    def test_small_risk_budget_rejects_unfundable_seed_before_any_order(self):
        other=self.path.parent/'small';other.mkdir()
        config=replace(self.bot.config,investment_quote=D(100),buy_grid_levels=3,sell_grid_levels=2)
        with self.assertRaises(GridSizingError):
            make_risk_bot(other/'grid_testnet.sqlite','2',config=config)
        self.assertEqual(GridDatabase(other/'grid_testnet.sqlite').fetch_all_orders(),[])

    def test_campaign_notification_is_distinct_from_a_price_stop(self):
        self.working();self.fill_buy();self.exchange.price=D(74);self.bot.run_cycle()
        notifier=SimpleNamespace(notify_campaign_risk=AsyncMock(),notify_hard_stop=AsyncMock())
        asyncio.run(self.bot._notify_liquidation(notifier))
        notifier.notify_campaign_risk.assert_awaited_once_with(True)
        notifier.notify_hard_stop.assert_not_called()


if __name__=='__main__':unittest.main()
