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


@dataclass(frozen=True)
class MacroTrendSnapshot:
    close: float
    sma200: float
    candle_day: str

    @property
    def uptrend(self) -> bool:
        return self.close > self.sma200  # Equality also suspends BUYs.


def fetch_macro_trend(*, now=None, download=None) -> MacroTrendSnapshot:
    """Blocking fetch for the background worker, using 250 completed UTC days."""
    today = pd.Timestamp(now if now is not None else datetime.now(timezone.utc)).tz_convert('UTC').normalize()
    start = today - pd.Timedelta(days=250)
    frame = (download or yf.download)(
        'BTC-USD', start=start.date().isoformat(), end=today.date().isoformat(),
        interval='1d', auto_adjust=False, progress=False, threads=False,
        timeout=10, multi_level_index=False,
    )
    if frame is None or frame.empty:
        raise ValueError('Yahoo returned no BTC-USD daily data.')
    # Be tolerant of single-ticker MultiIndex output across yfinance versions.
    close = frame['Close']
    if isinstance(close, pd.DataFrame):
        if list(close.columns) != ['BTC-USD']:
            raise ValueError('Ambiguous Yahoo Close columns.')
        close = close['BTC-USD']
    dates = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True))
    if dates.has_duplicates or not (dates == dates.normalize()).all():
        raise ValueError('Yahoo daily timestamps are duplicated or not UTC midnight.')
    close = pd.Series(pd.to_numeric(close, errors='coerce').to_numpy(), index=dates)
    required = pd.date_range(today - pd.Timedelta(days=200), periods=200, freq='D')
    close = close.reindex(required)  # Missing/stale days remain invalid; never forward-fill.
    if not all(math.isfinite(value) and value > 0 for value in close):
        raise ValueError('Yahoo SMA200 needs 200 consecutive valid closes through yesterday.')
    sma = float(close.rolling(200, min_periods=200).mean().iloc[-1])
    return MacroTrendSnapshot(float(close.iloc[-1]), sma, required[-1].date().isoformat())


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
        self.error = 'Yahoo SMA200 has not been fetched yet.'

    def _refresh(self):
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
                self.error = None
                self._next_refresh = self._updated + REFRESH_SECONDS
                self._refreshing = False

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
