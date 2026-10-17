"""Environment-isolated Binance Spot client and read-only connection check."""

import json
import logging
import math
import os
import random
import sys
import time
from collections import deque
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock, local
from typing import Any, Callable, Dict, Optional

import ccxt
from trading_environment import get_trading_settings


BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)


class CircuitOpenError(ccxt.RateLimitExceeded):
    """A local rejection: no HTTP request was sent to Binance."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = max(retry_after, 0.1)
        super().__init__(f"Binance API circuit open; retry after {self.retry_after:.1f}s.")


class ApiCircuitBreaker:
    """Share a rolling rate-limit budget and one recovery probe across threads."""

    def __init__(self, *, threshold: int = 3, window_seconds: float = 60,
                 penalty_seconds: float = 60,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if threshold < 1 or window_seconds <= 0 or penalty_seconds <= 0:
            raise ValueError("Circuit breaker limits must be positive.")
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.penalty_seconds = penalty_seconds
        self._clock = clock
        self._lock = Lock()
        self._failures: deque[float] = deque()
        self._open_until = 0.0
        self._probe = False
        self._generation = 0

    def retry_after(self) -> float:
        with self._lock:
            return max(self._open_until - self._clock(), 1.0 if self._probe else 0.0)

    def before_request(self) -> Optional[int]:
        with self._lock:
            now = self._clock()
            if self._open_until > now or self._probe:
                raise CircuitOpenError(max(self._open_until - now, 1.0 if self._probe else 0.0))
            if self._open_until:
                self._probe = True
                return self._generation  # Only one request tests recovery.
            return None

    def rate_limited(self, minimum_penalty: float = 0) -> None:
        if not math.isfinite(minimum_penalty) or minimum_penalty < 0:
            minimum_penalty = 0
        with self._lock:
            now = self._clock()
            while self._failures and self._failures[0] < now - self.window_seconds:
                self._failures.popleft()
            self._failures.append(now)
            if self._open_until or len(self._failures) >= self.threshold or minimum_penalty > 0:
                self._open_until = now + max(self.penalty_seconds, minimum_penalty)
                self._probe = False
                self._generation += 1
                LOGGER.warning("Binance API circuit opened for %.1fs after repeated rate limits.",
                               self._open_until - now)

    def completed(self, probe: Optional[int], *, success: bool) -> None:
        with self._lock:
            # A late response from before a trip cannot close a newer circuit.
            if probe is None or probe != self._generation or not self._probe:
                return
            self._probe = False
            if success:
                self._open_until = 0.0
                self._failures.clear()
            else:
                self._open_until = self._clock() + self.penalty_seconds
                self._generation += 1


# Every factory client in this process uses the same gate, including monitor reads.
API_CIRCUIT_BREAKER = ApiCircuitBreaker()


def install_circuit_breaker(exchange: Any,
                            breaker: Optional[ApiCircuitBreaker] = None) -> Any:
    """Gate CCXT's actual HTTP transport, covering public/private and retry calls."""
    if isinstance(getattr(exchange, "api_circuit_breaker", None), ApiCircuitBreaker):
        return exchange
    breaker = breaker if breaker is not None else API_CIRCUIT_BREAKER
    transport = exchange.fetch
    attempts = local()

    def guarded_fetch(url, method="GET", headers=None, body=None):
        probe = breaker.before_request()
        try:
            if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
                attempts.writes = getattr(attempts, "writes", 0) + 1
            result = transport(url, method, headers, body)
        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
            # Honor a longer server penalty when CCXT supplies Retry-After.
            response_headers = getattr(exchange, "last_response_headers", None) or {}
            try:
                retry_after = response_headers.get("Retry-After", response_headers.get("retry-after", 0))
                try:
                    penalty = float(retry_after)
                except (TypeError, ValueError):
                    penalty = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (TypeError, ValueError, AttributeError):
                penalty = 0
            breaker.rate_limited(penalty)
            raise
        except BaseException:
            breaker.completed(probe, success=False)
            raise
        breaker.completed(probe, success=True)
        return result

    exchange.fetch = guarded_fetch
    exchange.api_circuit_breaker = breaker
    exchange.api_http_attempts = attempts
    return exchange


def exchange_write_attempts(exchange: Any) -> int:
    """Count this thread's write attempts, including CCXT's internal retries."""
    attempts = getattr(exchange, "api_http_attempts", None)
    return getattr(attempts, "writes", 0) if isinstance(attempts, local) else 0


def check_exchange_circuit(exchange: Any) -> None:
    """Reject known cooldowns before writing an order intent to SQLite."""
    breaker = getattr(exchange, "api_circuit_breaker", None)
    if isinstance(breaker, ApiCircuitBreaker):
        remaining = breaker.retry_after()
        if remaining > 0:
            raise CircuitOpenError(remaining)


def exchange_retry_delay(exchange: Any, error: Exception, default: float) -> float:
    breaker = getattr(exchange, "api_circuit_breaker", None)
    remaining = breaker.retry_after() if isinstance(breaker, ApiCircuitBreaker) else 0
    local_delay = error.retry_after if isinstance(error, CircuitOpenError) else 0
    return max(default, remaining, local_delay)


def network_backoff(attempt: int, *, cap: float = 60) -> float:
    """Capped exponential delay with jitter; server penalties remain a minimum."""
    ceiling = min(cap, 2 ** min(attempt, 10))
    return random.uniform(ceiling / 2, ceiling)


def config_path() -> Path:
    """Allow container deployments to keep mutable config outside the image."""
    return Path(os.getenv("GRID_BOT_CONFIG_PATH", str(BASE_DIR / "config.json")))


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load Spot configuration; only TRADING_ENVIRONMENT selects the network."""
    path = path if path is not None else config_path()
    with path.open("r", encoding="utf-8") as config_file:
        config = json.load(config_file)

    exchange_config = config.get("exchange", {})
    if (
        exchange_config.get("name") != "binance"
        or exchange_config.get("market_type") != "spot"
    ):
        raise ValueError("Only Binance Spot is supported.")
    # A legacy JSON sandbox flag cannot override the selected environment.
    exchange_config["sandbox"] = get_trading_settings().sandbox

    symbol = config.get("grid", {}).get("symbol")
    if not isinstance(symbol, str) or "/" not in symbol:
        raise ValueError("grid.symbol must be a CCXT symbol such as BTC/USDT.")
    return config


def create_macro_exchange() -> ccxt.binance:
    """Credential-free production Spot history, including when trading on Testnet.

    Testnet resets do not supply the 200-day macro warm-up. This client is used
    exclusively for public OHLCV; order and account I/O use create_exchange().
    """
    exchange = ccxt.binance({"enableRateLimit": True, "timeout": 10000,
                             "options": {"defaultType": "spot"}})
    return install_circuit_breaker(exchange)


def create_exchange() -> ccxt.binance:
    """Create a rate-limited spot client; sandbox must be its first method call."""
    settings = get_trading_settings()
    exchange = ccxt.binance(
        {
            "apiKey": settings.api_key,
            "secret": settings.api_secret,
            "enableRateLimit": True,
            "timeout": 10000,
            "options": {
                "defaultType": "spot",
                # Writes are recovered by client ID, never replayed by CCXT.
                "maxRetriesOnFailure": 0,
                # The 5-minute account-wide audit deliberately uses Binance's
                # higher-weight no-symbol open-orders endpoint.
                "fetchOpenOrders": {"warnWithoutSymbol": False},
            },
        }
    )
    exchange.set_sandbox_mode(settings.sandbox)
    return install_circuit_breaker(exchange)


def check_connection() -> int:
    """Print the selected environment's ticker and pair asset balances."""
    settings = get_trading_settings()
    config = load_config()

    symbol = config["grid"]["symbol"]
    exchange = create_exchange()
    exchange.load_markets()
    market = exchange.market(symbol)
    if not market.get("spot"):
        raise ValueError(f"{symbol} is not a Spot market.")

    ticker = exchange.fetch_ticker(symbol)
    last = ticker.get("last")
    if last is None:
        raise ValueError(f"No last price was returned for {symbol}.")
    print(f"Binance Spot {settings.environment.value} {symbol} last price: {last}")

    balance = exchange.fetch_balance({"type": "spot"})
    for asset in (market["base"], market["quote"]):
        asset_balance = balance.get(asset) or {}
        print(
            f"{asset} balance: free={asset_balance.get('free', 0)}, "
            f"used={asset_balance.get('used', 0)}, "
            f"total={asset_balance.get('total', 0)}"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(check_connection())
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
    except ccxt.AuthenticationError:
        print("Authentication failed. Check the selected Spot API keys and IP allowlist.", file=sys.stderr)
    except ccxt.NetworkError:
        print("Network error while contacting Binance Spot.", file=sys.stderr)
    except ccxt.ExchangeError:
        print("Binance Spot rejected the read-only check.", file=sys.stderr)
    sys.exit(1)
