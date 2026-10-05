"""No automatic price exit or grid cancellation when tracked inventory is flat."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import ccxt
import engine
from models import TradingHalt
from settings import (
    BREAKOUT_COOLDOWN_SECONDS, BREAKOUT_TIMER_KEY, ENGINE_HALTED, ENGINE_RUNNING,
    ENGINE_STATUS_KEY, FILL_SNAPSHOT_PREFIX, LIQUIDATED, LIQUIDATION_KEY,
    SAFETY_MODE_KEY, TRAILING_STOP_KEY,
)
from tests.fakes import make_bot


class FlatInventoryRiskTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'grid_testnet.sqlite'
        alerts = patch.object(engine, 'schedule_telegram_alert', return_value=False)
        self.alerts = alerts.start()
        self.addCleanup(alerts.stop)

    def grid(self):
        bot, exchange = make_bot(self.path)
        bot.run_cycle()
        bot.run_cycle()
        bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        return bot, exchange

    def sell_all(self, bot, exchange):
        seed = bot.database.fetch_latest_orders_by_level()[0]
        seed_order = exchange.orders[seed['client_order_id']]
        sells = [row for row in bot.database.fetch_active_grids() if row['side'] == 'SELL']
        # Model the exchange's actual BTC commission, including the reserved BTC,
        # so the final SELL consumes the entire commission-adjusted seed lot.
        fee = Decimal(seed_order['filled']) - sum((Decimal(row['amount']) for row in sells), Decimal(0))
        seed_order['fees'] = [{'currency': 'BTC', 'cost': str(fee)}]
        exchange.base_free -= fee
        bot._save_fill_snapshot(seed['order_id'], seed_order)
        for row in sells:
            exchange.fill(row['client_order_id'])
        return {row['order_id'] for row in bot.database.fetch_active_grids() if row['side'] == 'BUY'}

    def assert_buy_orders_preserved(self, bot, exchange, ids):
        active = {row['order_id'] for row in bot.database.fetch_active_grids() if row['side'] == 'BUY'}
        self.assertTrue(ids.issubset(active))
        self.assertEqual(exchange.cancel_calls, [])
        self.assertEqual(exchange.market_sell_calls, 0)
        self.assertIsNone(bot.database.get_state(LIQUIDATION_KEY))
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_RUNNING)

    def test_final_sell_is_reconciled_before_price_stop_and_breakout_checks(self):
        bot, exchange = self.grid()
        bot.advance_trailing_stop(Decimal('200'))  # Existing raised floor: 170.
        trailing = bot.database.get_state(TRAILING_STOP_KEY)
        buys = self.sell_all(bot, exchange)
        exchange.price = Decimal('150')  # Above the grid but below the old floor.
        bot.run_cycle()
        self.assertEqual(bot._bot_btc_balance(refresh=False), 0)
        self.assertEqual(exchange.base_free, 1)  # Personal BTC is not bot exposure.
        self.assert_buy_orders_preserved(bot, exchange, buys)
        self.assertEqual(bot.database.get_state(TRAILING_STOP_KEY), trailing)
        self.assertTrue(bot.database.fetch_trade_history())

    def test_flat_price_spike_does_not_raise_stop_or_schedule_four_hour_reset(self):
        bot, exchange = self.grid()
        buys = self.sell_all(bot, exchange)
        trailing = bot.database.get_state(TRAILING_STOP_KEY)
        for seconds, price in ((0, '200'), (BREAKOUT_COOLDOWN_SECONDS + 60, '210'),
                               (BREAKOUT_COOLDOWN_SECONDS + 120, '150')):
            exchange.price = Decimal(price)
            with patch.object(engine.time, 'time', return_value=seconds):
                bot.run_cycle()
        bot.advance_trailing_stop(Decimal('250'))
        self.assertFalse(bot._observe_upper_breakout(Decimal('250'), now=seconds + 60))
        bot.liquidate(Decimal('60'))  # Automatic entry point must also skip a flat lot.
        self.assert_buy_orders_preserved(bot, exchange, buys)
        self.assertEqual(bot.database.get_state(TRAILING_STOP_KEY), trailing)
        self.assertIsNone(bot.database.get_state('grid_reset'))
        self.assertIsNone(bot.database.get_state(BREAKOUT_TIMER_KEY))

    def test_reentry_keeps_existing_buys_and_resumes_risk_after_a_fill(self):
        bot, exchange = self.grid()
        buys = self.sell_all(bot, exchange)
        exchange.price = Decimal('121')
        bot.run_cycle()
        exchange.price = Decimal('100')
        bot.run_cycle()
        self.assert_buy_orders_preserved(bot, exchange, buys)
        buy = bot.database.fetch_latest_orders_by_level()[-1]
        self.assertEqual(buy['side'], 'BUY')
        exchange.fill(buy['client_order_id'])
        exchange.price = Decimal('105')
        bot.run_cycle()
        self.assertGreater(bot._bot_btc_balance(refresh=False), 0)
        self.assertEqual(bot.config.stop_loss_price, Decimal('75'))
        self.assertEqual(bot.database.fetch_latest_orders_by_level()[-1]['side'], 'SELL')

    def test_partial_buy_reactivates_the_existing_hard_stop(self):
        bot, exchange = self.grid()
        bot.advance_trailing_stop(Decimal('200'))
        self.sell_all(bot, exchange)
        exchange.price = Decimal('150')
        bot.run_cycle()
        buy = bot.database.fetch_latest_orders_by_level()[1]
        exchange.orders[buy['client_order_id']].update(filled='1', cost=str(Decimal(buy['price'])))
        exchange.base_free += 1
        bot.run_cycle()
        self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_HALTED)
        self.assertEqual(exchange.market_sell_calls, 1)
        self.assertEqual(exchange.base_free, 1)

    def test_locked_inventory_and_small_real_residuals_remain_protected(self):
        bot, exchange = self.grid()
        self.assertGreater(bot._bot_btc_balance(), 0)
        self.assertLess(bot._free_bot_base(), bot._bot_btc_balance())
        # With zero exchange commissions, SELL sizing reserves are still real BTC.
        for row in bot.database.fetch_active_grids():
            if row['side'] == 'SELL':
                exchange.fill(row['client_order_id'])
        self.assertGreater(bot._bot_btc_balance(), 0)
        bot.advance_trailing_stop(Decimal('110'))
        self.assertEqual(bot.config.stop_loss_price, Decimal('80'))

    def test_unexpected_cancel_does_not_interrupt_an_exposed_hard_stop(self):
        bot, exchange = self.grid()
        buy = next(row for row in bot.database.fetch_active_grids() if row['side'] == 'BUY')
        exchange.cancel_order(exchange.orders[buy['client_order_id']]['id'], bot.config.symbol)
        exchange.price = Decimal('69')
        bot.run_cycle()
        self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_HALTED)
        self.assertEqual(exchange.market_sell_calls, 1)
        self.assertEqual(exchange.base_free, 1)

    def test_breakout_queued_before_final_sell_is_withdrawn_without_cancels(self):
        bot, exchange = self.grid()
        exchange.price = Decimal('121')
        bot._request_breakout_reset(exchange.price)
        buys = self.sell_all(bot, exchange)
        self.assertFalse(bot.reset_grid())
        self.assert_buy_orders_preserved(bot, exchange, buys)
        self.assertIsNone(bot.database.get_state('grid_reset'))

    def test_unknown_or_unreadable_exposure_cannot_be_treated_as_flat(self):
        bot, exchange = self.grid()
        with patch.object(exchange, 'fetch_order', side_effect=ccxt.RequestTimeout('unknown fills')):
            with self.assertRaises(ccxt.RequestTimeout):
                bot.advance_trailing_stop(Decimal('200'))
        seed = bot.database.fetch_latest_orders_by_level()[0]
        bot.database.clear_state(FILL_SNAPSHOT_PREFIX + seed['order_id'])
        with self.assertRaisesRegex(TradingHalt, 'cannot be verified'):
            bot._bot_btc_balance(refresh=False)

    def test_manual_panic_still_halts_a_verified_flat_grid(self):
        bot, exchange = self.grid()
        self.sell_all(bot, exchange)
        exchange.price = Decimal('121')
        bot.run_cycle()
        result = bot.force_liquidate()
        self.assertTrue(result['completed'])
        self.assertEqual(result['engine_status'], ENGINE_HALTED)
        self.assertEqual(result['mode'], LIQUIDATED)
        self.assertEqual(exchange.market_sell_calls, 0)
