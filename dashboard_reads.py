"""Bounded, single-flight account snapshots for non-essential dashboard reads.

Trading/risk decisions never consume this cache. A separate CCXT client prevents
dashboard timeouts from holding the engine's exchange lock.
"""
from copy import deepcopy
from threading import Lock
import logging
import time
from typing import Any, Callable

import ccxt
from exchange_handler import check_exchange_circuit, exchange_retry_delay, network_backoff

LOGGER = logging.getLogger(__name__)


class DashboardReadDeferred(ccxt.NetworkError):
    """A refresh is already running, invalidated, or waiting for its cooldown."""


class DashboardReads:
    def __init__(self, exchange: Any, *, ttl: float = 5,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.exchange = exchange
        self.ttl = ttl
        self.clock = clock
        self._lock = Lock()
        self._snapshot = None
        self._symbol = None
        self._expires = 0.0
        self._retry_at = 0.0
        self._failures = 0
        self._inflight = False
        self._epoch = 0

    def invalidate(self) -> None:
        with self._lock:
            self._epoch += 1
            self._snapshot = None
            self._expires = 0

    def snapshot(self, symbol: str) -> dict:
        with self._lock:
            now = self.clock()
            if self._snapshot is not None and self._symbol == symbol and now < self._expires:
                return deepcopy(self._snapshot)
            if self._inflight or now < self._retry_at:
                raise DashboardReadDeferred('Dashboard account refresh pending; retry later.')
            self._inflight = True
            epoch = self._epoch
        try:
            check_exchange_circuit(self.exchange)
            self.exchange.load_markets()
            # Exactly one attempt per refresh. No thread sleeps or queues here.
            balances = self.exchange.fetch_balance({'type': 'spot'})
            orders = self.exchange.fetch_open_orders()
            ticker = self.exchange.fetch_ticker(symbol)
            market = self.exchange.market(symbol)
            if not isinstance(balances, dict) or not isinstance(orders, list) or not isinstance(ticker, dict):
                raise ValueError('Invalid dashboard account snapshot.')
            snapshot = dict(balances=balances, orders=orders, ticker=ticker, market=market)
            with self._lock:
                if epoch != self._epoch:
                    raise DashboardReadDeferred('Account changed during dashboard refresh.')
                self._snapshot = deepcopy(snapshot)
                self._symbol = symbol
                self._expires = self.clock() + self.ttl
                self._retry_at = 0
                self._failures = 0
            return snapshot
        except DashboardReadDeferred:
            raise
        except Exception as error:
            with self._lock:
                delay = exchange_retry_delay(self.exchange, error,
                                            network_backoff(self._failures, cap=12) * 5)
                self._retry_at = self.clock() + delay
                self._failures = min(self._failures + 1, 10)
                self._snapshot = None
            LOGGER.warning('Dashboard account refresh failed (%s); shared retry in %.2fs.',
                           type(error).__name__, delay)
            raise
        finally:
            with self._lock:
                self._inflight = False
