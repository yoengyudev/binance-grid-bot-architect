"""One-off Binance Spot Testnet order cleanup and local database removal.

Run this on the host holding the bot's live database, after stopping the bot.
This cancels every BTC/USDT order in the account, including manual orders.
It never sells BTC or changes the grid allocation in config.json.
"""

import argparse
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
from main import GridBot, GridConfig, _portfolio_wallet


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


def _require_manual_holding_reconciled(
    database: GridDatabase, exchange: ccxt.binance,
) -> Decimal:
    """Confirm all previously tracked BTC is free and can become manual inventory."""
    held_value = _portfolio_wallet(database)["btc_held"]
    run_text = database.get_state("grid_run")
    if held_value is None or not run_text:
        raise ResetError("Cannot establish the previous bot BTC inventory and baseline.")
    try:
        held = Decimal(str(held_value))
        baseline = Decimal(json.loads(run_text)["baseline_base"])
        account = exchange.fetch_balance({"type": "spot"})["BTC"]
        free = Decimal(str(account["free"]))
        used = Decimal(str(account["used"]))
        total = Decimal(str(account["total"]))
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise ResetError("Cannot reconcile the BTC account balance; database retained.") from error
    amounts = (held, baseline, free, used, total)
    if any(not amount.is_finite() or amount < 0 for amount in amounts):
        raise ResetError("Invalid BTC ledger or exchange balance; database retained.")
    tolerance = Decimal("0.00000001")
    if (held <= 0 or used > tolerance or
            abs(total - free - used) > tolerance or
            abs(total - baseline - held) > tolerance):
        raise ResetError(
            "BTC account balance does not match the saved baseline plus bot "
            "inventory, or BTC is locked; database retained."
        )
    return held


def _refresh_bot_order_fills(
    database: GridDatabase, exchange: ccxt.binance, market: dict,
) -> None:
    """Capture fills that occurred while the bot was stopped or orders canceled."""
    bot = GridBot(GridConfig.load(), exchange, database)
    bot.market = market
    for row in database.fetch_all_orders():
        order = bot._fetch_order(row)
        status = order.get("status")
        if status == "open":
            raise ResetError(
                f"Tracked order {row['order_id']} remains open; database retained."
            )
        if status == "closed":
            if row["status"] not in ("FILLED", "CANCELED"):
                database.mark_order_filled(row["order_id"])
        elif status in ("canceled", "expired", "rejected"):
            if row["status"] in ("OPEN", "PARTIALLY_FILLED"):
                database.update_order_status(row["order_id"], status.upper())
        else:
            raise ResetError(
                f"Unknown exchange status for {row['order_id']}; database retained."
            )
    if database.fetch_active_grids():
        raise ResetError("Tracked orders still need reconciliation; database retained.")


def hard_reset(*, keep_btc_manual=False, input_fn=input, output=print) -> int:
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
    if (preview_held is None or preview_held != 0) and not keep_btc_manual:
        output(
            "Bot inventory is present or unverifiable. Orders can be canceled, "
            "but database deletion will be blocked until inventory is reconciled."
        )
    if keep_btc_manual:
        output("Previously bot-tracked BTC will remain in the Spot wallet as a manual holding.")
    answer = input_fn("Cancel ALL BTC/USDT orders and delete this database? (Y/n; type Y): ")
    if answer.strip().lower() != "y":
        output("Hard reset canceled; no exchange or database changes were made.")
        return 0

    api_key = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise ResetError("Both Binance Spot Testnet API keys are required in .env.")
    exchange = create_exchange(api_key, api_secret)
    # create_exchange already enables sandbox mode; assert it here as well before
    # the first request so this destructive utility cannot use Mainnet URLs.
    exchange.set_sandbox_mode(True)
    exchange.load_markets()
    market = exchange.market(SYMBOL)
    if not market.get("spot") or market.get("active") is False:
        raise ResetError("BTC/USDT is not an active Spot Testnet market.")

    open_orders = exchange.fetch_open_orders(SYMBOL)
    print(f"Open {SYMBOL} orders on Binance Spot Testnet before cancellation: {open_orders}")
    if open_orders:
        try:
            exchange.cancel_all_orders(SYMBOL)
        except ccxt.OrderNotFound:
            # A fill/cancel race may leave nothing to cancel. Verify below.
            pass
    if exchange.fetch_open_orders(SYMBOL):
        raise ResetError("BTC/USDT orders are still open; database retained.")
    output("Successfully canceled all active BTC/USDT orders on Binance Testnet.")

    _require_stopped_service()
    database = GridDatabase(database_path)
    _refresh_bot_order_fills(database, exchange, market)
    if keep_btc_manual:
        adopted = _require_manual_holding_reconciled(
            database, exchange,
        )
        output(f"Retaining {adopted} BTC as a manual holding outside the new bot baseline.")
    else:
        _require_no_bot_inventory(database, exchange)
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
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--keep-btc-manual", action="store_true",
            help="retain reconciled bot BTC in the wallet as a manual holding",
        )
        args = parser.parse_args()
        sys.exit(hard_reset(keep_btc_manual=args.keep_btc_manual))
    except ResetError as error:
        print(f"Hard reset stopped: {error}", file=sys.stderr)
    except ccxt.BaseError as error:
        print(
            f"Binance Testnet request failed ({type(error).__name__}): "
            f"{error}; database retained.", file=sys.stderr,
        )
    except (OSError, sqlite3.Error) as error:
        print(f"Database or service check failed ({type(error).__name__}); inspect the path and state.",
              file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("Hard reset canceled.", file=sys.stderr)
    sys.exit(1)
