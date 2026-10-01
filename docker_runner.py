"""Fail closed on missing persistent state and preserve trading halt semantics."""

import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Optional

from database import resolve_database_path
from trading_environment import get_trading_settings


REQUIRED_TABLES = {"grid_orders", "trade_history", "bot_state"}


def ready_to_start(secrets_path: Path = Path("/app/.env")) -> bool:
    config = os.getenv("GRID_BOT_CONFIG_PATH")
    database = str(resolve_database_path())
    if not config:
        print("Docker startup blocked: set the persistent config path.", file=sys.stderr)
        return False
    if not Path(config).is_file() or not Path(database).is_file():
        print("Docker startup blocked: config or SQLite database is missing from /data.", file=sys.stderr)
        return False
    if not secrets_path.is_file():
        print("Docker startup blocked: .env is not mounted at /app/.env.", file=sys.stderr)
        return False
    if not (
        os.access(config, os.R_OK | os.W_OK)
        and os.access(database, os.R_OK | os.W_OK)
        and os.access(Path(database).parent, os.W_OK)
        and os.access(secrets_path, os.R_OK)
    ):
        print("Docker startup blocked: mounted files have incompatible permissions.", file=sys.stderr)
        return False
    try:
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not REQUIRED_TABLES <= tables or connection.execute(
                "PRAGMA quick_check"
            ).fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("required tables or integrity check failed")
    except sqlite3.Error:
        print("Docker startup blocked: SQLite state is invalid.", file=sys.stderr)
        return False
    return True


def main() -> int:
    try:
        get_trading_settings()
        resolve_database_path()
    except ValueError as error:
        print(f"Docker configuration error: {error}", file=sys.stderr)
        return 1
    if not ready_to_start():
        return 0  # A missing or unsafe state must not trigger Docker's restart policy.

    child: Optional[subprocess.Popen] = None

    def forward_signal(signum: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGINT, forward_signal)
    child = subprocess.Popen([sys.executable, "main.py", "--ready"])
    exit_code = child.wait()
    if exit_code in (2, 130, -signal.SIGTERM, -signal.SIGINT):
        print("Trading stopped; automatic restart suppressed.", file=sys.stderr)
        return 0
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
