"""One immutable, fail-closed environment selection for the process."""

import os
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent


class TradingEnvironment(str, Enum):
    TESTNET = "TESTNET"
    MAINNET = "MAINNET"


@dataclass(frozen=True)
class TradingSettings:
    environment: TradingEnvironment
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    @property
    def sandbox(self) -> bool:
        return self.environment is TradingEnvironment.TESTNET

    @property
    def database_filename(self) -> str:
        return f"grid_{self.environment.value.lower()}.sqlite"


@lru_cache(maxsize=1)
def get_trading_settings() -> TradingSettings:
    # Shell/container settings take precedence over .env; never supply a default.
    load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)
    try:
        environment = TradingEnvironment(os.environ.get("TRADING_ENVIRONMENT"))
    except ValueError:
        raise ValueError(
            'TRADING_ENVIRONMENT must be explicitly "TESTNET" or "MAINNET".'
        ) from None
    prefix = f"BINANCE_{environment.value}"
    key_name, secret_name = f"{prefix}_API_KEY", f"{prefix}_API_SECRET"
    key = os.environ.get(key_name, "").strip()
    secret = os.environ.get(secret_name, "").strip()
    if not key or not secret:
        raise ValueError(f"{environment.value} requires both {key_name} and {secret_name}.")
    return TradingSettings(environment, key, secret)
