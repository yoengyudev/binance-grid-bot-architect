"""A reviewed grid trades its allocation without adopting pre-existing BTC."""
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from unittest.mock import patch

import engine
from database import GridDatabase
from engine import GridBot
from models import GridConfig, TradingHalt
from settings import ENGINE_IDLE, ENGINE_RUNNING, ENGINE_STATUS_KEY
from tests.fakes import FakeSpotExchange


class BitcoinWalletExchange(FakeSpotExchange):
    def __init__(self):
        super().__init__()
        self.price = Decimal('81260')

    def market(self, symbol):
        market = super().market(symbol)
        market['limits'] = {'amount': {'min': 0.00001}, 'cost': {'min': 5}}
        return market

    def amount_to_precision(self, symbol, amount):
        return str(Decimal(amount).quantize(Decimal('0.00001'), rounding=ROUND_DOWN))


class WalletBaselineStartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'grid_testnet.sqlite'
        self.exchange = BitcoinWalletExchange()
        self.database = GridDatabase(self.path)
        self.database.set_state(ENGINE_STATUS_KEY, ENGINE_IDLE)
        config = GridConfig('BTC/USDT', Decimal('500'), Decimal('70000'),
                            Decimal('90000'), Decimal('2.5'), Decimal('50'),
                            Decimal('65000'), 2)
        self.bot = GridBot(config, self.exchange, self.database)
        self.bot.ready_mode = True
        alerts = patch.object(engine, 'schedule_telegram_alert', return_value=False)
        alerts.start()
        self.addCleanup(alerts.stop)

    def save(self):
        self.bot.request_initial_grid('81260', '6.25', '7', '500', 8)
        return hashlib.sha256(self.database.get_state('grid_reset').encode()).hexdigest()

    def test_saved_500_usdt_eight_level_grid_starts_without_adopting_one_btc(self):
        token = self.save()
        self.assertEqual(self.exchange.orders, {})
        self.assertEqual(self.bot._reconcile_state()['unhedged_btc'], 0)
        result = self.bot.start_engine(auto_cover_inventory=True, pending_grid_token=token)
        self.assertEqual(result['engine_status'], ENGINE_RUNNING)
        self.assertEqual(self.exchange.orders, {})  # START only arms the engine.
        self.bot.reset_grid()
        self.bot.run_cycle()
        self.bot.run_cycle()
        active = self.database.fetch_active_grids()
        self.assertEqual(len(active), 8)
        self.assertEqual(sum(row['side'] == 'SELL' for row in active), 4)
        sell_amount = sum((Decimal(row['amount']) for row in active
                           if row['side'] == 'SELL'), Decimal(0))
        self.assertLess(sell_amount, Decimal('0.004'))
        self.assertGreaterEqual(self.exchange.base_free, Decimal('1'))
        self.assertEqual(self.bot.baseline_base, Decimal('1'))
        # Emergency exit still sells only the bot's new inventory.
        self.exchange.price = Decimal('70000')
        self.bot.run_cycle()
        self.assertEqual(self.exchange.base_free, Decimal('1'))

    def test_restart_restores_exclusion_from_sqlite_before_prepare(self):
        token = self.save()
        restarted = GridBot(self.bot.config, self.exchange, GridDatabase(self.path))
        restarted.ready_mode = True
        self.assertIsNone(restarted.baseline_base)
        result = restarted.start_engine(pending_grid_token=token)
        self.assertEqual(result['engine_status'], ENGINE_RUNNING)
        self.assertEqual(restarted.baseline_base, Decimal('1'))
        self.assertEqual(self.exchange.orders, {})

    def test_completed_liquidation_can_save_and_start_a_new_grid_with_wallet_btc(self):
        self.bot.start_engine(pending_grid_token=self.save())
        self.bot.reset_grid()
        self.bot.run_cycle()
        self.exchange.price = Decimal('70000')
        self.bot.run_cycle()
        self.exchange.price = Decimal('81260')
        trades = self.database.fetch_trade_history()
        self.bot.request_manual_recenter('81260', '6.25', '7', '500', 8)
        self.assertEqual(self.database.fetch_trade_history(), trades)
        self.assertEqual(self.database.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
        token = hashlib.sha256(self.database.get_state('grid_reset').encode()).hexdigest()
        result = self.bot.start_engine(auto_cover_inventory=True, pending_grid_token=token)
        self.assertEqual(result['engine_status'], ENGINE_RUNNING)
        self.assertEqual(self.exchange.base_free, Decimal('1'))

    def test_active_sell_orders_and_reserved_wallet_resume_without_duplicate_orders(self):
        self.bot.start_engine(pending_grid_token=self.save())
        self.bot.reset_grid()
        self.bot.run_cycle()
        self.bot.run_cycle()
        order_ids = set(self.exchange.orders)
        restarted = GridBot(self.bot.config, self.exchange, GridDatabase(self.path))
        restarted.ready_mode = True
        state = restarted._reconcile_state()
        self.assertTrue(state['resumable'])
        self.assertGreater(state['covered_btc'], 0)
        self.assertEqual(state['unhedged_btc'], 0)
        self.database.set_state(ENGINE_STATUS_KEY, ENGINE_IDLE)
        self.assertEqual(restarted.start_engine()['engine_status'], ENGINE_RUNNING)
        restarted.run_cycle()
        self.assertEqual(set(self.exchange.orders), order_ids)

    def test_additional_unexplained_btc_after_save_still_blocks_start(self):
        token = self.save()
        self.exchange.base_free += Decimal('0.01')
        state = self.bot._reconcile_state()
        self.assertEqual(state['unhedged_btc'], Decimal('0.01'))
        with self.assertRaisesRegex(TradingHalt, 'complete bot cost-basis'):
            self.bot.start_engine(auto_cover_inventory=True, pending_grid_token=token)
        self.assertEqual(self.database.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
        self.assertEqual(self.exchange.orders, {})

    def test_known_bot_inventory_still_requires_explicit_sell_protection(self):
        token = self.save()
        self.bot.start_engine(pending_grid_token=token)
        self.bot.reset_grid()
        self.bot.run_cycle()
        self.bot.stop_engine()
        state = self.bot._reconcile_state()
        self.assertGreater(state['unhedged_btc'], 0)
        self.assertLess(state['unhedged_btc'], Decimal('0.004'))
        with self.assertRaisesRegex(TradingHalt, 'Unhedged'):
            self.bot.start_engine()
        self.assertEqual(self.bot.start_engine(auto_cover_inventory=True)['engine_status'],
                         ENGINE_RUNNING)

    def test_baseline_cannot_hide_a_bot_lot_when_wallet_reserve_was_withdrawn(self):
        token = self.save()
        self.bot.start_engine(pending_grid_token=token)
        self.bot.reset_grid()
        self.bot.run_cycle()
        self.bot.stop_engine()
        self.exchange.base_free -= Decimal('0.5')
        self.assertGreater(self.bot._reconcile_state()['unhedged_btc'], 0)
        with self.assertRaises(TradingHalt):
            self.bot.start_engine()

    def test_invalid_baseline_fails_closed_without_placing_orders(self):
        self.save()
        original = json.loads(self.database.get_state('grid_run'))
        for value in (None, '-1', 'NaN', 'Infinity', 'bad'):
            with self.subTest(value=value):
                self.database.set_state('grid_run', json.dumps({**original, 'baseline_base': value}))
                with self.assertRaisesRegex(TradingHalt, 'starting BTC balance'):
                    self.bot._reconcile_state()
                self.assertEqual(self.database.get_state(ENGINE_STATUS_KEY), ENGINE_IDLE)
                self.assertEqual(self.exchange.orders, {})

    def test_without_saved_run_no_wallet_btc_is_silently_excluded(self):
        self.assertEqual(self.bot._reconcile_state()['unhedged_btc'], Decimal('1'))
        self.assertIsNone(self.database.get_state('grid_run'))
        self.assertEqual(self.exchange.orders, {})
