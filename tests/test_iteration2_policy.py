"""Iteration 2 deployment gates, fixed risk floor and manual-only recovery."""
import asyncio
from dataclasses import replace
import json
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import ccxt
import engine
from backtest_grid import Config
from database import GridDatabase
from tests.fakes import OfflineGridBot as GridBot
from engine import _apply_active_grid_config
from exchange_handler import create_macro_exchange
from models import GridConfig, TradingHalt
from notifier import TelegramNotifier
from settings import ENGINE_IDLE, ENGINE_RUNNING, ENGINE_STATUS_KEY, SAFETY_MODE_KEY, LIQUIDATED
from strategy_policy import catastrophic_stop, closed_daily_sma, DAILY_MS
from tests.fakes import FakeSpotExchange, make_bot

NOW = 1792152000000 // DAILY_MS * DAILY_MS


def candles(close=100, days=200):
    return [[NOW - i * DAILY_MS, close, close, close, close, 1]
            for i in range(days, 0, -1)]


class Iteration2PolicyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'grid_testnet.sqlite'

    def bot(self):
        return make_bot(self.path, initial_base=D(0))

    def test_default_research_policy_is_fixed_daily200_manual_recovery(self):
        config = Config()
        self.assertEqual((config.stop_mode, config.trend_timeframe, config.trend_period),
                         ('fixed', '1d', 200))
        self.assertEqual((config.stop_gap, config.min_grid_return), (D('.15'), D('.008')))
        self.assertFalse(config.restart_after_stop)

    def test_stop_rounding_uses_tick_and_never_shrinks_buffer(self):
        for tick in (D('.01'), D('.05'), D('.00000001')):
            stop = catastrophic_stop(D('107269.52'), tick)
            self.assertEqual(stop % tick, 0)
            self.assertLessEqual(stop, D('107269.52') * D('.85'))
            self.assertLess(D('107269.52') * D('.85') - stop, tick)

    def test_daily_signal_ignores_forming_and_future_days(self):
        data = candles() + [[NOW, 10000, 10000, 10000, 10000, 1],
                             [NOW + DAILY_MS, 20000, 20000, 20000, 20000, 1]]
        self.assertEqual(closed_daily_sma(data, NOW + 1000), (D(100), NOW))

    def test_missing_stale_duplicate_and_invalid_daily_data_fail_closed(self):
        for data in (candles(days=199), candles()[:-1], candles() + [candles()[0]],
                     candles()[:-1] + [[NOW - DAILY_MS, 1, 1, 1, 'nan', 1]]):
            with self.subTest(data_length=len(data)), self.assertRaises(ValueError):
                closed_daily_sma(data, NOW)

    def test_production_macro_client_is_public_without_credentials_or_sandbox(self):
        with patch('exchange_handler.ccxt.binance') as factory:
            client = factory.return_value
            with patch('exchange_handler.install_circuit_breaker', return_value=client):
                self.assertIs(create_macro_exchange(), client)
            settings = factory.call_args.args[0]
            self.assertNotIn('apiKey', settings)
            self.assertNotIn('secret', settings)
            client.set_sandbox_mode.assert_not_called()

    def test_strict_gate_uses_closed_data_and_daily_cache(self):
        bot, exchange = self.bot()
        exchange.fetch_ohlcv = Mock(return_value=candles())
        bot._macro_signal_day = None
        with patch.object(engine.time, 'time', return_value=NOW / 1000):
            self.assertFalse(bot._macro_entry_allowed(D(100)))
            self.assertFalse(bot._macro_entry_allowed(D(99)))
            self.assertTrue(bot._macro_entry_allowed(D('100.01')))
            self.assertEqual(exchange.fetch_ohlcv.call_count, 1)
        with patch.object(engine.time, 'time', return_value=(NOW + DAILY_MS) / 1000):
            self.assertFalse(bot._macro_entry_allowed(D(101)))  # Yesterday's cache is stale.
            self.assertIsNone(bot.macro_sma)
            self.assertEqual(exchange.fetch_ohlcv.call_count, 2)

    def test_failed_macro_fetch_blocks_start_without_order_writes(self):
        bot, exchange = self.bot()
        bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_IDLE)
        bot._macro_signal_day = None
        exchange.fetch_ohlcv = Mock(side_effect=ccxt.RequestTimeout('macro timeout'))
        with self.assertRaisesRegex(TradingHalt, 'SMA200 unavailable'):
            bot.start_engine()
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
        self.assertEqual(exchange.orders, {})
        self.assertIsNone(bot.database.get_state('grid_reset'))

    def test_below_macro_gate_defers_breakout_without_cancellation(self):
        bot, exchange = self.bot()
        bot.run_cycle()
        bot.run_cycle()
        exchange.fetch_ohlcv = Mock(return_value=candles(200))
        bot._macro_signal_day = None
        orders = dict(exchange.orders)
        with patch.object(engine.time, 'time', return_value=NOW / 1000):
            bot._request_breakout_reset(D(130))
        self.assertIsNone(bot.database.get_state('grid_reset'))
        self.assertEqual(exchange.cancel_calls, [])
        self.assertEqual(exchange.orders, orders)

    def test_macro_failure_never_disables_an_active_fixed_stop(self):
        bot, exchange = self.bot()
        bot.run_cycle()
        bot.run_cycle()
        exchange.fetch_ohlcv = Mock(side_effect=ccxt.RequestTimeout('offline macro'))
        bot._macro_signal_day = None
        self.assertFalse(bot._macro_entry_allowed(D(130)))
        exchange.price = D(69)
        bot.run_cycle()
        self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)
        self.assertEqual(exchange.market_sell_calls, 1)

    def test_high_water_does_not_move_floor_and_survives_restart(self):
        bot, exchange = self.bot()
        bot.run_cycle()
        floor = bot.config.stop_loss_price
        bot.advance_trailing_stop(D(200))
        self.assertEqual(bot.config.stop_loss_price, floor)
        self.assertEqual(bot.high_water_mark, 200)
        recovered = GridBot(bot.config, exchange, GridDatabase(self.path))
        recovered.prepare(persist=False)
        recovered.advance_trailing_stop(D(300))
        self.assertEqual(recovered.config.stop_loss_price, floor)
        self.assertEqual(recovered.high_water_mark, 300)

    def test_initial_grid_derives_stop_and_saving_does_not_place_orders(self):
        exchange = FakeSpotExchange()
        config = GridConfig('BTC/USDT', D(1000), D(80), D(120), D(10), D(50), D(70), 2)
        bot = GridBot(config, exchange, GridDatabase(self.path))
        bot.ready_mode = True
        bot.request_initial_grid('100', '20', '30', '1000', 8)
        self.assertEqual(bot.config.stop_loss_price, 68)
        self.assertEqual(exchange.orders, {})
        self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)

    def test_legacy_first_run_without_active_config_keeps_its_saved_floor(self):
        bot, _ = self.bot()
        self.assertIsNone(bot.database.get_state('active_grid_config'))
        new_template = replace(bot.config, stop_loss_price=D(68))
        restored = _apply_active_grid_config(new_template, bot.database)
        self.assertEqual(restored.stop_loss_price, 70)
        self.assertEqual(restored.fingerprint(), bot.config.fingerprint())

    def test_percentage_transport_noise_cannot_reject_or_lower_a_retained_floor(self):
        bot, _ = self.bot()
        # Browser computes percentage from a displayed fixed 70 USDT floor.
        bot.request_manual_recenter('100', '20', '30.00000000001', '1000', 8)
        request = json.loads(bot.database.get_state('grid_reset'))
        self.assertEqual(D(request['stop_loss_price']), 70)
        self.assertEqual(bot.config.stop_loss_price, 70)

    def test_liquidation_stays_halted_after_recovery_until_manual_new_grid_and_start(self):
        bot, exchange = self.bot()
        bot.run_cycle()
        bot.run_cycle()
        exchange.price = D(69)
        bot.run_cycle()
        before = exchange.next_id
        exchange.price = D(110)
        bot.run_cycle()
        with self.assertRaises(TradingHalt):
            bot.start_engine()
        self.assertEqual(exchange.next_id, before)
        self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)

    def test_fresh_direct_execution_rejects_sub_fee_grid_before_seed(self):
        exchange = FakeSpotExchange()
        config = GridConfig('BTC/USDT', D(1000), D(99), D(101), D('0.1'), D(50), D(80), 2,
                            buy_grid_levels=4, sell_grid_levels=4)
        bot = GridBot(config, exchange, GridDatabase(self.path))
        with self.assertRaisesRegex(ValueError, 'Fee defense'):
            asyncio.run(bot.run(TelegramNotifier()))
        self.assertEqual(exchange.orders, {})


if __name__ == '__main__':
    unittest.main()
