"""Durable sale-funded cap episodes against the real engine and offline exchange."""
from decimal import Decimal as D
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
import ccxt

import capital_recycling as policy
import engine
from budget_policy import CAP_STATE_KEY, CAP_ORDER_KEY, CAP_EXHAUSTED
from database import GridDatabase
from models import GridConfig, TradingHalt, UncertainOrderError
from settings import SAFETY_MODE_KEY, PAUSED_MANUAL, LIQUIDATED, LAST_MARKET_PRICE_KEY
from tests.fakes import OfflineGridBot, FakeSpotExchange
from tests.test_auto_atr import END, monitor_for
from auto_atr import ATRSnapshot


def make_recycling_bot(path, **kwargs):
    config = GridConfig('BTC/USDT', D(1000), D(80), D(120), D(10), D(50), D(70), 2)
    exchange = kwargs.pop('exchange', None) or FakeSpotExchange()
    with patch.dict(os.environ, {'GRID_CAPITAL_RECYCLING_ENABLED': 'true'}):
        bot = OfflineGridBot(config, exchange, GridDatabase(path), **kwargs)
    bot.prepare(persist=True)
    return bot, exchange


class RecyclingPolicyTests(unittest.TestCase):
    def test_default_off_strict_flag(self):
        with patch.dict(os.environ, {}, clear=True):self.assertFalse(policy.configured_enabled())
        for flag in ('true', '1'):
            with patch.dict(os.environ, {'GRID_CAPITAL_RECYCLING_ENABLED': flag}):
                self.assertTrue(policy.configured_enabled())
        with patch.dict(os.environ, {'GRID_CAPITAL_RECYCLING_ENABLED': 'yes'}):
            with self.assertRaises(ValueError):policy.configured_enabled()

    def test_original_cap_and_fee_reserve_are_exact_and_invalid_inputs_fail(self):
        self.assertTrue(policy.fits(D(100), D(40), D(10), D(49)))
        self.assertFalse(policy.fits(D(100), D(40), D(10), D(50)))
        self.assertFalse(policy.fits(D(100), D(0), D(0), D(101)))
        for value in (D('NaN'), D(-1), 2):
            with self.assertRaises(ValueError):policy.fits(D(100), value, D(0), D(1))


class RecyclingEngineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'grid_testnet.sqlite'
        alerts = patch.object(engine, 'schedule_telegram_alert', return_value=False)
        alerts.start();self.addCleanup(alerts.stop)
        self.bot, self.exchange = make_recycling_bot(self.path)
        self.seed_and_exhaust()

    def seed_and_exhaust(self, cost='995'):
        cost = D(cost)
        self.bot._submit_order(0, 'BUY', D(100), cost/100, order_type='MARKET', quote_cost=cost)
        self.bot.run_cycle()
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY), CAP_EXHAUSTED)

    def sell_seed(self):
        row = next(r for r in self.bot.database.fetch_active_grids() if r['side']=='SELL')
        self.exchange.fill(row['client_order_id'])
        self.bot._record_filled_sell(row)
        return row

    def episode(self):return self.bot._recycling_state()['episode']

    def test_real_cap_pause_releases_once_on_new_profitable_sell_and_reserves_full_buy(self):
        episode = self.episode()['id']
        sale = self.sell_seed()
        profit_before = self.bot.database.realized_pnl_summary()['total']
        identity = self.bot._try_recycle_cap(D(100))
        self.assertIsNotNone(identity)
        state = self.bot._recycling_state()
        self.assertIsNone(state['episode'])
        self.assertEqual(state['last_release']['episode_id'], episode)
        self.assertEqual(state['last_release']['sell_order_id'], sale['order_id'])
        self.assertEqual(state['last_release']['buy_order_id'], identity)
        self.assertIsNone(self.bot.database.get_state(CAP_STATE_KEY))
        held, pending = policy.exposure(self.bot.database)
        self.assertLessEqual(held+pending, D(1000))
        self.assertEqual(self.bot.config.investment_quote, D(1000))
        self.assertEqual(self.bot.database.realized_pnl_summary()['total'], profit_before)
        before = len(self.exchange.orders)
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.assertEqual(len(self.exchange.orders), before)

    def test_normal_cycle_books_sale_and_recycles_without_manual_method_calls(self):
        sale = next(r for r in self.bot.database.fetch_active_grids() if r['side']=='SELL')
        self.exchange.fill(sale['client_order_id'])
        self.bot.run_cycle()
        self.assertIsNotNone(self.bot._recycling_state()['last_release'])
        self.assertEqual(len(self.bot.database.fetch_trade_history()), 1)
        held, pending = policy.exposure(self.bot.database)
        self.assertLessEqual(held+pending, D(1000))

    def test_no_unlock_on_time_price_deposit_or_old_profit(self):
        self.exchange.quote_free += D(5000)
        self.bot.database.record_trade('100', '120', '200', sell_order_id='unrelated-profit')
        before = len(self.exchange.orders)
        for price in (D(100), D(101), D(99)):
            self.assertIsNone(self.bot._try_recycle_cap(price))
        self.assertEqual(len(self.exchange.orders), before)
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY), CAP_EXHAUSTED)

    def test_sell_filled_before_episode_but_booked_afterward_cannot_unlock(self):
        sale = next(r for r in self.bot.database.fetch_active_grids() if r['side']=='SELL')
        self.exchange.fill(sale['client_order_id'])
        self.bot._fetch_order(sale)
        self.bot._clear_cap_exhaustion();self.bot._exhaust_cap(None, 'New pause')
        self.bot._record_filled_sell(sale)
        self.assertIn(sale['order_id'], self.episode()['excluded_sells'])
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))

    def test_active_final_residual_blocks_release_until_verified_cancellation(self):
        other = self.path.parent/'final';other.mkdir()
        self.bot, self.exchange = make_recycling_bot(other/'grid_testnet.sqlite')
        self.seed_and_exhaust('917.2')
        final_id = self.bot.database.get_state(CAP_ORDER_KEY)
        self.assertIsNotNone(final_id)
        self.sell_seed()
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.bot._cancel_bot_orders(side='buy', selected_ids={final_id}, marker_prefix='macro_pause_buy')
        self.assertIsNotNone(self.bot._try_recycle_cap(D(100)))

    def test_missing_final_intent_or_cumulative_snapshot_cannot_be_assumed_settled(self):
        self.sell_seed()
        state=self.bot._recycling_state();state['episode']['final_order_id']='missing-order'
        self.bot.database.set_state(policy.STATE_KEY,json.dumps(state))
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)

    def test_late_exchange_fee_details_are_required_when_trade_api_exists(self):
        self.sell_seed()
        self.exchange.has={'fetchOrderTrades':True}
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)

    def test_partial_sell_never_qualifies_as_release_evidence(self):
        sale = next(r for r in self.bot.database.fetch_active_grids() if r['side']=='SELL')
        order = self.exchange.orders[sale['order_id']]
        order.update(filled='1', cost='110')
        self.bot._fetch_order(sale)
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.assertEqual(self.bot.database.fetch_trade_history(), [])

    def test_full_order_room_required_instead_of_repeated_residual_orders(self):
        other=self.path.parent/'room';other.mkdir()
        self.bot,self.exchange=make_recycling_bot(other/'grid_testnet.sqlite')
        self.seed_and_exhaust('917.2')
        final_id=self.bot.database.get_state(CAP_ORDER_KEY)
        self.exchange.fill(final_id);self.bot.run_cycle()
        self.sell_seed()
        self.exchange.price=D(101);self.bot.database.set_state(LAST_MARKET_PRICE_KEY,'101')
        before=len(self.exchange.orders)
        self.assertIsNone(self.bot._try_recycle_cap(D(101)))
        self.assertEqual(len(self.exchange.orders),before)
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)

    def test_losing_ordinary_sale_does_not_release_even_with_more_cash(self):
        sale = next(r for r in self.bot.database.fetch_active_grids() if r['side']=='SELL')
        order = self.exchange.orders[sale['order_id']]
        order['cost'] = str(D(order['amount'])*90)
        order['price'] = '90'
        self.exchange.fill(sale['client_order_id']);self.bot._record_filled_sell(sale)
        self.assertLess(D(self.bot.database.fetch_trade_history()[0]['profit']), 0)
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))

    def test_guards_pauses_macro_bounds_cash_and_full_sizing(self):
        self.sell_seed()
        for field, value in ((SAFETY_MODE_KEY, PAUSED_MANUAL), ('engine_fault','Review'),
                             ('engine_status','IDLE')):
            self.bot.database.set_state(field,value)
            self.assertIsNone(self.bot._try_recycle_cap(D(100)))
            self.bot.database.clear_state(field)
        self.bot._macro_trend_provider=lambda:False
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.bot._macro_trend_provider=lambda:True
        self.assertIsNone(self.bot._try_recycle_cap(D(121)))
        self.exchange.quote_free=D(0)
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.exchange.quote_free=D(10000)
        self.bot._inventory_sizing_enabled=True
        with patch.object(self.bot,'_inventory_buy_amount',return_value=D('.01')):
            self.assertIsNone(self.bot._try_recycle_cap(D(100)))  # A tapered partial does not unlock.
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)

    def test_budget_or_exposure_unknown_cannot_clear_latch(self):
        self.sell_seed()
        with patch.object(policy, 'exposure', side_effect=ValueError('Unknown')):
            self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)

    def test_crash_after_reservation_before_http_keeps_one_intent_and_consumed_sale(self):
        self.sell_seed()
        before = len(self.exchange.orders)
        with patch.object(engine, 'exchange_write_attempts', side_effect=KeyboardInterrupt('crash')):
            with self.assertRaises(KeyboardInterrupt):self.bot._try_recycle_cap(D(100))
        release = self.bot._recycling_state()['last_release']
        self.assertIsNotNone(self.bot.database.get_order(release['buy_order_id']))
        self.assertEqual(len(self.exchange.orders),before)
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))

    def test_sql_failure_rolls_back_intent_release_and_latch_together(self):
        self.sell_seed();episode=self.episode();before=len(self.exchange.orders)
        original=self.bot.database.record_transition
        def fail(previous,following,event,*args,**kwargs):
            if event=='RECYCLING_RELEASE_RESERVED':raise RuntimeError('journal failed')
            return original(previous,following,event,*args,**kwargs)
        with patch.object(self.bot.database,'record_transition',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'journal failed'):self.bot._try_recycle_cap(D(100))
        self.assertEqual(self.episode(),episode)
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)
        self.assertEqual(len(self.exchange.orders),before)
        self.assertIsNone(self.bot._recycling_state()['last_release'])

    def test_definite_maker_rejection_relocks_and_cannot_reuse_the_same_sell(self):
        self.sell_seed();old_episode=self.episode()['id']
        self.exchange.reject_post_only_once=True
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)
        self.assertNotEqual(self.episode()['id'],old_episode)
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))

    def test_uncertain_acceptance_keeps_exact_reserved_id_without_second_purchase(self):
        self.sell_seed();self.exchange.fail_create=True
        with self.assertRaises(UncertainOrderError):self.bot._try_recycle_cap(D(100))
        release=self.bot._recycling_state()['last_release']
        self.assertIsNotNone(self.bot.database.get_order(release['buy_order_id']))
        self.exchange.fail_create=False;before=len(self.exchange.orders)
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.assertEqual(len(self.exchange.orders),before)

    def test_lost_response_after_acceptance_recovers_same_order_once(self):
        self.sell_seed();before=len(self.exchange.orders)
        create=self.exchange.create_order
        def timeout_after_accept(*args,**kwargs):
            create(*args,**kwargs)
            raise ccxt.RequestTimeout('Lost accepted response')
        with patch.object(self.exchange,'create_order',side_effect=timeout_after_accept):
            identity=self.bot._try_recycle_cap(D(100))
        self.assertEqual(len(self.exchange.orders),before+1)
        self.assertEqual(identity,self.bot._recycling_state()['last_release']['buy_order_id'])
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))

    def test_definite_funds_rejection_consumes_sale_without_a_replay(self):
        self.sell_seed()
        with patch.object(self.exchange,'create_order',side_effect=ccxt.InsufficientFunds('Changed wallet')):
            self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))

    def test_ledger_change_between_preflight_and_intent_keeps_cap_and_creates_no_order(self):
        self.sell_seed();before=len(self.exchange.orders)
        authorize=self.bot._recycling_authorization
        calls=0
        def changed(mark):
            nonlocal calls
            calls+=1
            return authorize(mark) if calls==1 else None
        with patch.object(self.bot,'_recycling_authorization',side_effect=changed):
            self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.assertEqual(len(self.exchange.orders),before)
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)

    def test_same_profitable_sell_cannot_unlock_a_later_episode(self):
        sale=self.sell_seed();identity=self.bot._try_recycle_cap(D(100))
        self.assertIsNotNone(identity)
        self.bot._exhaust_cap(None,'Next episode')
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.assertGreaterEqual(self.episode()['booked_through'],
                                self.bot.database.get_trade_by_sell_order_id(sale['order_id'])['id'])

    def test_restart_preserves_fence_and_completed_release_and_rejects_disable(self):
        self.sell_seed();self.assertIsNotNone(self.bot._try_recycle_cap(D(100)))
        state=self.bot.database.get_state(policy.STATE_KEY)
        with patch.dict(os.environ,{'GRID_CAPITAL_RECYCLING_ENABLED':'true'}):
            recovered=OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path))
        recovered.prepare(persist=False)
        self.assertEqual(recovered.database.get_state(policy.STATE_KEY),state)
        self.assertIsNone(recovered._try_recycle_cap(D(100)))
        with self.assertRaisesRegex(TradingHalt,'disable'):
            OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path))

    def test_opting_into_an_old_cap_pause_fences_all_preexisting_sales(self):
        self.sell_seed()
        self.bot.database.clear_state(policy.STATE_KEY)  # Model a pre-feature ledger.
        with patch.dict(os.environ,{'GRID_CAPITAL_RECYCLING_ENABLED':'true'}):
            recovered=OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path))
        recovered.prepare(persist=False)
        self.assertGreaterEqual(recovered._recycling_state()['episode']['booked_through'],1)
        self.assertIsNone(recovered._try_recycle_cap(D(100)))

    def test_mainnet_rejected_and_default_disabled_baseline_does_not_unlock(self):
        with patch.dict(os.environ,{'GRID_CAPITAL_RECYCLING_ENABLED':'true'}), \
                patch.object(engine,'TRADING_SETTINGS',SimpleNamespace(sandbox=False)):
            with self.assertRaisesRegex(ValueError,'Testnet'):
                OfflineGridBot(self.bot.config,self.exchange,self.bot.database)
        self.sell_seed();self.bot._recycling_enabled=False
        self.assertEqual(self.bot.capital_recycling_status(),{'enabled':False})
        self.assertIsNone(self.bot._try_recycle_cap(D(100)))
        self.assertEqual(self.bot.database.get_state(CAP_STATE_KEY),CAP_EXHAUSTED)

    def test_hard_stop_precedes_recycling_and_does_not_sell_reserved_wallet_btc(self):
        self.exchange.price=D(69)
        with patch.object(self.bot,'_try_recycle_cap',side_effect=AssertionError('Stop first')):
            self.bot.run_cycle()
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY),LIQUIDATED)
        self.assertEqual(self.exchange.base_free,D(1))

    def test_dollar_loss_exit_precedes_recycling_even_with_cap_paused(self):
        other=self.path.parent/'dollar';other.mkdir()
        with patch.dict(os.environ,{'GRID_RISK_BUDGET_ENABLED':'true','GRID_RISK_BUDGET_USDT':'20'}):
            bot,exchange=make_recycling_bot(other/'grid_testnet.sqlite')
        bot.run_cycle();bot.run_cycle()
        buy=next(r for r in bot.database.fetch_active_grids() if r['side']=='BUY')
        exchange.fill(buy['client_order_id'])
        bot._exhaust_cap(None,'Paused while exposed');exchange.price=D(74)
        with patch.object(bot,'_try_recycle_cap',side_effect=AssertionError('Dollar exit first')):
            bot.run_cycle()
        self.assertEqual(bot.database.get_state('halt_reason'),'campaign_risk_limit')
        self.assertEqual(exchange.base_free,D(1))

    def test_status_is_read_only_and_allocation_does_not_compound_profit(self):
        self.sell_seed();before=self.bot.database.fetch_all_orders()
        status=self.bot.capital_recycling_status()
        self.assertTrue(status['available']);self.assertFalse(status['profit_compounding'])
        self.assertEqual(D(status['allocation_usdt']),D(1000))
        self.assertEqual(self.bot.database.fetch_all_orders(),before)

    def test_all_five_features_work_together_without_resetting_risk_or_sell_targets(self):
        other=self.path.parent/'combined';other.mkdir()
        regime=Mock();regime.buys_allowed.return_value=True
        regime.status.return_value=dict(enabled=True,available=True,buy_allowed=True,reason_code='RANGE')
        source=dict(snapshot=ATRSnapshot(END,D(2),D(100)),clock=0,hour=END)
        with patch.dict(os.environ,{'GRID_INVENTORY_SIZING_ENABLED':'true',
                'GRID_RISK_BUDGET_ENABLED':'true','GRID_RISK_BUDGET_USDT':'400'}):
            bot,exchange=make_recycling_bot(other/'grid_testnet.sqlite',
                regime_monitor=regime,atr_monitor=monitor_for(source))
        for _ in range(4):bot.run_cycle()
        bot._cancel_bot_orders(side='buy',marker_prefix='macro_pause_buy')
        bot._exhaust_cap(None,'Controlled test episode')
        risk_before=bot.database.get_state('campaign_risk_budget')
        sale=next(r for r in bot.database.fetch_active_grids() if r['side']=='SELL')
        exchange.fill(sale['client_order_id']);bot._record_filled_sell(sale)
        identity=bot._try_recycle_cap(D(100))
        self.assertIsNotNone(identity)
        self.assertIsNotNone(bot.database.get_state('auto_atr_exit:'+identity))
        self.assertEqual(bot.database.get_state('campaign_risk_budget'),risk_before)
        self.assertLessEqual(bot._campaign_risk_view()['modeled_risk'],D(400))
        held,pending=policy.exposure(bot.database)
        self.assertLessEqual(held+pending,D(750))
        self.assertEqual(bot.config.stop_loss_price,D(70))


if __name__=='__main__':unittest.main()
