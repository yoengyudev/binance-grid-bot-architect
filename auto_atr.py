"""Experimental BUY-net adaptation; frozen lot exits are owned by the engine."""
from dataclasses import dataclass
from decimal import Decimal as D
import os
from threading import Lock, Thread
import time

from production_baseline import closed_hourly_atr, HOUR_MS, MIN_GRID_RETURN

POLICY_ID = 'hourly-atr-buy-net-v1'
STATE_KEY = 'auto_atr_buy_net'
EXIT_PREFIX = 'auto_atr_exit:'
CANCEL_PREFIX = 'auto_atr_cancel'
COOLDOWN_MS = 4 * HOUR_MS
ATR_CHANGE = D('.25')
MAX_AGE_SECONDS = 180


def configured_enabled():
    flag = os.getenv('GRID_AUTO_ATR_ENABLED', 'false').strip().lower()
    if flag not in ('true', 'false', '1', '0'):
        raise ValueError('GRID_AUTO_ATR_ENABLED must be true/false or 1/0.')
    return flag in ('true', '1')


@dataclass(frozen=True)
class ATRSnapshot:
    closed_at_ms: int
    atr: D
    close: D


def calculate_atr(rows, now_ms):
    atr, close, end = closed_hourly_atr(rows, now_ms)
    return ATRSnapshot(end, atr, close)


def public_fetcher(symbol):
    client = None

    def fetch():
        nonlocal client
        from exchange_handler import create_macro_exchange, exchange_request
        if client is None:
            client = create_macro_exchange()
        rows = exchange_request(client.public_get_klines, {
            'symbol': symbol.replace('/', ''), 'interval': '1h', 'limit': 16,
        })
        return calculate_atr(rows, int(time.time() * 1000))
    return fetch


def make_plan(center, atr, levels, stop, tick, round_price):
    """All BUY lanes below center; each future lot gets its own fee-safe exit."""
    if any(not isinstance(v, D) or not v.is_finite() or v <= 0
           for v in (center, atr, stop, tick)):
        raise ValueError('ATR plan prices must be positive finite Decimals.')
    if not levels or len(set(levels)) != len(levels) or any(
            isinstance(level, bool) or not isinstance(level, int) or level == 0 for level in levels):
        raise ValueError('ATR plan requires distinct nonzero lane identifiers.')
    gap = max(D('.5') * atr / center, MIN_GRID_RETURN)
    lanes = {}
    previous = center
    for index, level in enumerate(levels, start=1):
        price = round_price(center / (1 + gap) ** index)
        target = round_price(price * (1 + gap))
        if target < price * (1 + gap):
            target = round_price(target + tick)
        if not stop < price < previous or target / price - 1 < MIN_GRID_RETURN:
            raise ValueError('ATR BUY net cannot fit fee defense above the retained hard stop.')
        lanes[str(level)] = dict(buy=str(price), sell=str(target))
        previous = price
    return dict(center=str(center), atr=str(atr), gap=str(gap),
                lower=str(previous), upper=str(center * (1 + gap)), lanes=lanes)


def needs_update(previous, snapshot, price):
    if previous is None:
        return True
    if snapshot.closed_at_ms - previous['closed_at_ms'] < COOLDOWN_MS:
        return False
    old_center, old_atr, gap = (D(previous[key]) for key in ('center', 'atr', 'gap'))
    return (abs(price / old_center - 1) >= gap or
            abs(snapshot.atr / old_atr - 1) >= ATR_CHANGE)


class ATRMonitor:
    """Dedicated daemon fetch; status/read never initiate exchange I/O."""
    def __init__(self, fetch, *, clock=time.monotonic, wall_clock=time.time, launch=None):
        self.fetch, self.clock, self.wall_clock = fetch, clock, wall_clock
        self._launch = launch or (lambda job: Thread(target=job, name='auto-atr', daemon=True).start())
        self._lock = Lock()
        self._snapshot = self._updated = None
        self._refreshing = False
        self._next_refresh = 0.
        self._failures = 0
        self._error = 'NotFetched'

    def read(self):
        now, end = self.clock(), int(self.wall_clock() * 1000) // HOUR_MS * HOUR_MS
        with self._lock:
            if (self._snapshot is not None and self._updated is not None and
                    0 <= now - self._updated < MAX_AGE_SECONDS and self._snapshot.closed_at_ms == end):
                return self._snapshot
        return None

    def status(self):
        snapshot = self.read()
        with self._lock:
            return dict(available=snapshot is not None, refreshing=self._refreshing, error=self._error,
                        snapshot=(dict(closed_at_ms=snapshot.closed_at_ms, atr=str(snapshot.atr),
                                       close=str(snapshot.close)) if snapshot else None))

    def _refresh(self):
        try:
            snapshot = self.fetch()
            if (not isinstance(snapshot, ATRSnapshot) or
                    snapshot.closed_at_ms != int(self.wall_clock() * 1000) // HOUR_MS * HOUR_MS or
                    any(not isinstance(v, D) or not v.is_finite() or v <= 0
                        for v in (snapshot.atr, snapshot.close))):
                raise ValueError('Invalid or stale ATR snapshot.')
        except Exception as error:
            with self._lock:
                self._snapshot = self._updated = None
                self._failures += 1
                self._error = type(error).__name__
                self._next_refresh = self.clock() + min(300, 30 * 2 ** min(self._failures - 1, 4))
                self._refreshing = False
        else:
            with self._lock:
                self._snapshot, self._updated = snapshot, self.clock()
                self._failures, self._error = 0, None
                boundary = (snapshot.closed_at_ms + HOUR_MS) / 1000 - self.wall_clock()
                self._next_refresh = self.clock() + min(60, max(0, boundary))
                self._refreshing = False

    def refresh(self):
        with self._lock:
            launch = not self._refreshing and self.clock() >= self._next_refresh
            if launch:
                self._refreshing = True
        if launch:
            try:
                self._launch(self._refresh)
            except Exception:
                with self._lock:
                    self._snapshot = self._updated = None
                    self._error = 'WorkerStartFailed'
                    self._refreshing = False
                    self._next_refresh = self.clock() + 30
        return self.read()
