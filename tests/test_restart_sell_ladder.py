"""STOP/restart must retain the reviewed grid instead of stacking SELLs."""
import asyncio
import json
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import engine
from database import GridDatabase
from engine import _reconcile_service_boot
from models import GridConfig, TradingHalt
from portfolio import _portfolio_wallet
from settings import ENGINE_IDLE, ENGINE_RUNNING, ENGINE_STATUS_KEY, TRAILING_STOP_KEY
from tests.fakes import OfflineGridBot as GridBot
from tests.test_wallet_baseline_start import BitcoinWalletExchange


class RestartSellLadderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'grid_testnet.sqlite'
        self.exchange = BitcoinWalletExchange()
        self.exchange.price = D('81693')
        self.config = GridConfig('BTC/USDT', D('500'), D('77144.23'),
                                 D('86241.77'), D('2.5'), D('50'),
                                 D('65572.59'), 2, buy_grid_levels=4,
                                 sell_grid_levels=4)
        self.bot = GridBot(self.config, self.exchange, GridDatabase(self.path))
        self.bot.ready_mode = True
        self.bot.prepare(persist=True)
        self.bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_RUNNING)
        alerts = patch.object(engine, 'schedule_telegram_alert', return_value=False)
        alerts.start()
        self.addCleanup(alerts.stop)
        self.bot.run_cycle()
        self.bot.run_cycle()
        self.original_sells = self.sells(self.bot)

    @staticmethod
    def sells(bot):
        return {row['level']: D(row['price']) for row in bot.database.fetch_active_grids()
                if row['side'] == 'SELL'}

    def reopen(self):
        bot = GridBot(self.bot.config, self.exchange, GridDatabase(self.path))
        bot.ready_mode = True
        bot.prepare(persist=False)
        return bot

    def test_stop_start_retains_exact_sell_prices_anchor_risk_and_inventory(self):
        held = _portfolio_wallet(self.bot.database)['btc_held']
        cost = self.bot._carry_inventory(require_available=False)[1]
        risk = self.bot.database.get_state(TRAILING_STOP_KEY)
        anchor = self.bot.anchor
        self.bot.stop_engine()
        old_ids = set(self.exchange.orders)
        self.exchange.price = D('81599.63')
        recovered = self.reopen()
        recovered.start_engine(auto_cover_inventory=True)
        self.assertTrue(recovered.reset_grid())
        self.assertEqual(self.sells(recovered), self.original_sells)
        self.assertEqual(recovered.anchor, anchor)
        self.assertEqual(recovered.database.get_state(TRAILING_STOP_KEY), risk)
        self.assertEqual(_portfolio_wallet(recovered.database)['btc_held'], held)
        self.assertEqual(recovered._carry_inventory(require_available=False)[1], cost)
        self.assertEqual(recovered.baseline_base, D('1'))
        placed = [order for key, order in self.exchange.orders.items() if key not in old_ids]
        self.assertEqual([order['side'] for order in placed], ['sell'] * 4 + ['buy'] * 4)
        self.assertTrue(all(order['type'] == 'limit' for order in placed))
        self.assertEqual(len(set(self.sells(recovered).values())), 4)

    def test_ready_boot_then_cold_macro_restart_retains_ladder(self):
        recovered = self.reopen()
        asyncio.run(_reconcile_service_boot(recovered, ready=True))
        self.exchange.price = D('81599.63')
        recovered.start_engine(auto_cover_inventory=True)
        recovered._macro_trend_provider = lambda: False
        self.assertFalse(recovered.reset_grid())
        self.assertEqual(self.exchange.fetch_open_orders('BTC/USDT'), [])
        recovered._macro_trend_provider = lambda: True
        self.assertTrue(recovered.reset_grid())
        self.assertEqual(self.sells(recovered), self.original_sells)

    def test_carried_sell_fill_is_accounted_once_then_rebuys_at_saved_level(self):
        self.bot.stop_engine()
        self.exchange.price = D('81599.63')
        self.bot.start_engine(auto_cover_inventory=True)
        self.bot.reset_grid()
        sell = next(row for row in self.bot.database.fetch_active_grids() if row['level'] == -1)
        self.exchange.fill(sell['client_order_id'])
        self.bot.run_cycle()
        row = self.bot.database.fetch_latest_orders_by_level()[-1]
        self.assertEqual((row['side'], D(row['price'])), ('BUY', D('81693')))
        trade = self.bot.database.get_trade_by_sell_order_id(sell['order_id'])
        self.assertIsNotNone(trade)
        self.assertGreater(D(str(trade['profit'])), 0)
        count = len(self.bot.database.fetch_trade_history())
        self.bot.run_cycle()
        self.assertEqual(len(self.bot.database.fetch_trade_history()), count)

    def test_unprofitable_carried_ladder_rejects_start_before_any_order_write(self):
        self.bot.stop_engine()
        amount, _ = self.bot._carry_inventory()
        old_ids = set(self.exchange.orders)
        # The target is above the raw cost but below cost plus the SELL fee.
        with patch.object(self.bot, '_carry_inventory', return_value=(amount, amount * D('82800'))):
            with self.assertRaisesRegex(TradingHalt, 'cost.*fee'):
                self.bot.start_engine(auto_cover_inventory=True)
        self.assertEqual(self.bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
        self.assertEqual(set(self.exchange.orders), old_ids)
        self.assertIsNone(self.bot.database.get_state('grid_reset'))

    def test_hard_stop_after_recovery_only_sells_bot_inventory(self):
        self.bot.stop_engine()
        self.bot.start_engine(auto_cover_inventory=True)
        self.bot.reset_grid()
        self.exchange.price = self.bot.config.stop_loss_price - 1
        self.bot.run_cycle()
        self.assertEqual(self.exchange.market_sell_calls, 1)
        self.assertEqual(self.exchange.base_free, D('1'))


if __name__ == '__main__':
    unittest.main()
