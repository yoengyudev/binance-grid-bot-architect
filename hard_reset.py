"""One-off Binance Spot Testnet order cleanup and local database removal.

Run this on the host holding the bot's live database, after stopping the bot.
This cancels every BTC/USDT order in the account, including manual orders.
It never sells BTC or changes the grid allocation in config.json.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path

import ccxt
from dotenv import load_dotenv

from database import DATABASE_PATH, GridDatabase
from exchange_handler import BASE_DIR, create_exchange, load_config
from main import _portfolio_wallet


SYMBOL = "BTC/USDT"
SERVICE_NAME = "binance-grid-bot"


class ResetError(RuntimeError):
    """A safety precondition failed; the database must be retained."""


def _database_path() -> Path:
    configured = os.getenv("GRID_BOT_DB_PATH")
    if configured and not Path(configured).is_absolute():
        raise ResetError("GRID_BOT_DB_PATH must be absolute for a hard reset.")
    path = Path(configured) if configured else DATABASE_PATH
    if path.is_symlink():
        raise ResetError("Refusing to delete a database through a symbolic link.")
    path = path.resolve()
    if path.suffix not in (".sqlite3", ".sqlite", ".db"):
        raise ResetError(f"Refusing to delete a file without a SQLite extension: {path}")
    if not path.is_file():
        raise ResetError(f"No SQLite database found at {path}; nothing was deleted.")
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            names = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name IN ('grid_orders', 'bot_state')"
                )
            }
    except sqlite3.Error as error:
        raise ResetError(f"Cannot inspect SQLite database at {path}.") from error
    if names != {"grid_orders", "bot_state"}:
        raise ResetError(f"File is not a recognized grid bot database: {path}")
    return path


def _require_stopped_service() -> None:
    if shutil.which("systemctl") is None:
        raise ResetError("Cannot verify the bot is stopped; run this on its systemd host.")
    result = subprocess.run(
        ["systemctl", "is-active", SERVICE_NAME],
        capture_output=True, text=True, check=False,
    )
    if result.stdout.strip() not in ("inactive", "failed"):
        raise ResetError(
            f"Stop {SERVICE_NAME}.service before resetting the database."
        )


def _require_no_bot_inventory(database: GridDatabase, exchange: ccxt.binance) -> None:
    wallet = _portfolio_wallet(database)
    held = wallet["btc_held"]
    if held is None or not (Decimal(str(held)).is_finite() and held == 0):
        raise ResetError(
            "Bot-tracked BTC remains or cannot be verified; database retained. "
            "Reconcile the inventory before wiping its cost basis."
        )

    run_text = database.get_state("grid_run")
    if run_text:
        try:
            baseline = Decimal(json.loads(run_text)["baseline_base"])
            balance = exchange.fetch_balance({"type": "spot"}).get("BTC", {})
            total = balance.get("total")
            if total is None:
                total = Decimal(str(balance["free"])) + Decimal(str(balance.get("used", 0)))
            current = Decimal(str(total))
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ResetError("Could not reconcile BTC balance with the saved baseline.") from error
        if (not baseline.is_finite() or not current.is_finite() or
                baseline < 0 or current < 0 or current > baseline + Decimal("0.00000001")):
            raise ResetError(
                "BTC balance exceeds the saved pre-bot baseline; "
                "database retained for inventory reconciliation."
            )


def hard_reset(*, input_fn=input, output=print) -> int:
    """Cancel all testnet orders, verify exposure, then remove the local DB."""
    load_dotenv(dotenv_path=BASE_DIR / ".env")
    config = load_config()
    if config["grid"]["symbol"] != SYMBOL:
        raise ResetError(f"Configured pair must be {SYMBOL} for this utility.")
    database_path = _database_path()
    _require_stopped_service()

    output(f"Database to delete on this host: {database_path}")
    output("This also cancels manual BTC/USDT Spot Testnet orders in the same account.")
    try:
        configured_capital = Decimal(str(config["grid"]["investment_quote"]))
    except (KeyError, InvalidOperation) as error:
        raise ResetError("Configured grid investment_quote is invalid.") from error
    if not configured_capital.is_finite() or configured_capital <= 0:
        raise ResetError("Configured grid investment_quote must be positive.")
    if configured_capital != Decimal("100"):
        output(
            f"Current config allocates {configured_capital} USDT, not 100 USDT. "
            "Keep the bot stopped until the new allocation is configured."
        )
    preview_held = _portfolio_wallet(GridDatabase(database_path))["btc_held"]
    if preview_held is None or preview_held != 0:
        output(
            "Bot inventory is present or unverifiable. Orders can be canceled, "
            "but database deletion will be blocked until inventory is reconciled."
        )
    answer = input_fn("Cancel ALL BTC/USDT orders and delete this database? (Y/n; type Y): ")
    if answer.strip().lower() != "y":
        output("Hard reset canceled; no exchange or database changes were made.")
        return 0

    api_key = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise ResetError("Both Binance Spot Testnet API keys are required in .env.")
    exchange = create_exchange(api_key, api_secret)
    exchange.load_markets()
    market = exchange.market(SYMBOL)
    if not market.get("spot") or market.get("active") is False:
        raise ResetError("BTC/USDT is not an active Spot Testnet market.")

    exchange.cancel_all_orders(SYMBOL)
    if exchange.fetch_open_orders(SYMBOL):
        raise ResetError("BTC/USDT orders are still open; database retained.")
    output("Successfully canceled all active BTC/USDT orders on Binance Testnet.")

    _require_stopped_service()
    _require_no_bot_inventory(GridDatabase(database_path), exchange)
    os.remove(database_path)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{database_path}{suffix}")
        if sidecar.exists():
            os.remove(sidecar)
    output("Successfully deleted the SQLite database file.")
    if configured_capital != Decimal("100"):
        output("Set grid.investment_quote to 100 USDT before starting the next run.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(hard_reset())
    except ResetError as error:
        print(f"Hard reset stopped: {error}", file=sys.stderr)
    except (ccxt.NetworkError, ccxt.ExchangeError) as error:
        print(f"Binance Testnet request failed ({type(error).__name__}); database retained.",
              file=sys.stderr)
    except (OSError, sqlite3.Error) as error:
        print(f"Database or service check failed ({type(error).__name__}); inspect the path and state.",
              file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("Hard reset canceled.", file=sys.stderr)
    sys.exit(1)
