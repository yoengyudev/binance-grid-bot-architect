"""Non-blocking Yahoo BTC-USD SMA200 BUY gate; no trading credentials or writes."""
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
from threading import Lock, Thread
import time
from typing import Callable

import pandas as pd
import yfinance as yf

LOGGER = logging.getLogger(__name__)
REFRESH_SECONDS = 300
MAX_AGE_SECONDS = 900
RETRY_SECONDS = 30
HOURLY_RECOVERY_DAYS = 7


@dataclass(frozen=True)
class MacroTrendSnapshot:
    close: float
    sma200: float
    candle_day: str
    reconstructed_days: tuple[str, ...] = ()

    @property
    def uptrend(self) -> bool:
        return self.close > self.sma200  # Equality also suspends BUYs.


def _yahoo_closes(frame) -> pd.Series:
    """Normalize one BTC-USD response without filling or deduplicating data."""
    if frame is None or frame.empty:
        raise ValueError('Yahoo returned no BTC-USD price data.')
    close = frame['Close']
    if isinstance(close, pd.DataFrame):
        if list(close.columns) != ['BTC-USD']:
            raise ValueError('Ambiguous Yahoo Close columns.')
        close = close['BTC-USD']
    dates = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True))
    if dates.hasnans or dates.has_duplicates:
        raise ValueError('Yahoo timestamps are missing or duplicated.')
    return pd.Series(pd.to_numeric(close, errors='coerce').to_numpy(), index=dates)


def _recover_daily_closes(close, *, today, retrieved, download):
    """Recover only recent absent daily closes from complete same-feed UTC days.

    A daily close is the final completed hourly close, not an average. Require
    exactly all 24 valid hourly bars; never interpolate or reuse another day.
    Already-valid daily values remain authoritative.
    """
    missing = close.index[close.isna()]
    if missing.empty:
        return ()
    oldest = today - pd.Timedelta(days=HOURLY_RECOVERY_DAYS)
    if (missing < oldest).any():
        raise ValueError('Yahoo SMA200 has missing daily closes outside the hourly recovery window.')
    hourly = _yahoo_closes(download(
        'BTC-USD', start=missing.min().date().isoformat(), end=retrieved.to_pydatetime(),
        interval='1h', auto_adjust=False, progress=False, threads=False,
        timeout=10, multi_level_index=False,
    ))
    recovered = {}
    for day in missing:
        expected = pd.date_range(day, periods=24, freq='h')
        bars = hourly.loc[(hourly.index >= day) & (hourly.index < day + pd.Timedelta(days=1))]
        if (len(bars) != 24 or not bars.index.sort_values().equals(expected)
                or not all(math.isfinite(value) and value > 0 for value in bars)):
            raise ValueError(f'Yahoo hourly recovery needs 24 valid UTC bars for {day.date()}.')
        recovered[day] = float(bars.loc[expected[-1]])
    # Do not partially repair a snapshot when any missing day is unverified.
    for day, value in recovered.items():
        close.loc[day] = value
    return tuple(day.date().isoformat() for day in missing)


def fetch_macro_trend(*, now=None, download=None) -> MacroTrendSnapshot:
    """Fetch a live-ended window; validate only completed UTC daily closes.

    A midnight end can make yfinance cache an incomplete historical response
    indefinitely. Ending at retrieval time uses its live request path. Today's
    forming candle is discarded by the completed-day reindex below. Recent
    blank daily closes may be recovered from 24 verified Yahoo hourly bars.
    """
    retrieved = pd.Timestamp(now if now is not None else datetime.now(timezone.utc)).tz_convert('UTC')
    today = retrieved.normalize()
    start = today - pd.Timedelta(days=250)
    download = download or yf.download
    frame = download(
        'BTC-USD', start=start.date().isoformat(), end=retrieved.to_pydatetime(),
        interval='1d', auto_adjust=False, progress=False, threads=False,
        timeout=10, multi_level_index=False,
    )
    close = _yahoo_closes(frame)
    if not (close.index == close.index.normalize()).all():
        raise ValueError('Yahoo daily timestamps are not UTC midnight.')
    required = pd.date_range(today - pd.Timedelta(days=200), periods=200, freq='D')
    close = close.reindex(required)  # Missing/stale days remain invalid; never forward-fill.
    reconstructed = _recover_daily_closes(close, today=today, retrieved=retrieved, download=download)
    if not all(math.isfinite(value) and value > 0 for value in close):
        raise ValueError('Yahoo SMA200 needs 200 consecutive valid closes through yesterday.')
    sma = float(close.rolling(200, min_periods=200).mean().iloc[-1])
    return MacroTrendSnapshot(float(close.iloc[-1]), sma, required[-1].date().isoformat(), reconstructed)


class MacroTrendMonitor:
    """Single-flight lazy refresh. Trading callers only read memory/start a daemon.

    Refresh every five minutes, retry failures after 30s, and reject any cached
    value older than 15 minutes or from a previous UTC signal day. A failed
    refresh invalidates an earlier positive result immediately.
    """
    def __init__(self, fetch: Callable = fetch_macro_trend, *, clock=time.monotonic,
                 utcnow=lambda: datetime.now(timezone.utc), launch=None):
        self.fetch, self.clock, self.utcnow = fetch, clock, utcnow
        self._launch = launch or (lambda job: Thread(target=job, name='yahoo-sma200', daemon=True).start())
        self._lock = Lock()
        self._snapshot = None
        self._updated = None
        self._next_refresh = 0.0
        self._refreshing = False
        self._last_attempt_at = None
        self._last_success_at = None
        self.error = 'Yahoo SMA200 has not been fetched yet.'

    def _refresh(self):
        with self._lock:
            self._last_attempt_at = self.utcnow().isoformat()
        try:
            result = self.fetch()
            if not isinstance(result, MacroTrendSnapshot):
                raise ValueError('Invalid Yahoo macro snapshot.')
        except Exception as error:
            with self._lock:
                self._snapshot, self._updated = None, None
                self.error = f'{type(error).__name__}: {error}'
                self._next_refresh = self.clock() + RETRY_SECONDS
                self._refreshing = False
            LOGGER.warning('Yahoo SMA200 unavailable; BUY orders suspended: %s', error)
        else:
            with self._lock:
                self._snapshot, self._updated = result, self.clock()
                self._last_success_at = self.utcnow().isoformat()
                self.error = None
                self._next_refresh = self._updated + REFRESH_SECONDS
                self._refreshing = False

    def status(self) -> dict:
        """Read-only telemetry: never fetch or start a worker from API polling."""
        now = self.clock()
        yesterday = (pd.Timestamp(self.utcnow()).tz_convert('UTC').normalize()
                     - pd.Timedelta(days=1)).date().isoformat()
        with self._lock:
            snapshot, updated = self._snapshot, self._updated
            age = max(0., now - updated) if updated is not None else None
            fresh = (snapshot is not None and updated is not None and
                     0 <= now - updated < MAX_AGE_SECONDS and snapshot.candle_day == yesterday)
            available = bool(fresh)
            allowed = bool(fresh and snapshot.uptrend)
            reason = ('ABOVE_SMA200' if allowed else 'BELOW_OR_EQUAL_SMA200' if fresh
                      else 'DATA_UNAVAILABLE' if snapshot is None else 'DATA_STALE')
            return dict(source='Yahoo BTC-USD', available=available, buy_allowed=allowed,
                        reason_code=reason, error=self.error, refreshing=self._refreshing,
                        last_attempt_at=self._last_attempt_at,
                        last_success_at=self._last_success_at, age_seconds=age,
                        candle_day=snapshot.candle_day if snapshot is not None else None,
                        reconstructed_days=list(snapshot.reconstructed_days) if snapshot is not None else [],
                        close=snapshot.close if snapshot is not None else None,
                        sma200=snapshot.sma200 if snapshot is not None else None)

    def get_macro_trend(self) -> bool:
        now = self.clock()
        today = pd.Timestamp(self.utcnow()).tz_convert('UTC').normalize()
        yesterday = (today - pd.Timedelta(days=1)).date().isoformat()
        with self._lock:
            snapshot, updated = self._snapshot, self._updated
            fresh = (snapshot is not None and updated is not None and
                     0 <= now - updated < MAX_AGE_SECONDS and snapshot.candle_day == yesterday)
            # Date rollover refreshes immediately even before the five-minute TTL.
            launch = (not self._refreshing and now >= self._next_refresh) or (
                not self._refreshing and snapshot is not None and snapshot.candle_day != yesterday)
            if launch:
                self._refreshing = True
        if launch:
            try:
                self._launch(self._refresh)  # Never download while holding the snapshot lock.
            except Exception as error:
                with self._lock:
                    self._snapshot, self._updated = None, None
                    self.error = f'Could not start Yahoo refresh: {error}'
                    self._refreshing = False
                    self._next_refresh = now + RETRY_SECONDS
                return False
        return bool(fresh and snapshot.uptrend)


_monitor = MacroTrendMonitor()


def get_macro_trend() -> bool:
    """True only for a fresh Yahoo closing price > SMA200; never waits on HTTP."""
    return _monitor.get_macro_trend()


def get_macro_status() -> dict:
    return _monitor.status()
