"""Regime filter integration against the actual engine and real SQLite."""
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import engine
from database import GridDatabase
from market_regime import RegimeMonitor, calculate_regime
from tests.test_market_regime import END, candles
from tests.fakes import make_bot, OfflineGridBot
from settings import ENGINE_STATUS_KEY, SAFETY_MODE_KEY, LIQUIDATED


class RegimeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'grid_testnet.sqlite'
        self.alerts=patch.object(engine,'schedule_telegram_alert',return_value=False)
        self.alerts.start(); self.addCleanup(self.alerts.stop)
        self.bot,self.exchange=make_bot(self.path,initial_base=D(0))
        self.monitor=Mock()
        self.set_signal('RANGE')
        self.bot._regime_monitor=self.monitor

    def set_signal(self,regime):
        self.monitor.buys_allowed.return_value=regime=='RANGE'
        self.monitor.status.return_value=dict(enabled=True,available=regime!='DATA_UNAVAILABLE',
                                              buy_allowed=regime=='RANGE',reason_code=regime)

    def working_grid(self):
        self.bot.run_cycle(); self.bot.run_cycle()
        self.assertTrue(any(o['side']=='BUY' for o in self.bot.database.fetch_active_grids()))
        self.assertTrue(any(o['side']=='SELL' for o in self.bot.database.fetch_active_grids()))

    def test_disabled_does_not_initialize_or_fetch_a_monitor(self):
        with patch.dict('os.environ',{'GRID_REGIME_FILTER_ENABLED':'false'}), \
                patch.object(engine,'RegimeMonitor') as create:
            bot=OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path))
            self.assertEqual(bot.regime_status()['reason_code'],'DISABLED')
            self.assertTrue(bot._regime_buys_allowed())
            create.assert_not_called()

    def test_experimental_feature_cannot_be_activated_on_mainnet(self):
        with patch.object(engine,'TRADING_SETTINGS',SimpleNamespace(sandbox=False)):
            with self.assertRaisesRegex(ValueError,'restricted to Testnet'):
                OfflineGridBot(self.bot.config,self.exchange,self.bot.database,
                               regime_monitor=self.monitor)

    def test_blocks_seed_and_direct_submission_without_any_order_intent(self):
        self.set_signal('DOWNTREND')
        self.bot.run_cycle()
        self.assertEqual(self.exchange.orders,{})
        self.assertIsNone(self.bot._submit_order(1,'BUY',D(90),D(1)))
        self.assertEqual(self.bot.database.fetch_all_orders(),[])
        self.assertEqual(self.bot.database.get_state('regime_buy_pause'),'1')

    def test_closed_candle_classifier_drives_real_engine_without_mock_permissions(self):
        source = {'rows': candles(-.3), 'now': 0}
        monitor = RegimeMonitor(lambda:calculate_regime(source['rows'],END),
            clock=lambda:source['now'], wall_clock=lambda:END/1000+10,
            launch=lambda job:job())
        self.bot._regime_monitor=monitor
        self.bot.run_cycle()
        self.assertEqual(self.exchange.orders,{})
        self.assertEqual(self.bot.database.get_state('regime_buy_reason'),'DOWNTREND')
        source.update(rows=candles(),now=60)
        self.working_grid()
        source.update(rows=candles(-.3),now=120)
        self.bot.run_cycle()
        self.assertTrue(all(o['side']=='SELL' for o in self.bot.database.fetch_active_grids()))

    def test_cancels_only_buys_keeps_sell_ids_prices_and_risk_floor(self):
        self.working_grid()
        sells={o['id']:D(o['price']) for o in self.exchange.orders.values()
               if o['side']=='sell' and o['status']=='open'}
        floor=self.bot.config.stop_loss_price
        self.set_signal('DOWNTREND'); self.bot.run_cycle()
        active=self.bot.database.fetch_active_grids()
        self.assertTrue(all(o['side']=='SELL' for o in active))
        self.assertEqual({o['exchange_order_id']:D(o['price']) for o in active},sells)
        self.assertEqual(self.bot.config.stop_loss_price,floor)
        self.assertEqual(self.exchange.market_sell_calls,0)

    def test_blocks_uptrend_transition_and_unavailable_not_just_downtrend(self):
        for signal in ('UPTREND','TRANSITION','DATA_UNAVAILABLE'):
            self.set_signal(signal)
            self.bot.run_cycle()
            self.assertEqual(self.exchange.orders,{})

    def test_resume_requires_both_filters_and_does_not_duplicate_orders(self):
        self.working_grid()
        self.set_signal('DOWNTREND'); self.bot.run_cycle()
        self.set_signal('RANGE')
        self.bot._macro_trend_provider=lambda:False
        self.bot.run_cycle()
        self.assertFalse(any(o['side']=='BUY' for o in self.bot.database.fetch_active_grids()))
        self.bot._macro_trend_provider=lambda:True
        self.bot.run_cycle()
        active=self.bot.database.fetch_active_grids()
        count=len(self.exchange.orders)
        self.bot.run_cycle()
        self.assertEqual(len(self.exchange.orders),count)
        self.assertEqual(len([o for o in active if o['side']=='BUY']),len(self.bot.levels))

    def test_late_buy_fill_is_protected_by_sell_while_gate_is_blocked(self):
        self.working_grid()
        buy=next(o for o in self.bot.database.fetch_active_grids() if o['side']=='BUY')
        self.exchange.fill(buy['client_order_id'])
        self.set_signal('DOWNTREND'); self.bot.run_cycle()
        exits=[o for o in self.bot.database.fetch_active_grids()
               if o['side']=='SELL' and o['parent_order_id']==buy['order_id']]
        self.assertEqual(len(exits),1)
        self.bot.run_cycle()
        self.assertEqual(len([o for o in self.bot.database.fetch_active_grids()
                              if o['parent_order_id']==buy['order_id']]),1)

    def test_network_failure_in_filter_never_disables_hard_stop(self):
        self.working_grid()
        self.monitor.reset_mock()
        self.monitor.buys_allowed.side_effect=TimeoutError('offline')
        self.exchange.price=D(69)
        self.bot.run_cycle()
        self.assertEqual(self.bot.database.get_state(SAFETY_MODE_KEY),LIQUIDATED)
        self.assertEqual(self.exchange.market_sell_calls,1)
        self.monitor.buys_allowed.assert_not_called()

    def test_fill_wins_cancel_race_and_receives_exactly_one_sell(self):
        self.working_grid()
        self.exchange.cancel_not_found_once=True
        self.set_signal('DOWNTREND'); self.bot.run_cycle(); self.bot.run_cycle()
        filled=[o for o in self.bot.database.fetch_all_orders()
                if o['side']=='BUY' and o['level']>0 and o['status']=='FILLED']
        self.assertEqual(len(filled),1)
        exits=[o for o in self.bot.database.fetch_all_orders()
               if o['side']=='SELL' and o['parent_order_id']==filled[0]['order_id']]
        self.assertEqual(len(exits),1)

    def test_cancel_response_timeout_is_reconciled_without_reopening_buys(self):
        self.working_grid()
        self.exchange.cancel_timeout='after'
        self.set_signal('DOWNTREND')
        self.bot.run_cycle(); self.bot.run_cycle()
        self.assertTrue(all(o['side']=='SELL' for o in self.bot.database.fetch_active_grids()))
        self.set_signal('RANGE'); self.bot.run_cycle()
        self.assertEqual(len([o for o in self.bot.database.fetch_active_grids()
                              if o['side']=='BUY']),len(self.bot.levels))

    def test_sellable_partial_fill_gets_an_exit_during_regime_pause(self):
        self.working_grid()
        buy=next(o for o in self.bot.database.fetch_active_grids() if o['side']=='BUY')
        order=self.exchange.orders[buy['client_order_id']]
        partial=(D(order['amount'])*D('.30')).quantize(D('.001'))
        order.update(filled=str(partial),cost=str(partial*D(order['price'])))
        self.exchange.base_free+=partial
        self.set_signal('DOWNTREND'); self.bot.run_cycle(); self.bot.run_cycle()
        exits=[o for o in self.bot.database.fetch_active_grids()
               if o['side']=='SELL' and o['parent_order_id']==buy['order_id']]
        self.assertEqual(len(exits),1)
        self.assertGreater(D(exits[0]['amount']),0)
        self.assertLessEqual(D(exits[0]['amount']),partial)  # Existing fee reserve may reduce it.

    def test_queued_reanchor_does_not_cancel_existing_sells_when_blocked(self):
        self.working_grid()
        sells={o['order_id'] for o in self.bot.database.fetch_active_grids() if o['side']=='SELL'}
        self.bot.request_manual_recenter('100','20','30','1000',8)
        self.set_signal('DOWNTREND')
        self.assertFalse(self.bot.reset_grid())
        self.assertTrue(sells.issubset({o['order_id'] for o in self.bot.database.fetch_active_grids()}))

    def test_filter_failure_and_restart_preserve_cancellation_intents(self):
        self.working_grid()
        self.set_signal('DATA_UNAVAILABLE'); self.bot.run_cycle()
        recovered=OfflineGridBot(self.bot.config,self.exchange,GridDatabase(self.path),
                                 regime_monitor=self.monitor)
        recovered.prepare(persist=False)
        recovered.run_cycle()
        self.set_signal('RANGE'); recovered.run_cycle()
        self.assertTrue(any(o['side']=='BUY' for o in recovered.database.fetch_active_grids()))

    def test_status_is_read_only_and_does_not_launch_fetches(self):
        self.bot.regime_status()
        self.monitor.buys_allowed.assert_not_called()
        self.monitor.status.assert_called_once()


if __name__=='__main__':
    unittest.main()
