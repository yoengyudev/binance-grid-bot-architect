"""Completed UTC 4h signals; no pandas-ta dependency or forming-bar inputs."""
from dataclasses import dataclass, asdict
import math

HOUR = 3600
FOUR_HOURS = 4 * HOUR


class DataGap(ValueError):
    pass


@dataclass(frozen=True)
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self):
        if (type(self.time) is not int or self.time % HOUR or
                not all(math.isfinite(x) and x > 0 for x in (self.open, self.high, self.low, self.close)) or
                self.low > min(self.open, self.close) or self.high < max(self.open, self.close)):
            raise ValueError('Invalid closed hourly candle')


@dataclass(frozen=True)
class Signal:
    closed_at: int
    close: float
    atr: float
    fast: float
    slow: float
    long: float
    previous_fast: float
    previous_slow: float
    ema_entry: bool
    ema_exit: bool

    @property
    def scheduled_at(self):
        return self.closed_at + HOUR

    def payload(self):
        return dict(asdict(self), scheduled_at=self.scheduled_at)


def indicators(candles, *, allowed_missing=frozenset()):
    """Match the frozen study's SMA-seeded EMA / Wilder ATR recurrences.

    Runtime has no allowed gaps. The historical parity audit may explicitly
    supply the study's documented venue-closure hours. Edge partial 4h groups
    are omitted; any interior gap or noncontiguous input blocks signals.
    """
    if not candles:
        return []
    if any(b.time <= a.time for a, b in zip(candles, candles[1:])):
        raise DataGap('Hourly candles are not unique and ascending')
    groups = {}
    for c in candles:
        groups.setdefault(c.time // FOUR_HOURS * FOUR_HOURS, []).append(c)
    first, last = min(groups), max(groups)
    bars = []
    for t in range(first, last + FOUR_HOURS, FOUR_HOURS):
        group = groups.get(t, [])
        expected = {t + i * HOUR for i in range(4)} - allowed_missing
        actual = {c.time for c in group}
        if actual != expected:
            if t == first and group and group[0].time != t and actual == {x for x in expected if x >= group[0].time}:
                continue
            if t == last and group and actual == {x for x in expected if x <= group[-1].time}:
                continue
            raise DataGap(f'Incomplete UTC 4h candle at {t}')
        if group:
            bars.append((t, group[0].open, max(c.high for c in group), min(c.low for c in group), group[-1].close))
    emas = {20: None, 50: None, 200: None}
    atr, trs, signals = None, [], []
    for i, (t, o, h, l, c) in enumerate(bars):
        previous_close = bars[i - 1][4] if i else o
        trs.append(max(h-l, abs(h-previous_close), abs(l-previous_close)))
        if i == 13:
            atr = sum(trs) / 14
        elif i > 13:
            atr = (atr * 13 + trs[-1]) / 14
        oldfast, oldslow = emas[20], emas[50]
        for n in emas:
            if i == n - 1:
                emas[n] = sum(b[4] for b in bars[:n]) / n
            elif i >= n:
                emas[n] += 2 / (n + 1) * (c - emas[n])
        if i >= 199 and atr and atr > 0:
            signals.append(Signal(t+FOUR_HOURS, c, atr, emas[20], emas[50], emas[200], oldfast, oldslow,
                                  emas[20] > emas[50] and oldfast <= oldslow and c > emas[200],
                                  emas[20] <= emas[50]))
    return signals
