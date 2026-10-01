"""Environment-isolated Binance Spot client and read-only connection check."""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import ccxt
from trading_environment import get_trading_settings


BASE_DIR = Path(__file__).resolve().parent


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
                # The 5-minute account-wide audit deliberately uses Binance's
                # higher-weight no-symbol open-orders endpoint.
                "fetchOpenOrders": {"warnWithoutSymbol": False},
            },
        }
    )
    exchange.set_sandbox_mode(settings.sandbox)
    return exchange


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
