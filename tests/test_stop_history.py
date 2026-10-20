"""Between-poll wick, crash durability and locked-inventory risk regressions."""
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

import ccxt
import engine
from database import GridDatabase
from settings import ENGINE_HALTED, ENGINE_STATUS_KEY, LIQUIDATED, LIQUIDATION_KEY, SAFETY_MODE_KEY
from stop_history import ALARM_KEY, BREACH_KEY, CURSOR_KEY, StopHistoryPending, StopTradeHistory
from tests.fakes import make_bot


def trade(identity, price='100'):
    return {'a': identity, 'T': 1000 + identity, 'p': price}


class StopHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'grid_testnet.sqlite'
        self.database = GridDatabase(self.path)
        self.rows = [trade(1)]
        self.calls = []
        self.monitor = StopTradeHistory(self.database, self.fetch, 'BTCUSDT')
        self.monitor.initialize()

    def fetch(self, params):
        self.assertFalse(self.database.in_transaction)
        self.calls.append(params)
        if 'fromId' not in params:
            return self.rows[-1:]
        return [row for row in self.rows if row['a'] >= params['fromId']][:params['limit']]

    def test_rebound_cannot_hide_crossing_and_restart_retains_breach(self):
        self.rows += [trade(2, '69'), trade(3, '105')]
        self.assertEqual(self.monitor.scan(D('70')), D('69'))
        self.assertEqual(json.loads(self.database.get_state(BREACH_KEY))['id'], 2)
        restarted = StopTradeHistory(GridDatabase(self.path), lambda _: self.fail('latched exit needs no HTTP'), 'BTCUSDT')
        self.assertEqual(restarted.scan(D('70')), D('69'))

    def test_equal_to_floor_does_not_trigger(self):
        self.rows += [trade(2, '70')]
        self.assertIsNone(self.monitor.scan(D('70')))
        self.assertIsNone(self.database.get_state(BREACH_KEY))

    def test_newly_raised_floor_does_not_trigger_on_earlier_trade(self):
        self.assertIsNone(self.monitor.scan(D('70')))
        self.rows += [trade(2, '75')]
        self.assertIsNone(self.monitor.scan(D('80')))
        self.rows += [trade(3, '75')]
        self.assertEqual(self.monitor.scan(D('80')), D('75'))

    def test_first_observed_trade_below_floor_is_not_discarded(self):
        self.database.clear_state(CURSOR_KEY)
        self.rows += [trade(2, '65')]
        self.assertEqual(self.monitor.scan(D('70')), D('65'))

    def test_pagination_checks_all_ids_including_same_millisecond(self):
        self.rows += [dict(trade(i), T=1002) for i in range(2, 2100)]
        self.rows[1500]['p'] = '60'
        self.assertEqual(self.monitor.scan(D('70')), D('60'))
        self.assertEqual([p['fromId'] for p in self.calls if 'fromId' in p], [2, 1002])

    def test_bounded_catchup_progress_survives_and_never_skips_to_tail(self):
        self.rows += [trade(i) for i in range(2, 2005)]
        with self.assertRaises(StopHistoryPending):
            self.monitor.scan(D('70'), max_pages=1)
        self.assertEqual(json.loads(self.database.get_state(CURSOR_KEY))['id'], 1001)
        self.assertIsNotNone(self.database.get_state(ALARM_KEY))
        self.rows[-2]['p'] = '65'
        self.assertEqual(self.monitor.scan(D('70')), D('65'))

    def test_gap_reset_or_bad_payload_never_advances_cursor(self):
        for rows in ([trade(3)], [trade(0)], [{'a': 2, 'T': 1002, 'p': 'NaN'}]):
            with self.subTest(rows=rows):
                self.rows = rows
                with self.assertRaises(StopHistoryPending):
                    self.monitor.scan(D('70'))
                self.assertEqual(json.loads(self.database.get_state(CURSOR_KEY))['id'], 1)

    def test_network_failure_keeps_cursor_for_retry(self):
        self.monitor.fetch = lambda _: (_ for _ in ()).throw(ccxt.NetworkError('offline'))
        with self.assertRaises(ccxt.NetworkError):
            self.monitor.scan(D('70'))
        self.assertEqual(json.loads(self.database.get_state(CURSOR_KEY))['id'], 1)

    def test_flat_acknowledgment_and_reviewed_start_clear_latch(self):
        self.rows += [trade(2, '60')]
        self.monitor.scan(D('70'))
        self.monitor.acknowledge_flat()
        self.assertIsNone(self.monitor.scan(D('70')))
        self.rows += [trade(3, '60')]
        self.monitor.scan(D('70'))
        self.rows += [trade(4)]
        self.monitor.initialize(reset=True)
        self.assertIsNone(self.database.get_state(BREACH_KEY))
        self.assertEqual(json.loads(self.database.get_state(CURSOR_KEY))['id'], 4)

    def test_rebounded_wick_cancels_locked_sells_liquidates_only_bot_once(self):
        bot, exchange = make_bot(self.path)
        with patch.object(engine, 'schedule_telegram_alert', return_value=False):
            bot.run_cycle()
            bot.run_cycle()
            self.assertTrue(any(row['side'] == 'SELL' for row in bot.database.fetch_active_grids()))
            self.assertGreater(bot._bot_btc_balance(), bot._free_bot_base())
            bot._stop_history = self.monitor
            self.rows += [trade(2, '69'), trade(3, '100')]
            bot.run_cycle()
            self.assertEqual(exchange.market_sell_calls, 1)
            self.assertEqual(exchange.base_free, D('1'))  # Reserved wallet BTC untouched.
            self.assertEqual(bot.database.get_state(SAFETY_MODE_KEY), LIQUIDATED)
            self.assertEqual(bot.database.get_state(ENGINE_STATUS_KEY), ENGINE_HALTED)
            self.assertEqual(json.loads(bot.database.get_state(LIQUIDATION_KEY))['trigger_price'], '69')
            bot.run_cycle()
            self.assertEqual(exchange.market_sell_calls, 1)
            self.assertFalse(bot.database.fetch_active_grids())

    def test_unverified_history_blocks_lane_replacement_without_touching_sell(self):
        bot, exchange = make_bot(self.path)
        with patch.object(engine, 'schedule_telegram_alert', return_value=False):
            bot.run_cycle()
            bot.run_cycle()
            buy = next(r for r in bot.database.fetch_active_grids() if r['side'] == 'BUY')
            exchange.fill(buy['client_order_id'])
            before = len(exchange.orders)
            bot._stop_history = self.monitor
            self.rows += [trade(3)]  # ID 2 missing.
            with self.assertRaises(StopHistoryPending):
                bot.run_cycle()
            self.assertEqual(len(exchange.orders), before)
            self.assertFalse(exchange.cancel_calls)
            self.assertEqual(exchange.market_sell_calls, 0)


if __name__ == '__main__':
    unittest.main()
