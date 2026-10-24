"""Experimental hourly BUY gate; no order/account writes or trading locks.

Fixed v1 policy, not optimized or established profitable. Only RANGE permits
new inventory. A dedicated public Mainnet client keeps Testnet wicks out of
the hourly classification; execution-price hard stops remain independent.
"""
from dataclasses import asdict, dataclass
import math
import os
from threading import Lock, Thread
import time

POLICY_ID = 'hourly-regime-v1'
HOUR_MS = 3_600_000
HISTORY = 240
PERIOD = 14
REFRESH_SECONDS = 60
MAX_AGE_SECONDS = 180
SLOPE_LIMIT = 0.1  # EMA20 change per hour / current Wilder ATR14, over three hours.


def configured_enabled():
    value = os.getenv('GRID_REGIME_FILTER_ENABLED', 'false').strip().lower()
    if value not in ('true', 'false', '1', '0'):
        raise ValueError('GRID_REGIME_FILTER_ENABLED must be true/false or 1/0.')
    return value in ('true', '1')


@dataclass(frozen=True)
class RegimeSnapshot:
    closed_at_ms: int
    regime: str
    adx: float
    plus_di: float
    minus_di: float
    atr: float
    ema20_slope_atr: float


def classify(adx, plus_di, minus_di, slope):
    """Ambiguous readings block BUYs instead of guessing a direction."""
    if not all(math.isfinite(x) for x in (adx, plus_di, minus_di, slope)):
        raise ValueError('Non-finite regime indicators.')
    if not all(0 <= x <= 100 for x in (adx, plus_di, minus_di)):
        raise ValueError('Directional indicators outside 0..100.')
    if adx < 20 and abs(slope) <= SLOPE_LIMIT:
        return 'RANGE'
    if adx >= 25:
        if minus_di > plus_di and slope < -SLOPE_LIMIT:
            return 'DOWNTREND'
        if plus_di > minus_di and slope > SLOPE_LIMIT:
            return 'UPTREND'
    return 'TRANSITION'


def calculate_regime(candles, now_ms):
    """Wilder-seeded ADX14/DMI/ATR14 and SMA-seeded EMA20; closed bars only.

    Exactly the latest 240 consecutive UTC hours are used for deterministic
    restarts. No forming-bar OHLC or future prices enter any indicator.
    """
    end = int(now_ms) // HOUR_MS * HOUR_MS
    by_time = {}
    for row in candles:
        if not isinstance(row, (list, tuple)) or len(row) < 6 or isinstance(row[0], bool):
            raise ValueError('Malformed regime candle.')
        stamp = int(row[0])
        if stamp != float(row[0]) or stamp % HOUR_MS or stamp in by_time:
            raise ValueError('Duplicate/misaligned regime candle.')
        by_time[stamp] = row
    bars = []
    for stamp in range(end - HISTORY * HOUR_MS, end, HOUR_MS):
        if stamp not in by_time:
            raise ValueError('Need 240 consecutive completed hourly candles.')
        if any(isinstance(value, bool) for value in by_time[stamp][1:6]):
            raise ValueError('Boolean hourly OHLCV is invalid.')
        o, h, l, c, v = map(float, by_time[stamp][1:6])
        if (not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)) or
                not math.isfinite(v) or v < 0 or h < max(o, l, c) or l > min(o, h, c)):
            raise ValueError('Invalid completed hourly OHLCV.')
        bars.append((h, l, c))
    trs, plus, minus = [], [], []
    for prior, current in zip(bars, bars[1:]):
        h, l, _ = current
        up, down = h - prior[0], prior[1] - l
        trs.append(max(h - l, abs(h - prior[2]), abs(l - prior[2])))
        plus.append(up if up > down and up > 0 else 0.)
        minus.append(down if down > up and down > 0 else 0.)
    st, sp, sm = (sum(values[:PERIOD]) for values in (trs, plus, minus))
    dxs, adx = [], None
    for i in range(PERIOD, len(bars)):
        if i > PERIOD:
            st, sp, sm = (st - st / PERIOD + trs[i-1],
                          sp - sp / PERIOD + plus[i-1],
                          sm - sm / PERIOD + minus[i-1])
        dx = 100 * abs(sp-sm) / (sp+sm) if st > 0 and sp+sm > 0 else 0.
        if adx is None:
            dxs.append(dx)
            if len(dxs) == PERIOD:
                adx = sum(dxs) / PERIOD
        else:
            adx = (adx * (PERIOD-1) + dx) / PERIOD
    atr = st / PERIOD
    if atr <= 0:
        raise ValueError('Zero hourly ATR cannot normalize the regime slope.')
    positive, negative = 100 * sp / st, 100 * sm / st
    closes = [bar[2] for bar in bars]
    ema = sum(closes[:20]) / 20
    values = [ema]
    for close in closes[20:]:
        ema += 2 / 21 * (close - ema)
        values.append(ema)
    slope = (values[-1] - values[-4]) / (3 * atr)
    return RegimeSnapshot(end, classify(adx, positive, negative, slope),
                          adx, positive, negative, atr, slope)


def public_fetcher(symbol):
    """Lazy, isolated, credential-free client; never share the trading lock."""
    client = None

    def fetch():
        nonlocal client
        from exchange_handler import create_macro_exchange, exchange_request
        if client is None:
            client = create_macro_exchange()
        rows = exchange_request(client.public_get_klines, {
            'symbol': symbol.replace('/', ''), 'interval': '1h', 'limit': HISTORY + 1,
        })
        return calculate_regime(rows, int(time.time() * 1000))

    return fetch


class RegimeMonitor:
    """Single-flight daemon fetch; a failed/stale refresh blocks BUYs only."""
    def __init__(self, fetch, *, clock=time.monotonic, wall_clock=time.time, launch=None):
        self.fetch, self.clock, self.wall_clock = fetch, clock, wall_clock
        self._launch = launch or (lambda job: Thread(target=job, name='hourly-regime', daemon=True).start())
        self._lock = Lock()
        self._snapshot = self._updated = None
        self._refreshing = False
        self._next_refresh = 0.
        self._failures = 0
        self._error = 'Not fetched yet.'

    def _refresh(self):
        try:
            snapshot = self.fetch()
            if not isinstance(snapshot, RegimeSnapshot):
                raise ValueError('Invalid regime snapshot.')
            if snapshot.closed_at_ms != int(self.wall_clock()*1000)//HOUR_MS*HOUR_MS:
                raise ValueError('Regime snapshot is stale or from the future.')
            if (not math.isfinite(snapshot.atr) or snapshot.atr <= 0 or
                    snapshot.regime != classify(snapshot.adx, snapshot.plus_di,
                                                 snapshot.minus_di, snapshot.ema20_slope_atr)):
                raise ValueError('Invalid regime indicators/classification.')
        except Exception as error:
            with self._lock:
                self._snapshot = self._updated = None
                self._failures += 1
                self._error = type(error).__name__  # No URLs/credentials in API telemetry.
                self._next_refresh = self.clock() + min(300, 30 * 2**min(self._failures-1, 4))
                self._refreshing = False
        else:
            with self._lock:
                self._snapshot, self._updated = snapshot, self.clock()
                self._failures, self._error = 0, None
                # Refresh at the next hour even if the regular TTL ends later.
                boundary = (snapshot.closed_at_ms+HOUR_MS)/1000 - self.wall_clock()
                self._next_refresh = self.clock() + min(REFRESH_SECONDS, max(0, boundary))
                self._refreshing = False

    def status(self):
        """Read-only API projection. Never starts exchange I/O."""
        now, end = self.clock(), int(self.wall_clock()*1000)//HOUR_MS*HOUR_MS
        with self._lock:
            snapshot, updated = self._snapshot, self._updated
            fresh = (snapshot is not None and updated is not None and
                     0 <= now-updated < MAX_AGE_SECONDS and snapshot.closed_at_ms == end)
            return dict(policy_id=POLICY_ID, enabled=True, source='Binance Mainnet Spot',
                        timeframe='1h', available=bool(fresh),
                        buy_allowed=bool(fresh and snapshot.regime == 'RANGE'),
                        reason_code=snapshot.regime if fresh else 'DATA_UNAVAILABLE',
                        snapshot=asdict(snapshot) if snapshot else None,
                        age_seconds=max(0, now-updated) if updated is not None else None,
                        refreshing=self._refreshing, error=self._error)

    def buys_allowed(self):
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
        return self.status()['buy_allowed']
