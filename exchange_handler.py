"""Phase 1: read-only Binance Spot Testnet connection check."""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import ccxt
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent


def config_path() -> Path:
    """Allow container deployments to keep mutable config outside the image."""
    return Path(os.getenv("GRID_BOT_CONFIG_PATH", str(BASE_DIR / "config.json")))


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the grid configuration and reject non-testnet exchange settings."""
    path = path if path is not None else config_path()
    with path.open("r", encoding="utf-8") as config_file:
        config = json.load(config_file)

    exchange_config = config.get("exchange", {})
    if (
        exchange_config.get("name") != "binance"
        or exchange_config.get("market_type") != "spot"
        or exchange_config.get("sandbox") is not True
    ):
        raise ValueError("Phase 1 supports Binance Spot Testnet only.")

    symbol = config.get("grid", {}).get("symbol")
    if not isinstance(symbol, str) or "/" not in symbol:
        raise ValueError("grid.symbol must be a CCXT symbol such as BTC/USDT.")
    return config


def create_exchange(api_key: str = "", api_secret: str = "") -> ccxt.binance:
    """Create a rate-limited spot client; sandbox must be its first method call."""
    exchange = ccxt.binance(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": 10000,
            "options": {
                "defaultType": "spot",
                # The 5-minute account-wide audit deliberately uses Binance's
                # higher-weight no-symbol open-orders endpoint.
                "fetchOpenOrders": {"warnWithoutSymbol": False},
            },
        }
    )
    exchange.set_sandbox_mode(True)
    return exchange


def check_connection() -> int:
    """Print the testnet ticker and, when configured, pair asset balances."""
    config = load_config()
    load_dotenv(dotenv_path=BASE_DIR / ".env")
    api_key = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "").strip()
    if bool(api_key) != bool(api_secret):
        raise ValueError("Set both BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET.")

    symbol = config["grid"]["symbol"]
    exchange = create_exchange(api_key, api_secret)
    exchange.load_markets()
    market = exchange.market(symbol)
    if not market.get("spot"):
        raise ValueError(f"{symbol} is not a Spot market.")

    ticker = exchange.fetch_ticker(symbol)
    last = ticker.get("last")
    if last is None:
        raise ValueError(f"No last price was returned for {symbol}.")
    print(f"Binance Spot Testnet {symbol} last price: {last}")

    if not api_key:
        print("Balance check skipped: add Spot Testnet keys to .env.")
        return 0

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
        print("Authentication failed. Check your Spot Testnet API keys and IP allowlist.", file=sys.stderr)
    except ccxt.NetworkError:
        print("Network error while contacting Binance Spot Testnet.", file=sys.stderr)
    except ccxt.ExchangeError:
        print("Binance Spot Testnet rejected the read-only check.", file=sys.stderr)
    sys.exit(1)
