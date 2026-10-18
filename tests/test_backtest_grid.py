"""Backtest accounting, causality, precision, stop and execution-path checks."""
from contextlib import redirect_stdout
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from backtest_grid import (Config, GridBacktest, atr_bounds, atr_for_next_open,
                           fetch_candles, geometric_levels, main, read_candles,
                           split_amount, validate_candles, prepare_indicators)

D = Decimal
START = pd.Timestamp('2025-01-02T00:00:00Z')


def LegacyConfig(**kwargs):
    # Preserve the former 1h SMA50 / trailing-buffer policy in regression fixtures.
    return Config(trend_timeframe='1h', trend_period=50, stop_gap=D('0.02'),
                  stop_mode='trailing', buy_trend_policy='legacy', allocation_policy='legacy_cash', **kwargs)


def candles(bars):
    """50 one-point-range macro warmup candles, then supplied hourly legacy bars."""
    rows = [[100, 100.5, 99.5, 100, 1000]] * 50 + bars
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close', 'volume'],
                        index=pd.date_range(START - pd.Timedelta(hours=50), periods=len(rows), freq='h'))


def fine_candles(bars, timeframe='5m', macro='1h'):
    step = pd.Timedelta(minutes=5 if timeframe == '5m' else 1)
    warmup = pd.Timedelta(hours=50 if macro == '1h' else 200)
    count = warmup // step
    rows = [[100, 100.5, 99.5, 100, 1000]] * count + bars
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close', 'volume'],
                        index=pd.date_range(START - warmup, periods=len(rows), freq=step))


class BacktestGridTests(unittest.TestCase):
    def config(self, **kwargs):
        return LegacyConfig(slippage_bps=D(0), execution_timeframe='1h', trend_filter=False,
                      cooldown_hours=D(0), min_grid_return=D(0), **kwargs)

    def test_frontend_buffer_and_geometric_lane_endpoints(self):
        lower, upper, stop = atr_bounds(D('80000'), D('20'), self.config())
        self.assertEqual((lower, upper, stop), (D('79960'), D('80040'), D('78360.80')))
        lower, upper, stop = atr_bounds(D('80000'), D('3000'), self.config())
        self.assertEqual(stop, D('71000'))
        self.assertEqual(geometric_levels(D('80000'), lower, 4, D('.01'))[-1], lower)
        self.assertEqual(geometric_levels(D('80000'), upper, 4, D('.01'))[-1], upper)
        self.assertEqual(split_amount(D('.00029'), 2, D('.00001')), [D('.00014'), D('.00015')])

    def test_macro_features_publish_only_at_close_for_1h_and_4h(self):
        for macro, bars_per_macro in [('1h', 12), ('4h', 48)]:
            frame = fine_candles([[100, 101, 99, 100, 1]] * (bars_per_macro + 1), macro=macro)
            changed = frame.copy()
            # Mutation inside a forming macro candle must not leak backward.
            finish = START + pd.Timedelta(hours=1 if macro == '1h' else 4)
            changed.loc[START:finish - pd.Timedelta(minutes=5), ['high', 'low', 'close']] = [1000, 1, 500]
            a = prepare_indicators(frame, '5m', macro)
            b = prepare_indicators(changed, '5m', macro)
            pd.testing.assert_frame_equal(a.loc[START:finish - pd.Timedelta(minutes=5)],
                                          b.loc[START:finish - pd.Timedelta(minutes=5)])
            self.assertEqual(a.loc[START, 'macro_sma'], 100)
            self.assertNotEqual(a.loc[finish, 'atr'], b.loc[finish, 'atr'])
            self.assertNotEqual(a.loc[finish, 'macro_sma'], b.loc[finish, 'macro_sma'])

    def test_one_minute_execution_uses_same_closed_hourly_features(self):
        frame = fine_candles([[101, 101, 101, 101, 1]] * 61, timeframe='1m')
        signals = prepare_indicators(frame, '1m', '1h')
        self.assertEqual(signals.loc[START + pd.Timedelta(minutes=59), 'macro_sma'], 100)
        self.assertEqual(signals.loc[START + pd.Timedelta(hours=1), 'macro_sma'], 100.02)
        bot = GridBacktest(LegacyConfig(execution_timeframe='1m'))
        result = bot.run(frame, START, START + pd.Timedelta(minutes=61))
        self.assertEqual(result['candles'], 61)
        self.assertEqual(result['execution_timeframe'], '1m')

    def test_initial_entry_requires_strictly_above_sma_not_equal(self):
        frame = fine_candles([[100, 100, 100, 100, 1], [101, 101, 101, 101, 1]])
        bot = GridBacktest(LegacyConfig())
        result = bot.run(frame, START, START + pd.Timedelta(minutes=10))
        starts = [e for e in bot.events if e['event'] == 'GRID_STARTED']
        self.assertEqual(len(starts), 1)
        self.assertEqual(pd.Timestamp(starts[0]['timestamp']), START + pd.Timedelta(minutes=5))
        self.assertEqual(result['entry_trend_blocked_bars'], 1)
        self.assertGreater(D(starts[0]['center']), D(starts[0]['macro_sma']))

    def test_stop_restart_requires_elapsed_cooldown_and_macro_trend(self):
        bot = GridBacktest(LegacyConfig(restart_after_stop=True, cooldown_hours=D(8)))
        bot.start_grid(START, D(100), D(1))
        stopped_at = START + pd.Timedelta(minutes=2)
        bot._hard_stop(D(90), stopped_at)
        bot.macro_sma = D(95)
        bot._restart(stopped_at + pd.Timedelta(hours=8) - pd.Timedelta(seconds=1), D(100), D(1))
        self.assertEqual(len([f for f in bot.fills if f['kind'] == 'SEED']), 1)
        bot.macro_sma = D(101)
        bot._restart(stopped_at + pd.Timedelta(hours=8), D(100), D(1))
        self.assertEqual(len([f for f in bot.fills if f['kind'] == 'SEED']), 1)
        bot.macro_sma = D(100)  # Equality is still blocked after cooldown expires.
        bot._restart(stopped_at + pd.Timedelta(hours=9), D(100), D(1))
        self.assertEqual(len([f for f in bot.fills if f['kind'] == 'SEED']), 1)
        bot.macro_sma = D(99)
        bot._restart(stopped_at + pd.Timedelta(hours=9, minutes=5), D(100), D(1))
        self.assertEqual(len([f for f in bot.fills if f['kind'] == 'SEED']), 2)
        self.assertEqual(bot.cooldown_blocked_bars, 1)
        self.assertEqual(bot.entry_trend_blocked_bars, 2)
        bot.mark(stopped_at + pd.Timedelta(hours=9, minutes=5), D(100))

    def test_five_minute_run_cannot_restart_within_same_hour_or_before_cooldown(self):
        frame = fine_candles([[101, 101, 90, 100, 1]] + [[101, 101.2, 100.8, 101, 1]] * 110)
        bot = GridBacktest(LegacyConfig(restart_after_stop=True, cooldown_hours=D(8)))
        report = bot.run(frame, START, START + pd.Timedelta(minutes=5 * 111))
        starts = [e for e in bot.events if e['event'] == 'GRID_STARTED']
        stops = [e for e in bot.events if e['event'] == 'HARD_STOP']
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(stops), 1)
        self.assertGreaterEqual(pd.Timestamp(starts[1]['timestamp']),
                                pd.Timestamp(stops[0]['timestamp']) + pd.Timedelta(hours=8))
        self.assertGreater(report['cooldown_blocked_bars'], 90)

    def test_reanchor_trend_gate_preserves_orders_inventory_and_risk(self):
        bot = GridBacktest(LegacyConfig())
        bot.start_grid(START, D(100), D(1))
        bot.macro_sma = D(106)
        before_inventory = bot.held
        bot._point(D(105), START)
        before_orders = dict(bot.orders)  # Risk may already cancel BUYs below the raised stop.
        bot._point(D(105), START + pd.Timedelta(hours=4))
        self.assertEqual(bot.breakout_count, 0)
        self.assertEqual(bot.orders, before_orders)
        self.assertEqual(bot.held, before_inventory)
        self.assertGreater(bot.stop, D('96.04'))
        self.assertEqual(bot.reanchor_trend_blocks, 1)
        bot.macro_sma = D(104)
        bot._point(D(105), START + pd.Timedelta(hours=4, minutes=5))
        self.assertEqual(bot.breakout_count, 1)

    def test_missing_macro_warmup_is_rejected_without_silent_skipping(self):
        frame = fine_candles([[101, 101, 101, 101, 1]])
        with self.assertRaisesRegex(ValueError, 'Insufficient closed macro history'):
            GridBacktest(LegacyConfig()).run(frame.iloc[12:], START, START + pd.Timedelta(minutes=5))

    def test_five_minute_pagination_uses_five_minute_cursor_and_no_keys(self):
        frame = fine_candles([[100, 101, 99, 100, 1]]).iloc[-20:]
        rows = [[int(at.timestamp() * 1000), *row] for at, row in zip(frame.index, frame.values.tolist())]
        requests = []
        class PublicExchange:
            def fetch_ohlcv(self, symbol, timeframe, since, limit, params):
                requests.append((timeframe, since))
                return [row for row in rows if since <= row[0] <= params['endTime']][:7]
        with patch('backtest_grid.ccxt.binance', return_value=PublicExchange()):
            actual = fetch_candles(frame.index[0], frame.index[-1] + pd.Timedelta(minutes=5), '5m')
        self.assertEqual(len(actual), 20)
        self.assertTrue(all(tf == '5m' for tf, _ in requests))
        self.assertEqual(requests[1][1] - requests[0][1], 7 * 5 * 60 * 1000)

    def test_atr_matches_local_pandas_ta_closed_fifteen_candle_windows(self):
        import pandas_ta
        frame = candles([[100, 105, 98, 103, 1000], [103, 108, 101, 106, 1000]])
        actual = atr_for_next_open(frame)
        for i in (15, 16):
            previous = frame.iloc[i - 15:i]
            expected = pandas_ta.atr(previous.high, previous.low, previous.close,
                                     length=14, talib=False).iloc[-1]
            self.assertAlmostEqual(actual.iloc[i], expected, places=12)

    def test_atr_does_not_see_current_or_future_candle(self):
        original = candles([[100, 105, 98, 103, 1000], [103, 108, 101, 106, 1000]])
        changed = original.copy()
        changed.loc[START:, ['high', 'low', 'close']] = [1000, 1, 500]
        self.assertEqual(atr_for_next_open(original).loc[START], atr_for_next_open(changed).loc[START])
        self.assertTrue(atr_for_next_open(original).iloc[:15].isna().all())

    def test_price_levels_match_production_engine_for_even_and_odd_counts(self):
        from engine import configured_grid_levels
        from models import GridConfig
        from decimal import ROUND_HALF_UP
        for count in (8, 9):
            lower, upper, stop = atr_bounds(D('81260'), D('230'), self.config())
            nb, ns = (count + 1) // 2, count // 2
            live = GridConfig('BTC/USDT', D(500), lower, upper, D('2.5'), D(50), stop, 2,
                              buy_grid_levels=nb, sell_grid_levels=ns)
            buys, sells = configured_grid_levels(D('81260'), live)
            self.assertEqual(geometric_levels(D('81260'), lower, nb, D('.01')),
                             [p.quantize(D('.01'), rounding=ROUND_HALF_UP) for p in buys])
            self.assertEqual(geometric_levels(D('81260'), upper, ns, D('.01')),
                             [p.quantize(D('.01'), rounding=ROUND_HALF_UP) for p in sells])

    def test_seed_sizes_budget_and_records_actual_base_fee_and_cost(self):
        bot = GridBacktest(self.config())
        bot.start_grid(START, D(100), D(1))
        self.assertEqual(bot.cash, D(250))
        self.assertEqual(bot.held, D('2.4975'))
        self.assertEqual(sum(lot.cost for lot in bot.lots.values()), D(250))
        self.assertEqual(len(bot.orders), 8)
        self.assertEqual(sum(o.side == 'BUY' for o in bot.orders.values()), 4)
        self.assertEqual(sum(o.quantity for o in bot.orders.values() if o.side == 'SELL'), D('2.49750'))
        self.assertGreaterEqual(bot.free_quote(), 0)
        bot.mark(START, D(100))
        self.assertEqual(bot.curve[-1]['equity'], 499.75)

    def test_sell_parent_cost_and_both_fees_are_charged_once(self):
        bot = GridBacktest(self.config())
        bot.start_grid(START, D(100), D(1))
        buy = bot.orders[1]
        bot._fill(buy, START)
        lot = bot.lots[bot.lanes[1].lot_id]
        original_quantity, original_cost = lot.quantity, lot.cost
        bot._maintain(buy.price, START)
        sell = bot.orders[1]
        expected = sell.quantity * sell.price * (1 - bot.c.fee) - original_cost * sell.quantity / original_quantity
        bot._fill(sell, START)
        self.assertEqual(bot.realized, expected)
        self.assertGreater(bot.realized, 0)
        bot.mark(START, sell.price)  # Also asserts the full cash/P&L accounting identity.

    def test_fully_flat_bot_keeps_buy_orders_and_never_advances_or_hits_stop(self):
        bot = GridBacktest(self.config(buy_fee_asset='quote'))
        bot.start_grid(START, D(100), D(1))
        for order in list(bot.orders.values()):
            if order.side == 'SELL':
                bot._fill(order, START)
        self.assertEqual(bot.held, 0)
        buy_orders = {key: o for key, o in bot.orders.items() if o.side == 'BUY'}
        old_stop, old_high = bot.stop, bot.high_water
        bot._point(D(200), START)
        bot._point(D(50), START)
        self.assertEqual((bot.stop, bot.high_water, bot.stop_count), (old_stop, old_high, 0))
        self.assertEqual(bot.orders, buy_orders)

    def test_stop_is_strict_and_a_crash_sale_is_after_fees(self):
        bot = GridBacktest(self.config())
        bot.start_grid(START, D(100), D(1))
        bot._point(bot.stop, START)
        self.assertEqual(bot.stop_count, 0)
        bot._point(bot.stop - D('.01'), START)
        self.assertEqual(bot.stop_count, 1)
        self.assertEqual(bot.orders, {})
        self.assertLess(bot.realized, 0)
        self.assertIn(bot.mode, ('HALTED', 'HALTED_DUST'))

    def test_gap_stop_sells_at_open_not_at_an_unavailable_stop_price(self):
        bot = GridBacktest(LegacyConfig(restart_after_stop=True, slippage_bps=D(100),
                                 execution_timeframe='1h', trend_filter=False, cooldown_hours=D(0)))
        bot.start_grid(START - pd.Timedelta(hours=1), D(100), D(1))
        report = bot.run(candles([[80, 81, 79, 80, 1000]]), START, START + pd.Timedelta(hours=1))
        exits = [f for f in bot.fills if f['kind'] == 'HARD_STOP']
        self.assertEqual(len(exits), 1)
        self.assertEqual(D(exits[0]['price']), D('79.2'))
        self.assertEqual(report['hard_stop_triggers'], 1)
        self.assertEqual(sum(e['event'] == 'GRID_STARTED' for e in bot.events), 1)

    def test_default_stays_halted_and_equity_retains_unsold_dust(self):
        frame = candles([[100, 100, 70, 75, 1000], [75, 80, 60, 70, 1000]])
        bot = GridBacktest(self.config())
        report = bot.run(frame, START, START + pd.Timedelta(hours=2))
        self.assertEqual(report['hard_stop_triggers'], 1)
        self.assertEqual(sum(e['event'] == 'GRID_STARTED' for e in bot.events), 1)
        self.assertLess(report['total_realized_pnl'], 0)
        self.assertGreater(report['max_drawdown_pct'], 0)
        self.assertAlmostEqual(report['ending_equity'] - 500,
                               report['total_realized_pnl'] + report['unrealized_pnl'], places=9)

    def test_operator_restart_waits_until_next_bar_and_keeps_dust_cost(self):
        frame = candles([[100, 100, 70, 75, 1000], [75, 76, 45, 50, 1000]])
        bot = GridBacktest(self.config(restart_after_stop=True))
        report = bot.run(frame, START, START + pd.Timedelta(hours=2))
        starts = [e for e in bot.events if e['event'] == 'GRID_STARTED']
        self.assertEqual(len(starts), 2)
        self.assertEqual(pd.Timestamp(starts[1]['timestamp']), START + pd.Timedelta(hours=1))
        self.assertLess(D(starts[1]['allocation']), D(500))
        self.assertGreaterEqual(report['hard_stop_triggers'], 2)
        self.assertAlmostEqual(report['ending_equity'] - 500,
                               report['total_realized_pnl'] + report['unrealized_pnl'], places=9)

    def test_ohlc_ordering_changes_results_instead_of_claiming_certain_intrabar_profit(self):
        frame = candles([[100, 101.5, 97.8, 99, 1000]])
        a, b = GridBacktest(self.config(), 'ohlc'), GridBacktest(self.config(), 'olhc')
        ra = a.run(frame, START, START + pd.Timedelta(hours=1))
        rb = b.run(frame, START, START + pd.Timedelta(hours=1))
        self.assertNotEqual(ra['total_trades'], rb['total_trades'])
        self.assertNotEqual(ra['total_realized_pnl'], rb['total_realized_pnl'])

    def test_end_inventory_is_marked_not_fictitiously_sold(self):
        bot = GridBacktest(self.config())
        report = bot.run(candles([[100, 100, 99.9, 99.9, 1000]]), START, START + pd.Timedelta(hours=1))
        self.assertEqual(report['total_realized_pnl'], 0)
        self.assertLess(report['unrealized_pnl'], 0)
        self.assertEqual(report['total_trades'], 1)

    def test_four_hour_breakout_carries_inventory_and_retains_stop_distance(self):
        bot = GridBacktest(self.config())
        bot.start_grid(START, D(100), D(1))
        held, cost = bot.held, sum(lot.cost for lot in bot.lots.values())
        distance = bot.stop_distance
        bot._point(D(105), START)
        retained_stop = bot.stop
        bot._point(D(105), START + pd.Timedelta(hours=4))
        self.assertEqual(bot.breakout_count, 1)
        self.assertEqual(bot.anchor, D(105))
        self.assertEqual((bot.stop, bot.stop_distance), (retained_stop, distance))
        self.assertEqual(bot.held, held)
        self.assertEqual(sum(lot.cost for lot in bot.lots.values()), cost)
        self.assertEqual(sum(f['kind'] == 'SEED' for f in bot.fills), 1)

    def test_breakout_cannot_turn_dust_into_valid_sell_lanes_or_drop_its_cost(self):
        bot = GridBacktest(self.config())
        bot.start_grid(START, D(80000), D(100))
        for order in list(bot.orders.values()):
            if order.side == 'SELL':
                bot._fill(order, START)
        self.assertGreater(bot.held, 0)
        self.assertLess(bot.held, bot.c.amount_step)
        cost = sum(lot.cost for lot in bot.lots.values())
        bot._point(D(80300), START)
        bot._point(D(80300), START + pd.Timedelta(hours=4))
        self.assertEqual(bot.mode, 'RUNNING')
        self.assertTrue(any(e['event'] == 'REANCHOR_SIZING_DEFERRED' for e in bot.events))
        self.assertEqual(sum(lot.cost for lot in bot.lots.values()), cost)
        bot._point(D(70000), START + pd.Timedelta(hours=5))
        self.assertEqual(bot.mode, 'HALTED_DUST')
        self.assertEqual(bot.stop_count, 1)

    def test_missing_or_invalid_ohlcv_is_rejected(self):
        frame = candles([[100, 101, 99, 100, 1000]])
        with self.assertRaisesRegex(ValueError, 'Missing'):
            validate_candles(frame.drop(frame.index[4]), '1h')
        frame.loc[START, 'low'] = 102
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            validate_candles(frame, '1h')

    def test_fetch_paginate_until_requested_end_even_when_pages_are_short(self):
        frame = candles([[100, 101, 99, 100, 1000]] * 10)
        rows = [[int(at.timestamp() * 1000), *row] for at, row in zip(frame.index, frame.values.tolist())]
        class PublicExchange:
            def fetch_ohlcv(self, symbol, timeframe, since, limit, params):
                self.assertion = (symbol == 'BTC/USDT' and timeframe == '1h')
                return [row for row in rows if since <= row[0] <= params['endTime']][:7]
        with patch('backtest_grid.ccxt.binance', return_value=PublicExchange()) as factory:
            actual = fetch_candles(frame.index[0], frame.index[-1] + pd.Timedelta(hours=1), '1h')
        self.assertEqual(len(actual), len(frame))
        self.assertNotIn('apiKey', factory.call_args.args[0])

    def test_offline_cli_outputs_metrics_and_never_contacts_exchange(self):
        frame = candles([[100, 101, 99, 100, 1000]] * 2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv = root / 'input.csv'
            frame.to_csv(csv, index_label='timestamp')
            self.assertEqual(len(read_candles(csv, '1h')), len(frame))
            with patch('backtest_grid.ccxt.binance', side_effect=AssertionError('Network forbidden')), redirect_stdout(io.StringIO()):
                code = main(['--csv', str(csv), '--start', START.isoformat(),
                             '--end', (START + pd.Timedelta(hours=2)).isoformat(),
                             '--output', str(root / 'output'), '--execution-timeframe', '1h',
                             '--no-trend-filter', '--cooldown-hours', '0'])
            self.assertEqual(code, 0)
            results = json.loads((root / 'output/summary.json').read_text())['results']
            self.assertEqual(len(results), 2)
            self.assertIn('max_drawdown_pct', results[0])
            self.assertIn('hard_stop_triggers', results[0])
            # Replaying a CSV in the default output location must preserve it.
            original = root / 'output/btc_usdt_1h.csv'
            original_bytes = original.read_bytes()
            with patch('backtest_grid.ccxt.binance', side_effect=AssertionError('Network forbidden')), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--csv', str(original), '--start', START.isoformat(),
                                       '--end', (START + pd.Timedelta(hours=2)).isoformat(),
                                       '--output', str(root / 'output'), '--execution-timeframe', '1h',
                                       '--no-trend-filter', '--cooldown-hours', '0']), 0)
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertTrue((root / 'output/data_snapshot.csv').is_file())
