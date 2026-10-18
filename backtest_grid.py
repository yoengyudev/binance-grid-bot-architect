#!/usr/bin/env python3
"""Research simulator for the exact-level BTC/USDT grid. No trading credentials.

Dependencies: pandas, ccxt. This module never imports the live application,
loads .env, opens its SQLite database, or submits exchange orders.

Default: 12 calendar months, 5m execution, 1h ATR, daily SMA200, 8 lanes, 500 USDT,
0.1% fees each side, 10 bps adverse MARKET slippage, both OHLC routes.
The real bot's post-stop HALT is the default. --restart-after-stop is an
explicit operator scenario: acknowledge unsellable dust and start a fresh
ATR grid only after an 8h cooldown AND price > the last closed daily SMA200.
Initial entry and automatic re-anchors also require price > that SMA.
Default Iteration 2 policy: fixed 15% catastrophic stop below the lower bound,
with no trailing, Daily SMA200 deployment gate and manual recovery after a stop.
Ongoing BUYs require Yahoo's completed daily BTC-USD close > its SMA200.
--buy-trend-policy legacy reproduces the earlier entry-only fixed-stop studies.
New campaigns enforce the production cost-basis plus pending BUY notional cap.
--allocation-policy legacy_cash preserves the older cash-only sizing model.
--replacement-buy-policy budget_aware is an opt-in research policy, not live behavior.
No-stop and trailing modes remain explicit research comparisons.

OHLC paths, instantaneous full limit fills, constant historical filters, and
interpolated breakout continuity are assumptions, not tick-level replay.
See BACKTEST.md for model boundaries and interpretation.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_CEILING
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import ccxt
import pandas as pd
from grid_geometry import MIN_GRID_RETURN, defended_bounds, validate_grid_returns
from budget_policy import CAP_EXHAUSTED, residual_quantity

D = Decimal
ZERO = D(0)
EPS = D('1e-18')
HOUR_MS = 3_600_000
TIMEFRAMES = {'1m': pd.Timedelta(minutes=1), '5m': pd.Timedelta(minutes=5),
              '1h': pd.Timedelta(hours=1), '4h': pd.Timedelta(hours=4),
              '1d': pd.Timedelta(days=1)}
COLUMNS = ['timestamp', 'open', 'high', 'low', 'close', 'volume']


class SizingError(ValueError):
    """A strategy order is below its minimum; keep inventory risk monitoring."""


class AllocationCapError(SizingError):
    """A proposed BUY commitment exceeds the campaign's total purchase-cost cap."""


def decimal(value) -> Decimal:
    return D(str(value))


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


@dataclass(frozen=True)
class Config:
    capital: Decimal = D('500')
    levels: int = 8
    fee: Decimal = D('0.001')
    buy_fee_asset: str = 'base'
    slippage_bps: Decimal = D('10')
    seed_fraction: Decimal = D('0.5')
    price_tick: Decimal = D('0.01')
    amount_step: Decimal = D('0.00001')
    min_notional: Decimal = D('5')
    stop_gap: Decimal = D('0.15')
    restart_after_stop: bool = False
    breakout_hours: Decimal = D('4')
    execution_timeframe: str = '5m'
    indicator_timeframe: str = '1h'
    trend_filter: bool = True
    cooldown_hours: Decimal = D('8')
    min_grid_return: Decimal = MIN_GRID_RETURN
    trend_timeframe: str = '1d'
    trend_period: int = 200
    stop_mode: str = 'fixed'
    buy_trend_policy: str = 'production'
    allocation_policy: str = 'total_cost_cap'
    replacement_buy_policy: str = 'fixed'
    atr_multiple: Decimal = D('2')

    def __post_init__(self):
        values = (self.capital, self.fee, self.slippage_bps, self.seed_fraction,
                  self.price_tick, self.amount_step, self.min_notional,
                  self.stop_gap, self.breakout_hours, self.cooldown_hours, self.min_grid_return,
                  self.atr_multiple)
        if not all(v.is_finite() for v in values):
            raise ValueError('Configuration must contain finite numbers.')
        if (isinstance(self.levels, bool) or not isinstance(self.levels, int)
                or not 2 <= self.levels <= 100 or self.capital <= 0
                or not 0 <= self.fee < 1 or not 0 < self.seed_fraction < 1
                or not 0 <= self.slippage_bps < 10_000
                or self.price_tick <= 0 or self.amount_step <= 0
                or self.min_notional <= 0 or not 0 <= self.stop_gap < 1
                or self.breakout_hours < 0 or self.cooldown_hours < 0
                or not 0 <= self.min_grid_return < 1
                or self.atr_multiple <= 0
                or self.execution_timeframe not in ('1m', '5m', '1h')
                or self.indicator_timeframe not in ('1h', '4h')
                or self.trend_timeframe not in ('1h', '4h', '1d')
                or isinstance(self.trend_period, bool) or not isinstance(self.trend_period, int)
                or not 2 <= self.trend_period <= 1000
                or self.stop_mode not in ('none', 'fixed', 'trailing')
                or self.buy_trend_policy not in ('production', 'legacy')
                or self.allocation_policy not in ('total_cost_cap', 'legacy_cash')
                or self.replacement_buy_policy not in ('fixed', 'budget_aware', 'residual')
                or (self.replacement_buy_policy in ('budget_aware', 'residual')
                    and self.allocation_policy != 'total_cost_cap')
                or (self.replacement_buy_policy == 'residual' and self.restart_after_stop)
                or self.buy_fee_asset not in ('base', 'quote')):
            raise ValueError('Invalid capital, levels, fees, precision, or risk settings.')


def prepare_buy_signals(frame: pd.DataFrame) -> pd.DataFrame:
    """Yahoo's last 200 completed daily closes, observable next UTC midnight."""
    daily = validate_candles(frame, '1d')
    signals = pd.DataFrame({'buy_close': daily.close,
                            'buy_sma': daily.close.rolling(200, min_periods=200).mean()})
    signals.index += TIMEFRAMES['1d']
    return signals


def closed_atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """Mirror pandas_ta's non-TA-Lib ATR on each fresh 15-candle window.

    pandas_ta seeds RMA with the first 14 TR values then smooths the 15th.
    The first candle of each local window has high-low as its TR. Values from
    this function become observable only when that macro candle closes.
    """
    hl = frame.high - frame.low
    previous_close = frame.close.shift(1)
    tr = pd.concat([hl, (frame.high - previous_close).abs(),
                    (frame.low - previous_close).abs()], axis=1).max(axis=1)
    first_mean = (tr.rolling(period).sum().shift(1)
                  - tr.shift(period) + hl.shift(period)) / period
    return (first_mean * (period - 1) + tr) / period


def atr_for_next_open(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    return closed_atr(frame, period).shift(1)


def aggregate_candles(frame, source_timeframe, target_timeframe):
    source_step, target_step = TIMEFRAMES[source_timeframe], TIMEFRAMES[target_timeframe]
    if source_step > target_step or target_step % source_step:
        raise ValueError('Indicator timeframe must be an integer multiple of source bars.')
    grouped = frame.resample(target_step, origin='epoch', closed='left', label='left')
    macro = grouped.agg({'open': 'first', 'high': 'max', 'low': 'min',
                         'close': 'last', 'volume': 'sum'})
    return macro.loc[grouped['close'].count() == target_step // source_step]


def prepare_indicators(frame: pd.DataFrame, execution_timeframe='5m',
                       indicator_timeframe='1h', *, trend_timeframe=None,
                       trend_period=50, trend_frame=None) -> pd.DataFrame:
    """Aggregate complete UTC macro bars; publish signals ONLY at their close.

    Never shift a macro signal by one execution bar: a 1h candle is unavailable
    throughout its entire hour. Count checks reject partial aggregation buckets.
    """
    frame = validate_candles(frame, execution_timeframe)
    macro = aggregate_candles(frame, execution_timeframe, indicator_timeframe)
    atr = closed_atr(macro)
    atr.index = atr.index + TIMEFRAMES[indicator_timeframe]
    trend_timeframe = trend_timeframe or indicator_timeframe
    trend = (validate_candles(trend_frame, trend_timeframe) if trend_frame is not None
             else aggregate_candles(frame, execution_timeframe, trend_timeframe))
    sma = trend.close.rolling(trend_period, min_periods=trend_period).mean()
    sma.index = sma.index + TIMEFRAMES[trend_timeframe]
    # The two independent availability clocks prevent daily data from leaking
    # into the preceding 288 five-minute execution bars.
    return pd.DataFrame({'atr': atr.reindex(frame.index, method='ffill'),
                          'macro_sma': sma.reindex(frame.index, method='ffill')})


def atr_bounds(center: Decimal, atr: Decimal, config: Config):
    """Fee-defended ATR width and the selected fixed or legacy trailing buffer."""
    lower, ceiling = defended_bounds(center, config.atr_multiple * atr, (config.levels + 1) // 2,
                                     config.levels // 2, minimum=config.min_grid_return,
                                     tick=config.price_tick)
    if config.stop_mode == 'none':
        return lower, ceiling, None
    buffer = lower * config.stop_gap
    if config.stop_mode == 'trailing':
        buffer = max(atr, buffer)  # Explicit legacy comparison, not the fixed policy.
    stop = floor_step(lower - buffer, config.price_tick)
    if not ZERO < stop < lower < center < ceiling:
        raise ValueError('ATR proposal has invalid positive bounds or hard stop.')
    return lower, ceiling, stop


def geometric_levels(anchor: Decimal, bound: Decimal, count: int,
                     tick: Decimal) -> list[Decimal]:
    ratio = (bound / anchor) ** (D(1) / count)
    levels = [round_tick(bound if i == count else anchor * ratio ** i, tick)
              for i in range(1, count + 1)]
    if len(set(levels + [round_tick(anchor, tick)])) != count + 1:
        raise ValueError('Exchange rounding collapses adjacent grid levels.')
    return levels


def split_amount(total: Decimal, count: int, step: Decimal) -> list[Decimal]:
    remaining = floor_step(total, step)
    result = []
    for i in range(count):
        amount = floor_step(remaining / (count - i), step)
        result.append(amount)
        remaining -= amount
    assert remaining == 0
    return result


@dataclass
class Lot:
    quantity: Decimal
    cost: Decimal


@dataclass
class Lane:
    level: int
    buy_price: Decimal
    sell_price: Decimal
    budget: Decimal | None
    next_side: str = 'BUY'
    quantity: Decimal = ZERO
    lot_id: int | None = None


@dataclass
class Order:
    level: int
    side: str
    price: Decimal
    quantity: Decimal
    lot_id: int | None


class GridBacktest:
    def __init__(self, config: Config, path: str = 'ohlc'):
        if path not in ('ohlc', 'olhc'):
            raise ValueError('Path must be ohlc or olhc.')
        self.c, self.path = config, path
        self.cash = config.capital  # Includes quote reserved by open BUYs.
        self.lots: dict[int, Lot] = {}
        self.reserves: list[Lot] = []
        self.lanes: dict[int, Lane] = {}
        self.orders: dict[int, Order] = {}
        self.fills, self.curve, self.events = [], [], []
        self.realized = self.fees = ZERO
        self.stop_count = self.breakout_count = 0
        self.mode = 'WAITING'
        self.reason = ''
        self.lower = self.upper = self.anchor = ZERO
        self.stop: Decimal | None = None if config.stop_mode == 'none' else ZERO
        self.high_water = self.stop_distance = self.width_fraction = ZERO
        self.allocation = config.capital
        self.above_since: pd.Timestamp | None = None
        self.next_lot = 1
        self.peak = config.capital
        self.max_dd = self.max_dd_quote = ZERO
        self.last_stop_at: pd.Timestamp | None = None
        self.macro_sma: Decimal | None = None
        self.buy_macro_close: Decimal | None = None
        self.buy_macro_sma: Decimal | None = None
        self.entry_trend_blocked_bars = self.cooldown_blocked_bars = 0
        self.reanchor_trend_blocks = 0
        self._reanchor_block_logged = False
        self.reanchor_floor_deferrals = 0
        self._floor_deferral_logged = False
        self.state_bar_counts: dict[str, int] = {}
        self.minimum_installed_grid_return: Decimal | None = None
        self.buy_paused = False
        self.buy_pause_episodes = self.buy_resume_episodes = self.buy_pause_bars = 0
        self.sma_canceled_buys = 0
        self.allocation_cap_buy_pauses = self.allocation_cap_reanchor_deferrals = 0
        self.budget_buy_resizes = self.budget_buy_deferrals = 0
        self.budget_deferred_lanes: set[int] = set()
        self.cap_exhausted_at = None
        self.residual_buy_count = 0

    def _trend_allows(self, price):
        return not self.c.trend_filter or (self.macro_sma is not None and price > self.macro_sma)

    def _buys_allowed(self, price):
        if not self.c.trend_filter:
            return True
        if self.c.buy_trend_policy == 'production':
            return (self.buy_macro_close is not None and self.buy_macro_sma is not None
                    and self.buy_macro_close > self.buy_macro_sma)
        return self.c.stop_mode != 'none' or self._trend_allows(price)

    def _sync_buy_pause(self, price, at):
        if self.mode not in ('RUNNING', 'SIZING_PAUSED', CAP_EXHAUSTED):
            self.buy_paused = False  # HALT is not an active BUY-pause episode.
            return
        paused = not self._buys_allowed(price)
        if paused and self.c.buy_trend_policy == 'production':
            # Production expires continuity when macro-off observations stop.
            self.above_since = None
        if paused:
            self.budget_deferred_lanes.clear()
            canceled = sum(o.side == 'BUY' for o in self.orders.values())
            self.sma_canceled_buys += canceled
            self.orders = {key: o for key, o in self.orders.items() if o.side == 'SELL'}
        else:
            canceled = 0
        if paused != self.buy_paused:
            if paused:
                self.buy_pause_episodes += 1
            else:
                self.buy_resume_episodes += 1
            self._event(at, 'SMA_BUYS_PAUSED' if paused else 'SMA_BUYS_RESUMED',
                        price=price, macro_sma=self.macro_sma, canceled_buys=canceled,
                        buy_macro_close=self.buy_macro_close, buy_macro_sma=self.buy_macro_sma,
                        held_btc=self.held)
        self.buy_paused = paused

    @property
    def held(self):
        return sum((lot.quantity for lot in self.lots.values()), ZERO)

    def free_quote(self):
        extra = self.c.fee if self.c.buy_fee_asset == 'quote' else ZERO
        return self.cash - sum((o.price * o.quantity * (1 + extra)
                                for o in self.orders.values() if o.side == 'BUY'), ZERO)

    def position_cost(self):
        # Acknowledged dust/reserved wallet BTC belongs outside this campaign.
        return sum((lot.cost for lot in self.lots.values()), ZERO)

    def buy_commitment(self):
        # Resting simulator orders are unfilled; fees are not part of commitment.
        return sum((o.price * o.quantity for o in self.orders.values() if o.side == 'BUY'), ZERO)

    def _require_total_cap(self, position_cost, buy_commitment, *, allocation=None):
        if self.c.allocation_policy == 'legacy_cash':
            return
        allocation = self.allocation if allocation is None else allocation
        if (not position_cost.is_finite() or not buy_commitment.is_finite()
                or position_cost < 0 or buy_commitment < 0):
            raise ValueError('Total allocation cost is invalid.')
        # Match production's float-projection tolerance; do not use market value.
        if position_cost + buy_commitment - allocation > D('0.000001'):
            raise AllocationCapError(
                f'Total allocation cap exceeded: bot BTC cost {position_cost:.2f} + '
                f'BUY commitments {buy_commitment:.2f} = {position_cost + buy_commitment:.2f} USDT, '
                f'above {allocation:.2f}. Future fees and price appreciation are excluded.')

    def _event(self, at, kind, **values):
        self.events.append({'timestamp': pd.Timestamp(at).isoformat(), 'event': kind,
                            **{key: str(value) for key, value in values.items()}})

    def mark(self, at, price, event='mark'):
        position = self.held + sum((lot.quantity for lot in self.reserves), ZERO)
        cost = sum((lot.cost for lot in self.lots.values()), ZERO)
        cost += sum((lot.cost for lot in self.reserves), ZERO)
        equity = self.cash + position * price
        unrealized = position * price - cost
        if abs(equity - self.c.capital - self.realized - unrealized) > D('1e-9'):
            raise AssertionError('Cash, realized P&L and cost-basis ledger disagree.')
        self.peak = max(self.peak, equity)
        self.max_dd_quote = max(self.max_dd_quote, self.peak - equity)
        self.max_dd = max(self.max_dd, (self.peak - equity) / self.peak)
        self.curve.append({'timestamp': pd.Timestamp(at).isoformat(), 'price': float(price),
                           'equity': float(equity), 'cash': float(self.cash),
                           'bot_btc': float(self.held), 'total_btc': float(position),
                           'realized_pnl': float(self.realized), 'unrealized_pnl': float(unrealized),
                           'hard_stop': float(self.stop) if self.stop is not None else None,
                           'buy_paused': self.buy_paused, 'state': self.mode, 'event': event})

    def _record_fill(self, at, side, kind, price, quantity, fee, pnl=ZERO, level=None):
        self.fees += fee
        self.realized += pnl
        self.fills.append({'timestamp': pd.Timestamp(at).isoformat(), 'side': side,
                           'kind': kind, 'level': level, 'price': str(price),
                           'quantity': str(quantity), 'fee_quote_value': str(fee),
                           'realized_pnl': str(pnl)})

    def _buy(self, at, price, quantity, kind, level=None):
        notional = quantity * price
        fee = notional * self.c.fee
        spent = notional + (fee if self.c.buy_fee_asset == 'quote' else ZERO)
        received = quantity * (1 - self.c.fee) if self.c.buy_fee_asset == 'base' else quantity
        if spent > self.cash + EPS:
            raise AssertionError('Simulator attempted to borrow quote.')
        self.cash -= spent
        lot_id = self.next_lot
        self.next_lot += 1
        self.lots[lot_id] = Lot(received, spent)
        self._record_fill(at, 'BUY', kind, price, quantity, fee, level=level)
        return lot_id

    def _valid_order(self, quantity, price, *, market=False):
        minimum = self.c.min_notional if market else max(D('7'), self.c.min_notional)
        return quantity >= self.c.amount_step and quantity * price >= minimum

    def _quantity_for_budget(self, budget, price):
        fee_factor = 1 + self.c.fee if self.c.buy_fee_asset == 'quote' else D(1)
        return floor_step(budget / (price * fee_factor), self.c.amount_step)

    def _budget_buy_quantity(self, requested, price, sell_price):
        """Research only: fit a replacement to remaining cost/cash, never enlarge it.

        Reserve quote-denominated BUY fees for resting BUYs as well as this one.
        This is conservative sizing; the underlying production cap still excludes
        future fees. Both the BUY and its net, rounded reverse SELL must be valid.
        An unfundable lane waits for later SELL proceeds rather than pausing peers.
        """
        fee_factor = 1 + self.c.fee if self.c.buy_fee_asset == 'quote' else D(1)
        room = self.allocation - self.position_cost() - self.buy_commitment() * fee_factor
        budget = max(ZERO, min(room, self.free_quote()))
        quantity = min(requested, self._quantity_for_budget(budget, price))
        received = quantity * (1 - self.c.fee) if self.c.buy_fee_asset == 'base' else quantity
        reverse = floor_step(received, self.c.amount_step)
        if not self._valid_order(quantity, price) or not self._valid_order(reverse, sell_price):
            return ZERO
        return quantity

    def _pause_sizing(self, at, reason):
        self.orders = {key: order for key, order in self.orders.items() if order.side == 'SELL'}
        self.budget_deferred_lanes.clear()
        self.mode, self.reason = 'SIZING_PAUSED', reason
        self.above_since = None
        self._event(at, 'SIZING_PAUSE', reason=reason)

    def _plan(self, anchor, lower, upper, stop, allocation, inventory, *,
              position_cost=None, seed_quote=ZERO):
        nb, ns = (self.c.levels + 1) // 2, self.c.levels // 2
        buys = geometric_levels(anchor, lower, nb, self.c.price_tick)
        sells = geometric_levels(anchor, upper, ns, self.c.price_tick)
        validate_grid_returns(round_tick(anchor, self.c.price_tick), buys, sells,
                              self.c.min_grid_return)
        if stop is not None and any(price <= stop for price in buys):
            raise ValueError('A configured BUY is at or below the retained stop.')
        lower_budget = allocation * (1 - self.c.seed_fraction) / nb
        if any(not self._valid_order(self._quantity_for_budget(lower_budget, p), p) for p in buys):
            raise SizingError('A lower BUY is below exchange/grid order minimums.')
        commitment = sum((self._quantity_for_budget(lower_budget, p) * p for p in buys), ZERO)
        self._require_total_cap(self.position_cost() if position_cost is None else position_cost,
                                commitment + seed_quote, allocation=allocation)
        amounts = split_amount(inventory, ns, self.c.amount_step)
        if any(not self._valid_order(q, p) for q, p in zip(amounts, sells)):
            raise SizingError('Carried/seed BTC is too small for the upper SELL lanes.')
        return buys, sells, amounts, lower_budget

    def _install(self, anchor, lower, upper, buys, sells, amounts, lower_budget, lot_id):
        actual = validate_grid_returns(round_tick(anchor, self.c.price_tick), buys, sells,
                                       self.c.min_grid_return)
        self.minimum_installed_grid_return = (actual if self.minimum_installed_grid_return is None
                                              else min(actual, self.minimum_installed_grid_return))
        self.anchor = anchor
        self.lower = round_tick(lower, self.c.price_tick)
        self.upper = round_tick(upper, self.c.price_tick)
        self.lanes = {}
        self.budget_deferred_lanes.clear()
        for index, price in enumerate(buys):
            self.lanes[index + 1] = Lane(index + 1, price,
                                         round_tick(anchor, self.c.price_tick) if index == 0 else buys[index - 1],
                                         lower_budget)
        for index, price in enumerate(sells):
            self.lanes[-index - 1] = Lane(-index - 1,
                                          round_tick(anchor, self.c.price_tick) if index == 0 else sells[index - 1],
                                          price, None, 'SELL', amounts[index], lot_id)
        self.mode = 'RUNNING'
        self.reason = ''
        self.above_since = None

    def start_grid(self, at, center, atr):
        if self.c.buy_trend_policy == 'production' and not self._buys_allowed(center):
            raise ValueError('Completed Yahoo SMA200 BUY approval is required before the seed.')
        lower, upper, stop = atr_bounds(center, atr, self.c)
        execution = center * (1 + self.c.slippage_bps / 10_000)
        # Restarts use remaining cash only: no hidden account top-ups.
        allocation = self.cash if self.c.restart_after_stop else self.c.capital
        seed_qty = self._quantity_for_budget(allocation * self.c.seed_fraction, execution)
        net = seed_qty * (1 - self.c.fee) if self.c.buy_fee_asset == 'base' else seed_qty
        buys, sells, amounts, budget = self._plan(
            center, lower, upper, stop, allocation, net,
            position_cost=ZERO, seed_quote=allocation * self.c.seed_fraction)
        if not self._valid_order(seed_qty, execution, market=True):
            raise ValueError('Initial market BUY is below exchange minimums.')
        self.allocation, self.stop = allocation, stop
        self.high_water = center
        self.stop_distance = center - stop if stop is not None else ZERO
        self.width_fraction = (upper - center) / center
        lot_id = self._buy(at, execution, seed_qty, 'SEED')
        self._install(center, lower, upper, buys, sells, amounts, budget, lot_id)
        self._event(at, 'GRID_STARTED', center=center, atr=atr, lower=lower,
                    upper=upper, stop=stop, allocation=allocation, macro_sma=self.macro_sma,
                    trend_timeframe=self.c.trend_timeframe, trend_period=self.c.trend_period,
                    stop_mode=self.c.stop_mode)
        self._sync_buy_pause(center, at)
        self._maintain(center, at)

    def _maintain(self, price, at):
        if self.mode not in ('RUNNING', CAP_EXHAUSTED):
            return
        for lane in sorted(self.lanes.values(), key=lambda lane: (lane.level > 0, abs(lane.level))):
            if lane.level in self.orders:
                continue
            target = lane.buy_price if lane.next_side == 'BUY' else lane.sell_price
            if lane.next_side == 'BUY':
                if self.mode == CAP_EXHAUSTED:
                    continue
                if not self._buys_allowed(price):
                    continue
                if (not self.lower <= price <= self.upper or target >= price
                        or (self.stop is not None and target <= self.stop)):
                    continue  # Outside bounds or Post-Only would immediately take.
                quantity = (self._quantity_for_budget(lane.budget, target) if lane.budget is not None
                            else floor_step(lane.quantity, self.c.amount_step))
            else:
                if target <= price:
                    continue  # Do not retroactively fill a newly created SELL.
                quantity = lane.quantity
            if not self._valid_order(quantity, target):
                self._pause_sizing(at, 'A replacement order rounded below its minimum.')
                return
            if lane.next_side == 'BUY':
                fee_factor = 1 + self.c.fee if self.c.buy_fee_asset == 'quote' else D(1)
                requested = quantity
                final_residual = False
                if self.c.replacement_buy_policy == 'residual':
                    room = self.allocation-self.position_cost()-self.buy_commitment()
                    if quantity*target > room:
                        quantity = residual_quantity(quantity, target, room,
                            lambda value: floor_step(value, self.c.amount_step))
                        received = quantity*(1-self.c.fee) if self.c.buy_fee_asset == 'base' else quantity
                        reverse = floor_step(received,self.c.amount_step)
                        if not self._valid_order(quantity,target) or not self._valid_order(reverse,lane.sell_price):
                            self._exhaust_cap(at, 'Residual cannot fund a valid BUY and reverse SELL.')
                            continue
                        final_residual = True
                if self.c.replacement_buy_policy == 'budget_aware':
                    quantity = self._budget_buy_quantity(quantity, target, lane.sell_price)
                    if quantity == ZERO:
                        if lane.level not in self.budget_deferred_lanes:
                            self.budget_buy_deferrals += 1
                            self.budget_deferred_lanes.add(lane.level)
                            self._event(at, 'BUY_BUDGET_DEFERRED', level=lane.level,
                                        requested=requested, price=target,
                                        position_cost=self.position_cost(),
                                        buy_commitment=self.buy_commitment(),
                                        free_quote=self.free_quote(), allocation=self.allocation)
                        continue
                if quantity * target * fee_factor > self.free_quote() + EPS:
                    continue
                try:
                    self._require_total_cap(self.position_cost(), self.buy_commitment() + quantity * target)
                except AllocationCapError as error:
                    self.allocation_cap_buy_pauses += 1
                    self._pause_sizing(at, str(error))
                    return  # Keep existing SELLs and risk checks; do not resize/market-sell BTC.
            else:
                lot = self.lots[lane.lot_id]
                locked = sum((o.quantity for o in self.orders.values()
                              if o.side == 'SELL' and o.lot_id == lane.lot_id), ZERO)
                if quantity > lot.quantity - locked + EPS:
                    raise AssertionError('SELL allocation exceeds its parent lot.')
            self.orders[lane.level] = Order(lane.level, lane.next_side, target, quantity, lane.lot_id)
            if lane.next_side == 'BUY' and self.c.replacement_buy_policy == 'residual' and final_residual:
                self.residual_buy_count += 1
                self._event(at,'FINAL_RESIDUAL_BUY',level=lane.level,price=target,
                            quantity=quantity,requested=requested,remaining=room)
                self._exhaust_cap(at, 'Final residual BUY reserved; no automatic BUY restart.')
            if lane.next_side == 'BUY' and self.c.replacement_buy_policy == 'budget_aware':
                if lane.level in self.budget_deferred_lanes:
                    self.budget_deferred_lanes.remove(lane.level)
                    self._event(at, 'BUY_BUDGET_RESUMED', level=lane.level, quantity=quantity)
                if quantity < requested:
                    self.budget_buy_resizes += 1
                    self._event(at, 'BUY_BUDGET_RESIZED', level=lane.level, price=target,
                                requested=requested, quantity=quantity,
                                position_cost=self.position_cost(),
                                buy_commitment=self.buy_commitment(), allocation=self.allocation)

    def _exhaust_cap(self, at, reason):
        if self.cap_exhausted_at is None:
            self.cap_exhausted_at = pd.Timestamp(at)
            self._event(at,CAP_EXHAUSTED,reason=reason)
        self.mode,self.reason = CAP_EXHAUSTED,reason
        self.above_since = None

    def _fill(self, order, at):
        del self.orders[order.level]
        lane = self.lanes[order.level]
        if order.side == 'BUY':
            lot_id = self._buy(at, order.price, order.quantity, 'LIMIT', order.level)
            lane.lot_id = lot_id
            lane.quantity = floor_step(self.lots[lot_id].quantity, self.c.amount_step)
            lane.next_side = 'SELL'
        else:
            lot = self.lots[order.lot_id]
            cost = lot.cost * order.quantity / lot.quantity
            proceeds = order.price * order.quantity
            fee = proceeds * self.c.fee
            self.cash += proceeds - fee
            lot.quantity -= order.quantity
            lot.cost -= cost
            self._record_fill(at, 'SELL', 'LIMIT', order.price, order.quantity,
                              fee, proceeds - fee - cost, order.level)
            lane.quantity, lane.next_side = order.quantity, 'BUY'

    def _hard_stop(self, price, at):
        if self.c.stop_mode == 'none':
            raise RuntimeError('Hard-stop liquidation is disabled in no-stop research mode.')
        self.stop_count += 1
        self.last_stop_at = pd.Timestamp(at)
        self.orders.clear()
        self.buy_paused = False
        self.budget_deferred_lanes.clear()
        self.above_since = None
        held, cost = self.held, sum((lot.cost for lot in self.lots.values()), ZERO)
        execution = price * (1 - self.c.slippage_bps / 10_000)
        quantity = floor_step(held, self.c.amount_step)
        sold = ZERO
        if self._valid_order(quantity, execution, market=True):
            sold = quantity
            fraction = sold / held
            for lot in self.lots.values():
                lot.quantity *= 1 - fraction
                lot.cost *= 1 - fraction
            gross, allocated_cost = sold * execution, cost * fraction
            fee = gross * self.c.fee
            self.cash += gross - fee
            self._record_fill(at, 'SELL', 'HARD_STOP', execution, sold,
                              fee, gross - fee - allocated_cost)
        self.mode = 'HALTED_DUST' if self.held > EPS else 'HALTED'
        self.reason = ('Hard stop; waiting for cooldown AND macro trend.' if self.c.restart_after_stop
                       else 'Hard stop; manual recovery/restart required.')
        self._event(at, 'HARD_STOP', trigger=price, floor=self.stop, sold=sold,
                    residual=self.held, state=self.mode,
                    cooldown_until=at + pd.Timedelta(hours=float(self.c.cooldown_hours)))

    def _risk(self, price, at):
        if self.c.stop_mode == 'none':
            return
        if self.mode not in ('RUNNING', 'SIZING_PAUSED', CAP_EXHAUSTED) or self.held <= EPS:
            return  # No stop/trailing activity when bot BTC is actually flat.
        if price > self.high_water:
            self.high_water = price
            if self.c.stop_mode == 'trailing':
                self.stop = max(self.stop, price - self.stop_distance)
        if price < self.stop:  # Production uses strict <, not <=.
            self._hard_stop(price, at)
            return
        self.orders = {key: o for key, o in self.orders.items()
                       if o.side != 'BUY' or o.price > self.stop}

    def _breakout(self, price, at):
        if self.c.buy_trend_policy == 'production' and not self._buys_allowed(price):
            self.above_since = None
            return
        if (self.mode != 'RUNNING' or self.held <= EPS
                or price <= self.upper or self.c.breakout_hours == 0):
            self.above_since = None
            self._reanchor_block_logged = False
            self._floor_deferral_logged = False
            return
        if self.above_since is None:
            self.above_since = at
        deadline = self.above_since + pd.Timedelta(hours=float(self.c.breakout_hours))
        if at < deadline:
            return
        if not self._trend_allows(price):
            # Preserve live orders and risk monitoring while entry is blocked.
            if not self._reanchor_block_logged:
                self.reanchor_trend_blocks += 1
                self._event(at, 'REANCHOR_TREND_BLOCK', price=price, macro_sma=self.macro_sma)
                self._reanchor_block_logged = True
            return
        self._reanchor_block_logged = False
        lower, upper = defended_bounds(price, price * self.width_fraction,
                                       (self.c.levels + 1) // 2, self.c.levels // 2,
                                       minimum=self.c.min_grid_return, tick=self.c.price_tick)
        if self.stop is not None and lower <= self.stop:
            if not self._floor_deferral_logged:
                self.reanchor_floor_deferrals += 1
                self._event(at, 'REANCHOR_FLOOR_DEFERRED', lower=lower, retained_stop=self.stop)
                self._floor_deferral_logged = True
            return  # Keep existing orders, cost basis and risk monitoring intact.
        self._floor_deferral_logged = False
        carry, cost = self.held, sum((lot.cost for lot in self.lots.values()), ZERO)
        new_stop = (max(self.stop, floor_step(lower * (1 - self.c.stop_gap), self.c.price_tick))
                    if self.c.stop_mode == 'fixed' else self.stop)
        try:
            plan = self._plan(price, lower, upper, new_stop, self.allocation, carry)
            fee_factor = 1 + self.c.fee if self.c.buy_fee_asset == 'quote' else D(1)
            needed = sum((self._quantity_for_budget(plan[3], p) * p * fee_factor for p in plan[0]), ZERO)
            if needed > self.cash:
                self._event(at, 'REANCHOR_CAPITAL_DEFERRED', required=needed, cash=self.cash)
                self.above_since = at
                return
        except AllocationCapError as error:
            self.allocation_cap_reanchor_deferrals += 1
            self._event(at, 'REANCHOR_ALLOCATION_CAP_DEFERRED', reason=error,
                        carried_cost=cost, allocation=self.allocation)
            self.above_since = at
            return
        except SizingError as error:
            self._event(at, 'REANCHOR_SIZING_DEFERRED', reason=error)
            self.above_since = at
            return
        except ValueError as error:
            self.mode, self.reason = 'HALTED_FAULT', str(error)
            self.above_since = None
            self._event(at, 'BREAKOUT_REJECTED', reason=error)
            return
        self.orders.clear()  # Commit only a feasible replacement, not before validation.
        self.stop = new_stop
        if self.c.stop_mode == 'fixed':
            self.stop_distance = price - new_stop
        self.lots = {self.next_lot: Lot(carry, cost)}
        lot_id, self.next_lot = self.next_lot, self.next_lot + 1
        self._install(price, lower, upper, *plan, lot_id)
        self.breakout_count += 1
        self._event(at, 'BREAKOUT_REANCHOR', center=price, lower=lower, upper=upper,
                    macro_sma=self.macro_sma, retained_stop=self.stop, stop_mode=self.c.stop_mode)

    def _point(self, price, at):
        self._sync_buy_pause(price, at)
        self._risk(price, at)
        self._breakout(price, at)
        self._maintain(price, at)
        self.mark(at, price)

    def _segment(self, start_price, end_price, start_time, end_time):
        price, at = start_price, start_time
        self._point(price, at)
        while price != end_price and self.mode in ('RUNNING', 'SIZING_PAUSED', CAP_EXHAUSTED):
            ascending = end_price > price
            candidates = []
            for order in self.orders.values():
                if ((ascending and order.side == 'SELL' and price < order.price <= end_price)
                        or (not ascending and order.side == 'BUY' and end_price <= order.price < price)):
                    candidates.append(order.price)
            # Observe entry/exit of the grid even if there are no open orders.
            boundaries = [self.lower, self.upper, self.upper + self.c.price_tick]
            if (self.c.buy_trend_policy == 'legacy' and self.c.stop_mode == 'none'
                    and self.c.trend_filter and self.macro_sma is not None):
                # Observe both sides of the threshold so cancellations/resumptions
                # happen within the chosen OHLC route, not only at bar extrema.
                boundaries.extend([self.macro_sma,
                                   floor_step(self.macro_sma, self.c.price_tick) + self.c.price_tick])
            for boundary in boundaries:
                if (price < boundary <= end_price if ascending else end_price <= boundary < price):
                    candidates.append(boundary)
            if not ascending and self.held > EPS and self.stop is not None:
                strict_trigger = ((self.stop / self.c.price_tick).to_integral_value(
                    rounding=ROUND_CEILING) - 1) * self.c.price_tick
                if end_price <= strict_trigger < price:
                    candidates.append(strict_trigger)
            target = (min(candidates) if ascending else max(candidates)) if candidates else end_price
            fraction = (target - price) / (end_price - price)
            event_time = at + (end_time - at) * float(fraction)
            if self.above_since is not None:
                deadline = self.above_since + pd.Timedelta(hours=float(self.c.breakout_hours))
                if at < deadline < event_time:
                    target = price + (end_price - price) * decimal((deadline - at) / (end_time - at))
                    event_time = deadline
            resting = [o for o in self.orders.values() if o.price == target and
                       o.side == ('SELL' if ascending else 'BUY')]
            self.mark(event_time, target, 'before_fills')
            for order in resting:
                if order.level in self.orders:
                    self._fill(order, event_time)
            price, at = target, event_time
            self._point(price, at)
        self._point(end_price, end_time)

    def _restart(self, at, price, atr):
        if self.mode == 'WAITING' or (self.c.restart_after_stop and self.mode in ('HALTED', 'HALTED_DUST')):
            if self.last_stop_at is not None and at < self.last_stop_at + pd.Timedelta(hours=float(self.c.cooldown_hours)):
                self.cooldown_blocked_bars += 1
                return
            if not self._trend_allows(price) or not self._buys_allowed(price):
                self.entry_trend_blocked_bars += 1
                return
            if self.mode == 'HALTED_DUST':
                if self._valid_order(floor_step(self.held, self.c.amount_step), price, market=True):
                    return  # A tradable residual cannot be acknowledged as dust.
                self._event(at, 'OPERATOR_DUST_ACK', quantity=self.held,
                            cost=sum((lot.cost for lot in self.lots.values()), ZERO))
                self.reserves.extend(self.lots.values())
            self.lots, self.orders = {}, {}
            try:
                self.start_grid(at, price, atr)
            except ValueError as error:
                self.mode, self.reason = 'HALTED_CONFIG', str(error)
                self._event(at, 'GRID_REJECTED', reason=error)

    def run(self, frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, *,
            trend_frame=None, buy_signal_frame=None):
        indicators = prepare_indicators(frame, self.c.execution_timeframe, self.c.indicator_timeframe,
                                        trend_timeframe=self.c.trend_timeframe,
                                        trend_period=self.c.trend_period, trend_frame=trend_frame)
        step = TIMEFRAMES[self.c.execution_timeframe]
        test = frame.loc[(frame.index >= start) & (frame.index < end)]
        if test.empty:
            raise ValueError('No candles in the requested test window.')
        buy_signals = None
        if self.c.buy_trend_policy == 'production' and self.c.trend_filter:
            if buy_signal_frame is None:
                raise ValueError('Production BUY policy requires Yahoo daily buy_signal_frame '
                                 '(CLI: --buy-signal-csv for offline replay).')
            buy_signals = prepare_buy_signals(buy_signal_frame)
            required = pd.date_range(test.index[0].normalize(), test.index[-1].normalize(), freq='D')
            if buy_signals.reindex(required).isna().any().any():
                raise ValueError('Yahoo BUY signal needs 200 completed days and coverage through every test day.')
            buy_signals = buy_signals.reindex(test.index, method='ffill')
        if self.c.trend_filter and trend_frame is not None:
            trend = validate_candles(trend_frame, self.c.trend_timeframe)
            trend_step = TIMEFRAMES[self.c.trend_timeframe]
            first_needed = start.floor(trend_step) - trend_step * self.c.trend_period
            last_needed = test.index[-1].floor(trend_step) - trend_step
            if trend.index[0] > first_needed or trend.index[-1] < last_needed:
                raise ValueError('Trend data must cover SMA warm-up and every closed trend bar through the test end.')
        first = indicators.loc[test.index[0]]
        if not math.isfinite(first.atr) or (self.c.trend_filter and not math.isfinite(first.macro_sma)):
            raise ValueError(f'Insufficient closed macro history: need {self.c.trend_period} '
                             f'closed {self.c.trend_timeframe} bars for SMA, and 15 ATR bars.')
        for bar_index, row in enumerate(test.itertuples()):
            at = row.Index
            signal = indicators.loc[at]
            self.macro_sma = decimal(signal.macro_sma) if math.isfinite(signal.macro_sma) else None
            if buy_signals is not None:
                buy_signal = buy_signals.loc[at]
                self.buy_macro_close = decimal(buy_signal.buy_close)
                self.buy_macro_sma = decimal(buy_signal.buy_sma)
            prices = {key: decimal(getattr(row, key)) for key in ('open', 'high', 'low', 'close')}
            self.mark(at, prices['open'], 'bar_open_before_fills')
            # At an opening gap, pre-existing marketable exchange limits may
            # execute before the engine can cancel them. Fill them at their
            # limits (no favorable gap price improvement), then check the stop.
            stops_before_open = self.stop_count
            marketable = [o for o in self.orders.values()
                          if (o.side == 'BUY' and prices['open'] <= o.price)
                          or (o.side == 'SELL' and prices['open'] >= o.price)]
            for order in sorted(marketable, key=lambda o: o.price, reverse=True):
                self._fill(order, at)
            self._point(prices['open'], at)
            halted_at_open = self.stop_count > stops_before_open
            atr = signal.atr
            if math.isfinite(atr) and atr > 0 and not halted_at_open:
                self._restart(at, prices['open'], decimal(atr))
            route = ['open', 'high', 'low', 'close'] if self.path == 'ohlc' else ['open', 'low', 'high', 'close']
            for i in range(3):
                self._segment(prices[route[i]], prices[route[i + 1]],
                              at + step * i / 3, at + step * (i + 1) / 3)
            if self.cash < -EPS or self.free_quote() < -EPS:
                raise AssertionError('Negative cash/free quote after candle.')
            self.state_bar_counts[self.mode] = self.state_bar_counts.get(self.mode, 0) + 1
            if self.buy_paused and self.mode in ('RUNNING', 'SIZING_PAUSED', CAP_EXHAUSTED):
                self.buy_pause_bars += 1
            if bar_index and bar_index % 20_000 == 0:
                print(f'{self.path}: {bar_index:,}/{len(test):,} execution bars; '
                      f'{at.isoformat()}, {self.mode}, stops={self.stop_count}', file=sys.stderr, flush=True)
        last_price = decimal(test.close.iloc[-1])
        self.mark(end, last_price, 'end_mark_no_forced_sale')
        last = self.curve[-1]
        sell_fills = [fill for fill in self.fills if fill['side'] == 'SELL']
        wins = sum(decimal(fill['realized_pnl']) > 0 for fill in sell_fills)
        return {
            'path': self.path, 'restart_after_stop': self.c.restart_after_stop,
            'execution_timeframe': self.c.execution_timeframe,
            'indicator_timeframe': self.c.indicator_timeframe,
            'trend_timeframe': self.c.trend_timeframe, 'trend_period': self.c.trend_period,
            'stop_mode': self.c.stop_mode,
            'stop_gap_pct': float(self.c.stop_gap * 100) if self.c.stop_mode != 'none' else None,
            'hard_stop_enabled': self.c.stop_mode != 'none',
            'allocation_policy': self.c.allocation_policy,
            'replacement_buy_policy': self.c.replacement_buy_policy,
            'atr_multiple': float(self.c.atr_multiple),
            'residual_buy_count': self.residual_buy_count,
            'cap_exhausted_at': self.cap_exhausted_at.isoformat() if self.cap_exhausted_at is not None else None,
            'budget_buy_resizes': self.budget_buy_resizes,
            'budget_buy_deferrals': self.budget_buy_deferrals,
            'budget_deferred_lanes_at_end': sorted(self.budget_deferred_lanes),
            'allocation_cap_buy_pauses': self.allocation_cap_buy_pauses,
            'allocation_cap_reanchor_deferrals': self.allocation_cap_reanchor_deferrals,
            'buy_trend_policy': ('completed_Yahoo_daily_close_gt_Yahoo_SMA200'
                                 if self.c.buy_trend_policy == 'production' else
                                 'pause_resting_and_new_buys' if self.c.stop_mode == 'none' else 'entry_only'),
            'buy_pause_episodes': self.buy_pause_episodes, 'buy_resume_episodes': self.buy_resume_episodes,
            'buy_pause_bars': self.buy_pause_bars, 'sma_canceled_buys': self.sma_canceled_buys,
            'final_buy_paused': self.buy_paused,
            'trend_filter': self.c.trend_filter, 'cooldown_hours': float(self.c.cooldown_hours),
            'entry_trend_blocked_bars': self.entry_trend_blocked_bars,
            'cooldown_blocked_bars': self.cooldown_blocked_bars,
            'reanchor_trend_blocks': self.reanchor_trend_blocks,
            'reanchor_floor_deferrals': self.reanchor_floor_deferrals,
            'state_bar_counts': self.state_bar_counts,
            'minimum_installed_grid_return_pct': (float(self.minimum_installed_grid_return * 100)
                                                   if self.minimum_installed_grid_return is not None else None),
            'start_utc': start.isoformat(), 'end_exclusive_utc': end.isoformat(),
            'candles': len(test), 'initial_capital': float(self.c.capital),
            'btc_start_price': float(test.open.iloc[0]), 'btc_end_price': float(test.close.iloc[-1]),
            'btc_price_return_pct': (float(test.close.iloc[-1]) / float(test.open.iloc[0]) - 1) * 100,
            'total_realized_pnl': float(self.realized),
            'hard_stop_realized_pnl': float(sum((decimal(f['realized_pnl']) for f in self.fills
                                                if f['kind'] == 'HARD_STOP'), ZERO)),
            'ending_equity': last['equity'], 'unrealized_pnl': last['unrealized_pnl'],
            'total_return_pct': (last['equity'] / float(self.c.capital) - 1) * 100,
            'max_drawdown_pct': float(self.max_dd * 100),
            'max_drawdown_usdt': float(self.max_dd_quote),
            'total_trades': len(self.fills), 'buy_fills': len(self.fills) - len(sell_fills),
            'sell_fills': len(sell_fills), 'profitable_sell_fills': wins,
            'hard_stop_triggers': self.stop_count, 'breakout_reanchors': self.breakout_count,
            'fees_quote_value': float(self.fees), 'ending_cash': float(self.cash),
            'ending_bot_btc': float(self.held),
            'ending_position_cost': float(self.position_cost()),
            'ending_buy_commitment': float(self.buy_commitment()),
            'ending_position_plus_buy_cost': float(self.position_cost() + self.buy_commitment()),
            'reserved_dust_btc': float(sum((lot.quantity for lot in self.reserves), ZERO)),
            'final_state': self.mode, 'final_reason': self.reason,
        }


def validate_candles(frame: pd.DataFrame, timeframe='5m') -> pd.DataFrame:
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError('Candles require a UTC-aware DatetimeIndex.')
    frame = frame.sort_index().copy()
    if frame.empty or frame.index.has_duplicates:
        raise ValueError('Empty data or duplicate candle timestamps.')
    frame = frame[['open', 'high', 'low', 'close', 'volume']].apply(pd.to_numeric, errors='raise')
    if (frame.isna().any().any() or frame.isin([math.inf, -math.inf]).any().any()
            or (frame[['open', 'high', 'low', 'close']] <= 0).any().any()
            or (frame.volume < 0).any() or (frame.high < frame[['open', 'low', 'close']].max(axis=1)).any()
            or (frame.low > frame[['open', 'high', 'close']].min(axis=1)).any()):
        raise ValueError('Invalid OHLCV values or impossible high/low ordering.')
    step = TIMEFRAMES[timeframe]
    expected = pd.date_range(frame.index[0], frame.index[-1], freq=step)
    if not frame.index.equals(expected) or (frame.index != frame.index.floor(step)).any():
        raise ValueError(f'Missing/misaligned {timeframe} candles; refusing to invent fills across data gaps.')
    return frame


def read_candles(path: Path, timeframe='5m') -> pd.DataFrame:
    raw = pd.read_csv(path)
    if 'timestamp' not in raw:
        raise ValueError('CSV must have timestamp, open, high, low, close, volume columns.')
    numeric = pd.to_numeric(raw.timestamp, errors='coerce')
    timestamp = (pd.to_datetime(numeric, unit='ms', utc=True) if numeric.notna().all()
                 else pd.to_datetime(raw.timestamp, utc=True, format='mixed'))
    return validate_candles(raw.drop(columns='timestamp').set_axis(pd.DatetimeIndex(timestamp), axis=0), timeframe)


def yahoo_candles(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize one Yahoo ticker without importing live code or reading .env."""
    if isinstance(raw.columns, pd.MultiIndex):
        if set(raw.columns.get_level_values(1)) != {'BTC-USD'}:
            raise ValueError('Ambiguous Yahoo ticker columns.')
        raw = raw.xs('BTC-USD', axis=1, level=1)
    raw = raw.rename(columns=lambda key: str(key).lower()).copy()
    if 'timestamp' in raw:
        dates = pd.to_datetime(raw.pop('timestamp'), utc=True, format='mixed')
    else:
        dates = pd.to_datetime(raw.index, utc=True)
    if 'volume' not in raw:
        raw['volume'] = 0  # Only prices feed the daily SMA; older saved CSVs lack volume.
    return validate_candles(raw.set_axis(pd.DatetimeIndex(dates), axis=0), '1d')


def read_buy_signal_candles(path: Path) -> pd.DataFrame:
    return yahoo_candles(pd.read_csv(path))


def fetch_buy_signal_candles(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Public BTC-USD daily history for the production ongoing BUY policy."""
    import yfinance as yf
    raw = yf.download('BTC-USD', start=start.date().isoformat(), end=end.date().isoformat(),
                      interval='1d', auto_adjust=False, progress=False, threads=False,
                      timeout=10, multi_level_index=False)
    if raw is None or raw.empty:
        raise ValueError('Yahoo returned no daily BTC-USD BUY signal history.')
    return yahoo_candles(raw)


def fetch_candles(start: pd.Timestamp, end: pd.Timestamp, timeframe='5m') -> pd.DataFrame:
    """Public production Spot history, paginated; no keys, no Testnet resets."""
    exchange = ccxt.binance({'enableRateLimit': True, 'timeout': 20_000,
                            'options': {'defaultType': 'spot', 'fetchMarkets': {'types': ['spot']}}})
    cursor, finish = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    step_ms = int(TIMEFRAMES[timeframe].total_seconds() * 1000)
    rows = {}
    while cursor < finish:
        for attempt in range(5):
            try:
                page = exchange.fetch_ohlcv('BTC/USDT', timeframe, since=cursor,
                                            limit=1000, params={'endTime': finish - 1})
                break
            except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
                if attempt == 4:
                    raise
                time.sleep(60 * (attempt + 1))
            except ccxt.NetworkError:
                if attempt == 4:
                    raise
                time.sleep(min(2 ** attempt, 16))
        if not page:
            raise ValueError('Binance returned no candles before the requested end; coverage incomplete.')
        for row in page:
            if cursor <= row[0] < finish:
                rows[row[0]] = row
        next_cursor = int(page[-1][0]) + step_ms
        if next_cursor <= cursor:
            raise ValueError('OHLCV pagination did not advance.')
        cursor = next_cursor
        print(f'Fetched {len(rows):,} closed {timeframe} candles...', file=sys.stderr, flush=True)
    raw = pd.DataFrame(sorted(rows.values()), columns=COLUMNS)
    index = pd.to_datetime(raw.pop('timestamp'), unit='ms', utc=True)
    return validate_candles(raw.set_axis(pd.DatetimeIndex(index), axis=0), timeframe)


def utc_hour(value, timeframe='1h'):
    stamp = pd.Timestamp(value)
    stamp = stamp.tz_localize('UTC') if stamp.tz is None else stamp.tz_convert('UTC')
    if stamp != stamp.floor(TIMEFRAMES[timeframe]):
        raise ValueError(f'Explicit start/end times must align to UTC {timeframe} bars.')
    return stamp


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--start', help='UTC execution-bar boundary; optional override of --months')
    parser.add_argument('--end', help='Exclusive UTC execution-bar boundary; defaults to latest closed bar')
    parser.add_argument('--levels', type=int, default=8)
    parser.add_argument('--execution-timeframe', choices=['1m', '5m', '1h'], default='5m',
                        help='Fill resolution; 1h retained only for legacy comparisons')
    parser.add_argument('--indicator-timeframe', choices=['1h', '4h'], default='1h',
                        help='ATR timeframe; independent of the daily trend filter')
    parser.add_argument('--trend-timeframe', choices=['1h', '4h', '1d'], default='1d',
                        help='Closed trend-candle timeframe; default daily')
    parser.add_argument('--trend-period', type=int, default=200, help='Closed trend bars for SMA; default 200 (50 supported)')
    parser.add_argument('--trend-csv', type=Path, help='Separate closed trend OHLCV CSV; required for offline daily mode')
    parser.add_argument('--buy-trend-policy', choices=['production', 'legacy'], default='production',
                        help='Production: completed Yahoo daily close/SMA200 gates ongoing BUYs; legacy reproduces older studies')
    parser.add_argument('--allocation-policy', choices=['total_cost_cap', 'legacy_cash'], default='total_cost_cap',
                        help='Default: tracked BTC purchase cost + outstanding BUY notionals capped by allocation; legacy_cash reproduces older sizing')
    parser.add_argument('--replacement-buy-policy', choices=['fixed', 'residual', 'budget_aware'], default='fixed',
                        help='fixed: prior baseline; residual: one final capped BUY then latched CAP_EXHAUSTED; budget_aware: rejected research comparison only')
    parser.add_argument('--atr-multiple', type=D, default=D('2'), help='Research half-width multiplier; live ATR proposal remains 2')
    parser.add_argument('--buy-signal-csv', type=Path,
                        help='Yahoo BTC-USD daily OHLCV including 200-day warm-up; required for offline production policy')
    parser.add_argument('--stop-gap-pct', type=D, default=D('15'), help='Percent below lower bound; default 15')
    parser.add_argument('--stop-mode', choices=['none', 'fixed', 'trailing'], default='fixed',
                        help='Default fixed: 15%% catastrophe stop, no trailing; none/trailing are research comparisons')
    parser.add_argument('--cooldown-hours', type=D, default=D('8'),
                        help='Minimum elapsed time since stop, AND price must exceed the selected SMA; 0 for ablation')
    parser.add_argument('--no-trend-filter', action='store_true', help='Disable SMA only for explicit ablation comparisons')
    parser.add_argument('--min-grid-spacing-pct', type=D, default=D('0.8'),
                        help='Minimum gross SELL/BUY return per rounded level, percent; must be >=0.8')
    parser.add_argument('--capital', type=D, default=D('500'))
    parser.add_argument('--fee', type=D, default=D('0.001'), help='Fraction per execution; 0.001 = 0.1%%')
    parser.add_argument('--buy-fee-asset', choices=['base', 'quote'], default='base')
    parser.add_argument('--slippage-bps', type=D, default=D('10'), help='Adverse MARKET fills only; default 10 bps')
    parser.add_argument('--price-tick', type=D, default=D('0.01'))
    parser.add_argument('--amount-step', type=D, default=D('0.00001'))
    parser.add_argument('--min-notional', type=D, default=D('5'), help='MARKET minimum; grid orders also require >=7 USDT')
    parser.add_argument('--path', choices=['both', 'ohlc', 'olhc'], default='both')
    parser.add_argument('--restart-after-stop', action='store_true', help='Meta-strategy: acknowledge dust/reinvest only after cooldown AND SMA gate')
    parser.add_argument('--breakout-hours', type=D, default=D('4'), help='Continuous above-grid re-anchor; 0 disables this model assumption')
    parser.add_argument('--csv', type=Path, help='Use local OHLCV CSV; no network requests')
    parser.add_argument('--output', type=Path, default=Path('backtest_results'))
    args = parser.parse_args(argv)
    try:
        if args.months < 12 and not args.start:
            raise ValueError('Use at least --months 12, or specify an explicit --start for a custom study.')
        if not args.min_grid_spacing_pct.is_finite() or not D('0.8') <= args.min_grid_spacing_pct < 100:
            raise ValueError('--min-grid-spacing-pct must be finite and at least 0.8, below 100.')
        step = TIMEFRAMES[args.execution_timeframe]
        end = utc_hour(args.end, args.execution_timeframe) if args.end else pd.Timestamp.now(tz='UTC').floor(step)
        if end > pd.Timestamp.now(tz='UTC').floor(step):
            raise ValueError('End cannot include an unfinished or future candle.')
        start = utc_hour(args.start, args.execution_timeframe) if args.start else end - pd.DateOffset(months=args.months)
        if start >= end:
            raise ValueError('Start must precede end.')
        macro_step = TIMEFRAMES[args.indicator_timeframe]
        independent_trend = args.trend_timeframe != args.indicator_timeframe or args.trend_csv is not None
        warmup = start.floor(macro_step) - macro_step * max(50, args.trend_period if not independent_trend else 15)
        config = Config(capital=args.capital, levels=args.levels, fee=args.fee,
                        buy_fee_asset=args.buy_fee_asset, slippage_bps=args.slippage_bps,
                        price_tick=args.price_tick, amount_step=args.amount_step,
                        min_notional=args.min_notional, restart_after_stop=args.restart_after_stop,
                        breakout_hours=args.breakout_hours, execution_timeframe=args.execution_timeframe,
                        indicator_timeframe=args.indicator_timeframe, cooldown_hours=args.cooldown_hours,
                        trend_filter=not args.no_trend_filter, min_grid_return=args.min_grid_spacing_pct / 100,
                        trend_timeframe=args.trend_timeframe, trend_period=args.trend_period,
                        stop_gap=args.stop_gap_pct / 100, stop_mode=args.stop_mode,
                        buy_trend_policy=args.buy_trend_policy, allocation_policy=args.allocation_policy,
                        replacement_buy_policy=args.replacement_buy_policy, atr_multiple=args.atr_multiple)
        frame = read_candles(args.csv, args.execution_timeframe) if args.csv else fetch_candles(warmup, end, args.execution_timeframe)
        frame = validate_candles(frame.loc[(frame.index >= warmup) & (frame.index < end)], args.execution_timeframe)
        if frame.index[0] != warmup or frame.index[-1] != end - step:
            raise ValueError('Execution data must cover the complete test window and ATR/selected trend warm-up.')
        trend_frame, trend_source = None, None
        if config.trend_filter and independent_trend:
            trend_step = TIMEFRAMES[config.trend_timeframe]
            trend_start = start.floor(trend_step) - trend_step * config.trend_period
            trend_end = end.floor(trend_step)
            if args.csv and args.trend_csv is None:
                raise ValueError('Offline execution --csv requires --trend-csv for an independent daily trend; no network request was made.')
            trend_frame = (read_candles(args.trend_csv, config.trend_timeframe) if args.trend_csv
                           else fetch_candles(trend_start, trend_end, config.trend_timeframe))
            trend_frame = validate_candles(trend_frame.loc[(trend_frame.index >= trend_start)
                                                          & (trend_frame.index < trend_end)], config.trend_timeframe)
            if trend_frame.index[0] != trend_start or trend_frame.index[-1] != trend_end - trend_step:
                raise ValueError('Trend data must cover the complete daily/selected SMA warm-up and closed-bar interval.')
            trend_source = str(args.trend_csv.resolve()) if args.trend_csv else 'Binance production public Spot via CCXT'
        buy_frame, buy_source = None, None
        if config.buy_trend_policy == 'production' and config.trend_filter:
            if args.csv and args.buy_signal_csv is None:
                raise ValueError('Offline production BUY policy requires --buy-signal-csv; no Yahoo request was made.')
            buy_start = start.normalize() - TIMEFRAMES['1d'] * 200
            # Fetch through yesterday for the final execution day, end exclusive.
            buy_end = (end - step).normalize()
            buy_frame = (read_buy_signal_candles(args.buy_signal_csv) if args.buy_signal_csv
                         else fetch_buy_signal_candles(buy_start, buy_end))
            buy_frame = buy_frame.loc[(buy_frame.index >= buy_start) & (buy_frame.index < buy_end)]
            buy_frame = validate_candles(buy_frame, '1d')
            if buy_frame.index[0] != buy_start or buy_frame.index[-1] != buy_end - TIMEFRAMES['1d']:
                raise ValueError('Yahoo BUY data must cover the complete SMA200 warm-up and closed-day interval.')
            buy_source = str(args.buy_signal_csv.resolve()) if args.buy_signal_csv else 'Yahoo Finance BTC-USD via yfinance'
        elif args.buy_signal_csv is not None:
            raise ValueError('--buy-signal-csv requires the production policy and an enabled trend filter.')
        args.output.mkdir(parents=True, exist_ok=True)
        data_path = args.output / f'btc_usdt_{args.execution_timeframe}.csv'
        protected = {p.resolve() for p in (args.csv, args.trend_csv, args.buy_signal_csv) if p is not None}
        if data_path.resolve() in protected:
            data_path = args.output / 'data_snapshot.csv'
            if data_path.resolve() in protected:
                raise ValueError('Output snapshot must not overwrite the input CSV.')
        trend_path = None
        if trend_frame is not None:
            trend_path = args.output / f'btc_usdt_{config.trend_timeframe}_trend.csv'
            if trend_path.resolve() in protected:
                trend_path = args.output / 'trend_snapshot.csv'
                if trend_path.resolve() in protected:
                    raise ValueError('Trend snapshot must not overwrite either input CSV.')
            trend_frame.to_csv(trend_path, index_label='timestamp')
        frame.to_csv(data_path, index_label='timestamp')
        buy_path = None
        if buy_frame is not None:
            buy_path = args.output / 'btc_usd_1d_buy_signal.csv'
            if buy_path.resolve() in protected:
                buy_path = args.output / 'buy_signal_snapshot.csv'
                if buy_path.resolve() in protected:
                    raise ValueError('BUY signal snapshot must not overwrite an input CSV.')
            buy_frame.to_csv(buy_path, index_label='timestamp')
        reports = []
        for path in (['ohlc', 'olhc'] if args.path == 'both' else [args.path]):
            simulator = GridBacktest(config, path)
            report = simulator.run(frame, start, end, trend_frame=trend_frame, buy_signal_frame=buy_frame)
            reports.append(report)
            pd.DataFrame(simulator.fills, columns=['timestamp', 'side', 'kind', 'level', 'price',
                                                  'quantity', 'fee_quote_value', 'realized_pnl']).to_csv(
                args.output / f'trades_{path}.csv', index=False)
            pd.DataFrame(simulator.curve).to_csv(args.output / f'equity_{path}.csv', index=False)
            pd.DataFrame(simulator.events).to_csv(args.output / f'events_{path}.csv', index=False)
        summary = {'config': {key: str(value) if isinstance(value, D) else value
                              for key, value in asdict(config).items()},
                   'data_source': str(args.csv.resolve()) if args.csv else 'Binance production public Spot via CCXT',
                   'data_file': data_path.name,
                   'data_sha256': hashlib.sha256(data_path.read_bytes()).hexdigest(),
                   'trend_source': trend_source,
                   'trend_file': trend_path.name if trend_path is not None else None,
                   'trend_sha256': hashlib.sha256(trend_path.read_bytes()).hexdigest() if trend_path is not None else None,
                   'buy_signal_source': buy_source,
                   'buy_signal_file': buy_path.name if buy_path is not None else None,
                   'buy_signal_sha256': hashlib.sha256(buy_path.read_bytes()).hexdigest() if buy_path is not None else None,
                   'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   'geometry_sha256': hashlib.sha256(Path(__file__).with_name('grid_geometry.py').read_bytes()).hexdigest(),
                   'ccxt_version': ccxt.__version__, 'pandas_version': pd.__version__,
                   'assumptions': ['Fixed full-fill OHLC paths, not tick replay or proven profit bounds.',
                                   'Default total_cost_cap: tracked BTC cost basis plus remaining BUY notionals <= campaign allocation; excludes future fees, price appreciation and outside reserves.',
                                   'Cap checks run before seed/plans, re-anchors and each new BUY; existing accepted fills are accounted even if quote fees push cost above cap.',
                                   'Cap-rejected re-anchors defer under the atomic replacement model; cap-rejected new BUYs cause a sizing pause retaining SELLs and the hard stop.',
                                   'Opt-in budget_aware research policy shrinks replacement BUYs to remaining cost/cash, reserving quote BUY fees, or defers only that lane when BUY/reverse SELL minimums cannot fit; seed/re-anchor plans stay fixed.',
                                   'legacy_cash allocation is an explicit earlier research model, independent of the selected BUY trend policy.',
                                   'ATR and SMA use independent CLOSED candle clocks; each signal available only at its own candle close.',
                                   'Selected SMA gates initial entry/restart/re-anchor; elapsed cooldown AND trend required after stop (unless explicit ablation).',
                                   'ATR bounds widened to minimum gross per-cycle return after price rounding; not a net profit guarantee.',
                                   'Infeasible auto re-anchors defer without lowering protective stops or cancelling the old grid.',
                                   'No manual pauses, manual re-anchors or external wallet inventory.',
                                   'Constant tick/lot/notional filters over history; no queue/latency/partial-fill model.',
                                   'Default fixed stop: 15% below lower bound, no trailing; manual recovery after liquidation.',
                                   'Production BUY policy: completed Yahoo BTC-USD close must exceed its closed daily SMA200; SELLs and fixed stop remain active while BUYs pause.',
                                   'Legacy policy reproduces earlier entry-only fixed/trailing and price-crossing no-stop comparisons.',
                                   'Yahoo history assumes complete daily publication at UTC midnight; feed outages are not modeled.',
                                   'Explicit no-stop mode: no forced MARKET SELL; inventory remains marked.',
                                   'No-stop/trailing modes and automatic restart are explicit research comparisons, not production policy.',
                                   'Price stops run only with positive tracked BTC; dust is not silently discarded.',
                                   '4h breakout continuity inferred from each OHLC path; not observed ticker history.',
                                   'No forced end-of-test liquidation; unrealized inventory is marked to market.'],
                   'results': reports}
        (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
        metrics = ['path', 'total_realized_pnl', 'ending_equity', 'unrealized_pnl', 'max_drawdown_pct',
                   'total_trades', 'hard_stop_triggers', 'hard_stop_realized_pnl', 'final_state']
        print(pd.DataFrame(reports)[metrics].to_string(index=False, float_format=lambda value: f'{value:,.4f}'))
        print(f'\nReports: {args.output.resolve()}')
        print('OHLC sensitivity scenarios are estimates, not proof of a statistical edge.')
        return 0
    except (ValueError, OSError, ccxt.BaseError) as error:
        print(f'Backtest failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
