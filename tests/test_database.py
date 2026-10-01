import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier

from database import GridDatabase


class GridDatabaseTests(unittest.TestCase):
    def test_runner_lease_fences_competing_connections_and_expired_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "grid_testnet.sqlite"
            first, second = GridDatabase(path), GridDatabase(path)
            gate = Barrier(2)

            def compete(database, token):
                gate.wait()
                return database.acquire_runner_lease(token, now_ms=1_000)

            with ThreadPoolExecutor(max_workers=2) as pool:
                one = pool.submit(compete, first, "runner-one")
                two = pool.submit(compete, second, "runner-two")
                epochs = [one.result(), two.result()]
            self.assertEqual(sorted(epoch for epoch in epochs if epoch is not None), [1])
            winner, loser = ((first, second) if epochs[0] else (second, first))
            winner_token, loser_token = (("runner-one", "runner-two") if epochs[0]
                                         else ("runner-two", "runner-one"))
            self.assertIsNone(loser.acquire_runner_lease(loser_token, now_ms=1_001))
            self.assertTrue(winner.renew_runner_lease(winner_token, 1, now_ms=2_000))
            self.assertEqual(loser.acquire_runner_lease(loser_token, now_ms=33_000), 2)
            self.assertFalse(winner.renew_runner_lease(winner_token, 1, now_ms=33_000))
            self.assertFalse(winner.release_runner_lease(winner_token, 1))
            self.assertTrue(loser.runner_lease_valid(loser_token, 2, now_ms=33_000))
            self.assertTrue(loser.release_runner_lease(loser_token, 2))

    def test_realized_pnl_uses_utc_windows_and_deduplicates_sell_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = GridDatabase(Path(temporary_directory) / "grid_testnet.sqlite")
            database.record_trade("100", "110", "8.79", sell_order_id="today-win",
                                  buy_order_id="buy-1", sold_base="1",
                                  fee_basis="estimated")
            database.record_trade("100", "90", "-10.19", sell_order_id="today-loss")
            database.record_trade("100", "105", "4.79", sell_order_id="week")
            database.record_trade("100", "120", "19.79", sell_order_id="old")
            self.assertEqual(
                database.record_trade("100", "999", "999", sell_order_id="today-win"),
                database.fetch_trade_history()[0]["id"],
            )
            with database._connection() as connection:
                connection.execute(
                    "UPDATE trade_history SET timestamp = ? WHERE sell_order_id = ?",
                    ("2026-10-01T01:00:00.000Z", "today-win"),
                )
                connection.execute(
                    "UPDATE trade_history SET timestamp = ? WHERE sell_order_id = ?",
                    ("2026-10-01T03:00:00.000Z", "today-loss"),
                )
                connection.execute(
                    "UPDATE trade_history SET timestamp = ? WHERE sell_order_id = ?",
                    ("2026-09-27T00:00:00.000Z", "week"),
                )
                connection.execute(
                    "UPDATE trade_history SET timestamp = ? WHERE sell_order_id = ?",
                    ("2026-09-01T00:00:00.000Z", "old"),
                )
            totals = database.realized_pnl_summary(
                datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
            )
            self.assertEqual(totals, {
                "today": Decimal("-1.40"),
                "seven_day": Decimal("3.39"),
                "total": Decimal("23.18"),
            })
            trade = database.fetch_trade_history()[0]
            self.assertEqual(trade["buy_order_id"], "buy-1")
            self.assertEqual(trade["sold_base"], "1")
            self.assertEqual(trade["fee_basis"], "estimated")

    def test_existing_trade_history_migrates_without_losing_profit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "grid_testnet.sqlite"
            with closing(sqlite3.connect(path)) as connection:
                with connection:
                    connection.execute("CREATE TABLE trading_environment (id INTEGER PRIMARY KEY, environment TEXT)")
                    connection.execute("INSERT INTO trading_environment VALUES (1, 'TESTNET')")
                    connection.execute(
                        "CREATE TABLE trade_history (id INTEGER PRIMARY KEY, "
                        "buy_price TEXT NOT NULL, sell_price TEXT NOT NULL, "
                        "profit TEXT NOT NULL, timestamp TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO trade_history VALUES "
                        "(1, '100', '110', '9', '2026-10-01T00:00:00.000Z')"
                    )
            database = GridDatabase(path)
            self.assertEqual(database.fetch_trade_history()[0]["profit"], "9")
            self.assertEqual(database.realized_pnl_summary(
                datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
            )["total"], Decimal("9"))

    def test_order_and_trade_state_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "grid_testnet.sqlite"
            database = GridDatabase(path)
            database.insert_order("buy-1", 0, "BUY", "84620.01", "0.01000000")
            database.insert_order("sell-1", 1, "SELL", "85889.31", "0.01000000")
            self.assertTrue(database.mark_order_filled("buy-1"))
            self.assertFalse(database.mark_order_filled("buy-1"))
            database.record_trade("84620.01", "85889.31", "12.69")

            recovered = GridDatabase(path)
            self.assertEqual(recovered.get_order("buy-1")["status"], "FILLED")
            self.assertEqual(
                [order["order_id"] for order in recovered.fetch_active_grids()],
                ["sell-1"],
            )
            self.assertEqual(recovered.get_order("sell-1")["amount"], "0.01000000")
            self.assertEqual(recovered.fetch_trade_history()[0]["profit"], "12.69")

    def test_rejects_duplicate_orders_and_invalid_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = GridDatabase(Path(temporary_directory) / "grid_testnet.sqlite")
            database.insert_order("order-1", 0, "BUY", "100", "0.1")
            with self.assertRaises(sqlite3.IntegrityError):
                database.insert_order("order-1", 0, "BUY", "100", "0.1")
            with self.assertRaises(ValueError):
                database.insert_order("bad", 0, "BUY", "NaN", "0.1")
            database.update_order_status("order-1", "CANCELED")
            with self.assertRaises(ValueError):
                database.mark_order_filled("order-1")
            with self.assertRaises(KeyError):
                database.mark_order_filled("missing")

    def test_post_only_cleanup_cannot_remove_an_accepted_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = GridDatabase(Path(temporary_directory) / "grid_testnet.sqlite")
            database.insert_order(
                "maker-1", 1, "BUY", "85000", "0.01", client_order_id="maker-1"
            )
            database.set_exchange_order_id("maker-1", "exchange-1")
            with self.assertRaises(ValueError):
                database.discard_rejected_post_only_order("maker-1")
            self.assertEqual(database.get_order("maker-1")["exchange_order_id"], "exchange-1")

    def test_recent_fills_include_archived_runs_and_exclude_synthetic_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = GridDatabase(Path(temporary_directory) / "grid_testnet.sqlite")
            database.insert_order("old-fill", 1, "BUY", "80000", "0.01",
                                  client_order_id="gridbot-old-fill")
            database.mark_order_filled("old-fill")
            database.insert_order("synthetic-carry", 0, "BUY", "79000", "0.01",
                                  order_type="MARKET")
            database.mark_order_filled("synthetic-carry")
            database.complete_grid_reset("{}", "{}", "{}")
            database.insert_order("new-fill", -1, "SELL", "90000", "0.01",
                                  client_order_id="gridbot-new-fill")
            database.mark_order_filled("new-fill")
            self.assertEqual(
                {row["order_id"] for row in database.fetch_recent_filled_orders()},
                {"old-fill", "new-fill"},
            )
            self.assertEqual(len(database.fetch_recent_filled_orders(1)), 1)
            with self.assertRaises(ValueError):
                database.fetch_recent_filled_orders(101)


if __name__ == "__main__":
    unittest.main()
