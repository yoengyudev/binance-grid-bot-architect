import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import GridDatabase


class GridDatabaseTests(unittest.TestCase):
    def test_order_and_trade_state_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "grid.sqlite3"
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
            database = GridDatabase(Path(temporary_directory) / "grid.sqlite3")
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


if __name__ == "__main__":
    unittest.main()
