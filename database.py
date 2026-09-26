"""Persistent local state for grid orders and completed trades (Phase 2)."""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union


DATABASE_PATH = Path(__file__).resolve().parent / "grid_bot.sqlite3"
PathValue = Union[str, Path]
NumberValue = Union[str, int, float, Decimal]

ORDER_TRANSITIONS = {
    "OPEN": {"PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "EXPIRED"},
    "PARTIALLY_FILLED": {"FILLED", "CANCELED", "EXPIRED"},
    "FILLED": set(),
    "CANCELED": set(),
    "REJECTED": set(),
    "EXPIRED": set(),
}


def _decimal_text(value: NumberValue, *, positive: bool = True) -> str:
    """Store monetary values as decimal text to avoid SQLite REAL rounding."""
    if isinstance(value, bool):
        raise ValueError("Monetary values must be numbers, not booleans.")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Monetary values must be valid decimals.") from error
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError("Monetary values must be finite and prices/amounts positive.")
    return format(number, "f")


class GridDatabase:
    """Each operation commits atomically and can be recovered after a restart."""

    def __init__(self, path: Optional[PathValue] = None) -> None:
        self.path = Path(path) if path is not None else Path(
            os.getenv("GRID_BOT_DB_PATH", str(DATABASE_PATH))
        )
        self.initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(str(self.path), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection

    def initialize(self) -> None:
        """Create tables and indexes without changing any existing rows."""
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS grid_orders (
                    order_id TEXT PRIMARY KEY,
                    client_order_id TEXT UNIQUE,
                    exchange_order_id TEXT,
                    parent_order_id TEXT,
                    order_type TEXT NOT NULL DEFAULT 'LIMIT'
                        CHECK (order_type IN ('LIMIT', 'MARKET')),
                    level INTEGER NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                    price TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN'
                        CHECK (status IN ('OPEN', 'PARTIALLY_FILLED', 'FILLED',
                                          'CANCELED', 'REJECTED', 'EXPIRED')),
                    created_at TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
                """
            )
            existing_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(grid_orders)")
            }
            for name in ("client_order_id", "exchange_order_id", "parent_order_id"):
                if name not in existing_columns:
                    connection.execute(f"ALTER TABLE grid_orders ADD COLUMN {name} TEXT")
            if "order_type" not in existing_columns:
                connection.execute(
                    "ALTER TABLE grid_orders ADD COLUMN order_type TEXT NOT NULL "
                    "DEFAULT 'LIMIT' CHECK (order_type IN ('LIMIT', 'MARKET'))"
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_grid_orders_client_id "
                "ON grid_orders (client_order_id)"
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_grid_orders_status_level
                ON grid_orders (status, level)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sell_order_id TEXT,
                    buy_price TEXT NOT NULL,
                    sell_price TEXT NOT NULL,
                    profit TEXT NOT NULL,
                    timestamp TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
                """
            )
            trade_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(trade_history)")
            }
            if "sell_order_id" not in trade_columns:
                connection.execute("ALTER TABLE trade_history ADD COLUMN sell_order_id TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_history_sell_order "
                "ON trade_history (sell_order_id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS archived_grid_orders AS
                SELECT grid_orders.*, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    AS archived_at
                FROM grid_orders WHERE 0
                """
            )

    def insert_order(
        self,
        order_id: Union[str, int],
        level: int,
        side: str,
        price: NumberValue,
        amount: NumberValue,
        *,
        client_order_id: Optional[str] = None,
        exchange_order_id: Optional[str] = None,
        parent_order_id: Optional[str] = None,
        order_type: str = "LIMIT",
    ) -> None:
        """Save a newly accepted exchange order; duplicate IDs are rejected."""
        if not str(order_id).strip():
            raise ValueError("order_id cannot be empty.")
        if isinstance(level, bool) or not isinstance(level, int):
            raise ValueError("level must be an integer.")
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL.")
        if order_type not in ("LIMIT", "MARKET"):
            raise ValueError("order_type must be LIMIT or MARKET.")
        price_text = _decimal_text(price)
        amount_text = _decimal_text(amount)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO grid_orders
                    (order_id, client_order_id, exchange_order_id, parent_order_id,
                     order_type, level, side, price, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(order_id), client_order_id, exchange_order_id, parent_order_id,
                    order_type, level, side, price_text, amount_text,
                ),
            )

    def set_exchange_order_id(self, order_id: Union[str, int], exchange_order_id: str) -> None:
        """Attach the exchange ID after a persisted client-order intent is accepted."""
        if not str(exchange_order_id).strip():
            raise ValueError("exchange_order_id cannot be empty.")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE grid_orders
                SET exchange_order_id = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE order_id = ?
                """,
                (str(exchange_order_id), str(order_id)),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown order ID: {order_id}")

    def get_order(self, order_id: Union[str, int]) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM grid_orders WHERE order_id = ?", (str(order_id),)
            ).fetchone()
        return dict(row) if row is not None else None

    def update_order_status(self, order_id: Union[str, int], status: str) -> bool:
        """Apply a valid state transition; return False if already in that state."""
        if status not in ORDER_TRANSITIONS:
            raise ValueError(f"Unsupported order status: {status}")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM grid_orders WHERE order_id = ?", (str(order_id),)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown order ID: {order_id}")
            previous = row["status"]
            if previous == status:
                return False
            if status not in ORDER_TRANSITIONS[previous]:
                raise ValueError(f"Invalid order status transition: {previous} -> {status}")
            connection.execute(
                """
                UPDATE grid_orders
                SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE order_id = ?
                """,
                (status, str(order_id)),
            )
        return True

    def mark_order_filled(self, order_id: Union[str, int]) -> bool:
        """Mark an open or partially filled order as filled."""
        return self.update_order_status(order_id, "FILLED")

    def fetch_active_grids(self) -> List[Dict[str, Any]]:
        """Recover orders that still need exchange reconciliation."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM grid_orders
                WHERE status IN ('OPEN', 'PARTIALLY_FILLED')
                ORDER BY level, order_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_all_orders(self) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM grid_orders ORDER BY rowid").fetchall()
        return [dict(row) for row in rows]

    def fetch_latest_orders_by_level(self) -> Dict[int, Dict[str, Any]]:
        """Return the most recently inserted order in each grid lane."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM grid_orders AS orders
                WHERE rowid = (
                    SELECT MAX(rowid) FROM grid_orders WHERE level = orders.level
                )
                ORDER BY level
                """
            ).fetchall()
        return {row["level"]: dict(row) for row in rows}

    def get_state(self, key: str) -> Optional[str]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM bot_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row is not None else None

    def set_state(self, key: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO bot_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def update_runtime_grid_settings(self, grid_run: str, active_config: str) -> None:
        """Persist a setting change with its matching run fingerprint atomically."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            saved_run = connection.execute(
                "SELECT value FROM bot_state WHERE key = 'grid_run'"
            ).fetchone()
            pending_reset = connection.execute(
                "SELECT 1 FROM bot_state WHERE key = 'grid_reset'"
            ).fetchone()
            if saved_run is None or pending_reset is not None:
                raise ValueError("A running grid without a pending reset is required.")
            for key, value in (
                ("grid_run", grid_run),
                ("active_grid_config", active_config),
            ):
                connection.execute(
                    "INSERT INTO bot_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def complete_grid_reset(self, grid_run: str, active_config: str,
                            placing_request: str, *, carry_order_id: Optional[str] = None,
                            carry_price: Optional[str] = None,
                            carry_amount: Optional[str] = None,
                            carry_cost: Optional[str] = None) -> None:
        """Archive old lanes and switch runs in one SQLite transaction."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT COUNT(*) FROM grid_orders "
                "WHERE status IN ('OPEN', 'PARTIALLY_FILLED')"
            ).fetchone()[0]
            if active:
                raise ValueError("Old grid still has active orders.")
            connection.execute(
                "INSERT INTO archived_grid_orders "
                "SELECT grid_orders.*, strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "FROM grid_orders"
            )
            connection.execute("DELETE FROM grid_orders")
            if carry_order_id is not None:
                if not all((carry_price, carry_amount, carry_cost)):
                    raise ValueError("Carry inventory has incomplete cost basis.")
                connection.execute(
                    "INSERT INTO grid_orders "
                    "(order_id, order_type, level, side, price, amount, status) "
                    "VALUES (?, 'MARKET', 0, 'BUY', ?, ?, 'FILLED')",
                    (carry_order_id, carry_price, carry_amount),
                )
            for key, value in (
                ("grid_run", grid_run),
                ("active_grid_config", active_config),
                ("grid_reset", placing_request),
                ("carry_inventory", json.dumps({
                    "order_id": carry_order_id, "cost": carry_cost,
                })),
            ):
                connection.execute(
                    "INSERT INTO bot_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def clear_state(self, key: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM bot_state WHERE key = ?", (key,))

    def finish_grid_reset(self) -> None:
        """Mark placement complete and queue its owner notification atomically."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM bot_state WHERE key = 'grid_reset'")
            connection.execute(
                "INSERT INTO bot_state (key, value) "
                "VALUES ('grid_reset_notification_pending', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'"
            )

    def record_trade(
        self,
        buy_price: NumberValue,
        sell_price: NumberValue,
        profit: NumberValue,
        *,
        sell_order_id: Optional[str] = None,
    ) -> int:
        """Store caller-calculated realized profit, which may be negative."""
        values = (
            _decimal_text(buy_price),
            _decimal_text(sell_price),
            _decimal_text(profit, positive=False),
        )
        with self._connection() as connection:
            if sell_order_id is not None:
                existing = connection.execute(
                    "SELECT id FROM trade_history WHERE sell_order_id = ?",
                    (sell_order_id,),
                ).fetchone()
                if existing is not None:
                    return existing["id"]
            connection.execute(
                """
                INSERT INTO trade_history (buy_price, sell_price, profit, sell_order_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sell_order_id) DO NOTHING
                """,
                (*values, sell_order_id),
            )
            if sell_order_id is not None:
                row = connection.execute(
                    "SELECT id FROM trade_history WHERE sell_order_id = ?",
                    (sell_order_id,),
                ).fetchone()
                return row["id"]
            return connection.execute("SELECT last_insert_rowid()").fetchone()[0]

    def fetch_trade_history(self) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM trade_history ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]
