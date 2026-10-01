"""Persistent local state for grid orders and completed trades (Phase 2)."""

import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
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
                "CREATE INDEX IF NOT EXISTS idx_grid_orders_recent_fills "
                "ON grid_orders (status, updated_at DESC)"
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
            for name in ("buy_order_id", "sold_base", "fee_basis"):
                if name not in trade_columns:
                    connection.execute(f"ALTER TABLE trade_history ADD COLUMN {name} TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_history_sell_order "
                "ON trade_history (sell_order_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_history_timestamp "
                "ON trade_history (timestamp)"
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
                CREATE TABLE IF NOT EXISTS runner_lease (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    owner_token TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL
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
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_archived_grid_orders_recent_fills "
                "ON archived_grid_orders (status, updated_at DESC)"
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

    def discard_rejected_post_only_order(self, order_id: Union[str, int]) -> None:
        """Remove an intent only after Binance definitively rejects a maker order."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM grid_orders
                WHERE order_id = ? AND order_type = 'LIMIT'
                  AND status = 'OPEN' AND exchange_order_id IS NULL
                """,
                (str(order_id),),
            )
            if cursor.rowcount != 1:
                raise ValueError("Post-only rejection does not match a pending limit order.")

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

    def fetch_recent_filled_orders(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return recent exchange-backed fills from current and archived runs."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Filled-order limit must be from 1 to 100.")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT fills.*, snapshots.value AS fill_snapshot
                FROM (
                    SELECT order_id, side, order_type, status, price, amount,
                           created_at, updated_at
                    FROM grid_orders
                    WHERE status = 'FILLED' AND client_order_id IS NOT NULL
                    UNION ALL
                    SELECT order_id, side, order_type, status, price, amount,
                           created_at, updated_at
                    FROM archived_grid_orders
                    WHERE status = 'FILLED' AND client_order_id IS NOT NULL
                ) AS fills
                LEFT JOIN bot_state AS snapshots
                  ON snapshots.key = 'fill_snapshot:' || fills.order_id
                ORDER BY fills.updated_at DESC, fills.order_id DESC
                LIMIT ?
                """,
                (limit,),
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

    def get_or_create_state(self, key: str, value: str) -> str:
        """Initialize a shared state key once across competing processes."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO bot_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO NOTHING", (key, value),
            )
            row = connection.execute(
                "SELECT value FROM bot_state WHERE key = ?", (key,),
            ).fetchone()
        return row["value"]

    @staticmethod
    def _lease_time_ms(now_ms: Optional[int]) -> int:
        return time.time_ns() // 1_000_000 if now_ms is None else now_ms

    def acquire_runner_lease(
        self, owner_token: str, *, ttl_seconds: int = 30,
        now_ms: Optional[int] = None,
    ) -> Optional[int]:
        """Atomically acquire the singleton runner lease; return its fencing epoch."""
        if not owner_token or ttl_seconds <= 0:
            raise ValueError("Runner lease requires an owner and positive TTL.")
        now = self._lease_time_ms(now_ms)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_token, epoch, expires_at_ms FROM runner_lease WHERE id = 1"
            ).fetchone()
            if row is not None and row["expires_at_ms"] > now:
                if row["owner_token"] != owner_token:
                    return None
                epoch = row["epoch"]
            else:
                epoch = (row["epoch"] if row is not None else 0) + 1
            connection.execute(
                "INSERT INTO runner_lease (id, owner_token, epoch, expires_at_ms) "
                "VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "owner_token = excluded.owner_token, epoch = excluded.epoch, "
                "expires_at_ms = excluded.expires_at_ms",
                (owner_token, epoch, now + ttl_seconds * 1000),
            )
            return epoch

    def renew_runner_lease(
        self, owner_token: str, epoch: int, *, ttl_seconds: int = 30,
        now_ms: Optional[int] = None,
    ) -> bool:
        """A stale or expired owner cannot renew its fencing epoch."""
        if ttl_seconds <= 0:
            raise ValueError("Runner lease TTL must be positive.")
        now = self._lease_time_ms(now_ms)
        with self._connection() as connection:
            changed = connection.execute(
                "UPDATE runner_lease SET expires_at_ms = ? WHERE id = 1 "
                "AND owner_token = ? AND epoch = ? AND expires_at_ms > ?",
                (now + ttl_seconds * 1000, owner_token, epoch, now),
            ).rowcount
        return changed == 1

    def runner_lease_valid(
        self, owner_token: str, epoch: int, *, now_ms: Optional[int] = None,
    ) -> bool:
        now = self._lease_time_ms(now_ms)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM runner_lease WHERE id = 1 AND owner_token = ? "
                "AND epoch = ? AND expires_at_ms > ?",
                (owner_token, epoch, now),
            ).fetchone()
        return row is not None

    def release_runner_lease(self, owner_token: str, epoch: int) -> bool:
        """Release only the currently owned generation, never a successor's lease."""
        with self._connection() as connection:
            changed = connection.execute(
                "DELETE FROM runner_lease WHERE id = 1 AND owner_token = ? AND epoch = ?",
                (owner_token, epoch),
            ).rowcount
        return changed == 1

    def set_state(self, key: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO bot_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def require_order_cleanup(self, reason: str) -> None:
        """Atomically stop placement and persist the exchange cleanup obligation."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for key, value in (
                ("engine_stop_cleanup_pending", "1"),
                ("engine_status", "IDLE"),
                ("order_cleanup_alarm", reason),
            ):
                connection.execute(
                    "INSERT INTO bot_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def complete_order_cleanup(self) -> None:
        """Clear the obligation only after the caller verifies the exchange book."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM bot_state WHERE key IN "
                "('engine_stop_cleanup_pending', 'order_cleanup_alarm')"
            )

    def update_runtime_grid_settings(self, grid_run: str, active_config: str,
                                     *, trailing_stop: Optional[str] = None,
                                     allow_pending_reset: bool = False) -> None:
        """Persist a setting change with its matching run fingerprint atomically."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            saved_run = connection.execute(
                "SELECT value FROM bot_state WHERE key = 'grid_run'"
            ).fetchone()
            pending_reset = connection.execute(
                "SELECT 1 FROM bot_state WHERE key = 'grid_reset'"
            ).fetchone()
            if saved_run is None or (pending_reset is not None and
                                     not allow_pending_reset):
                raise ValueError("A running grid without a pending reset is required.")
            states = [
                ("grid_run", grid_run),
                ("active_grid_config", active_config),
            ]
            if trailing_stop is not None:
                states.append(("trailing_stop", trailing_stop))
            for key, value in states:
                connection.execute(
                    "INSERT INTO bot_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def complete_grid_reset(self, grid_run: str, active_config: str,
                            placing_request: str, *, carry_order_id: Optional[str] = None,
                            carry_price: Optional[str] = None,
                            carry_amount: Optional[str] = None,
                            carry_cost: Optional[str] = None,
                            breakout_width_percent: Optional[str] = None,
                            trailing_stop: Optional[str] = None,
                            clear_liquidation_state: bool = False) -> None:
        """Archive old lanes and switch runs in one SQLite transaction."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if clear_liquidation_state:
                mode = connection.execute(
                    "SELECT value FROM bot_state WHERE key = 'safety_mode'"
                ).fetchone()
                if mode is None or mode["value"] != "LIQUIDATED":
                    raise ValueError("A completed liquidation is required for admin reset.")
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
            states = [
                ("grid_run", grid_run),
                ("active_grid_config", active_config),
                ("grid_reset", placing_request),
                ("carry_inventory", json.dumps({
                    "order_id": carry_order_id, "cost": carry_cost,
                })),
            ]
            if breakout_width_percent is not None:
                states.append(("breakout_width_percent", breakout_width_percent))
            if trailing_stop is not None:
                states.append(("trailing_stop", trailing_stop))
            for key, value in states:
                connection.execute(
                    "INSERT INTO bot_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            if clear_liquidation_state:
                connection.execute(
                    "DELETE FROM bot_state WHERE key IN "
                    "('safety_mode', 'halt_reason', 'hard_stop_liquidation', "
                    "'hard_stop_notification_pending')"
                )

    def clear_state(self, key: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM bot_state WHERE key = ?", (key,))

    def factory_reset(self) -> None:
        """Erase trading history atomically while retaining restart configuration."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT COUNT(*) FROM grid_orders "
                "WHERE status IN ('OPEN', 'PARTIALLY_FILLED')"
            ).fetchone()[0]
            if active:
                raise ValueError("Tracked orders remain active; reconcile them first.")
            connection.execute("DELETE FROM trade_history")
            connection.execute("DELETE FROM archived_grid_orders")
            connection.execute("DELETE FROM grid_orders")
            connection.execute(
                "DELETE FROM bot_state WHERE key NOT IN "
                "('grid_run', 'active_grid_config', 'trailing_stop', "
                "'breakout_width_percent', 'order_client_prefix')"
            )
            connection.execute(
                "INSERT INTO bot_state (key, value) VALUES ('safety_mode', 'LIQUIDATED')"
            )
            connection.execute(
                "INSERT INTO bot_state (key, value) VALUES "
                "('hard_stop_liquidation', ?)",
                (json.dumps({"phase": "complete", "residual_base": "0"}),),
            )

    def finish_grid_reset(
        self, notification_key: str = "grid_reset_notification_pending",
        notification_value: str = "1",
    ) -> None:
        """Mark placement complete and queue its owner notification atomically."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM bot_state WHERE key = 'grid_reset'")
            connection.execute(
                "INSERT INTO bot_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (notification_key, notification_value),
            )

    def record_trade(
        self,
        buy_price: NumberValue,
        sell_price: NumberValue,
        profit: NumberValue,
        *,
        sell_order_id: Optional[str] = None,
        buy_order_id: Optional[str] = None,
        sold_base: Optional[NumberValue] = None,
        fee_basis: Optional[str] = None,
    ) -> int:
        """Store caller-calculated realized profit, which may be negative."""
        if fee_basis not in (None, "exchange", "estimated", "mixed"):
            raise ValueError("Trade fee basis must identify exchange or estimated fees.")
        values = (
            _decimal_text(buy_price),
            _decimal_text(sell_price),
            _decimal_text(profit, positive=False),
            buy_order_id,
            _decimal_text(sold_base) if sold_base is not None else None,
            fee_basis,
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
                INSERT INTO trade_history
                    (buy_price, sell_price, profit, buy_order_id, sold_base,
                     fee_basis, sell_order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
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

    def realized_pnl_summary(self, now: Optional[datetime] = None) -> Dict[str, Decimal]:
        """Sum recorded net profits in UTC without SQLite floating-point rounding."""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("P&L summary requires a timezone-aware timestamp.")
        current = current.astimezone(timezone.utc)
        today_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = current - timedelta(days=7)
        totals = {"today": Decimal(0), "seven_day": Decimal(0),
                  "total": Decimal(0)}
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT profit, timestamp FROM trade_history"
            ).fetchall()
        for row in rows:
            profit = Decimal(row["profit"])
            recorded = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            recorded = recorded.astimezone(timezone.utc)
            totals["total"] += profit
            if today_start <= recorded <= current:
                totals["today"] += profit
            if week_start <= recorded <= current:
                totals["seven_day"] += profit
        return totals
