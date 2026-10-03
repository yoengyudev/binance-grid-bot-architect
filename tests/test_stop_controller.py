import tempfile
import unittest
from pathlib import Path

from database import GridDatabase
from exchange_handler import ApiCircuitBreaker
from stop_controller import StopController


class FakeExchange:
    def __init__(self) -> None:
        self.canceled = []

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        self.canceled.append((order_id, symbol))
        if order_id == "filled-before-cancel":
            raise RuntimeError("Already filled")
        if order_id == "unknown-outcome":
            raise RuntimeError("Network unavailable")
        return {"status": "canceled"}

    def fetch_order(self, order_id: str, _symbol: str) -> dict:
        if order_id == "filled-before-cancel":
            return {"status": "closed"}
        raise RuntimeError("Cannot verify order")


class StopControllerTests(unittest.TestCase):
    def test_stop_pauses_and_reconciles_tracked_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = GridDatabase(Path(temporary_directory) / "grid_testnet.sqlite")
            for order_id in ("cancel-me", "filled-before-cancel", "unknown-outcome"):
                database.insert_order(order_id, 0, "BUY", "100", "0.1")
            exchange = FakeExchange()
            controller = StopController(exchange, database, "BTC/USDT")

            result = controller.request_stop()

            self.assertTrue(controller.stop_requested.is_set())
            self.assertEqual(database.get_state("engine_stop_cleanup_pending"), "1")
            self.assertIsNotNone(database.get_state("order_cleanup_alarm"))
            self.assertEqual((result.canceled, result.filled, result.unresolved), (1, 1, 1))
            self.assertEqual(database.get_order("cancel-me")["status"], "CANCELED")
            self.assertEqual(database.get_order("filled-before-cancel")["status"], "FILLED")
            self.assertEqual(
                [order["order_id"] for order in database.fetch_active_grids()],
                ["unknown-outcome"],
            )
            self.assertEqual(len(exchange.canceled), 3)
            journal = GridDatabase(database.path).fetch_transition_journal()
            details = "\n".join(row["error_details"] or "" for row in journal)
            for text in ("Already filled", "Network unavailable", "Cannot verify order",
                         "RuntimeError", "Traceback", "unknown-outcome"):
                self.assertIn(text, details)
            self.assertIn("STOP", [row["trigger_event"] for row in journal])

    def test_open_circuit_defers_all_remaining_cleanup_without_network_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            database = GridDatabase(Path(directory) / "grid_testnet.sqlite")
            for order_id in ("one", "two", "three"):
                database.insert_order(order_id, 0, "BUY", "100", "0.1")
            exchange = FakeExchange()
            exchange.api_circuit_breaker = ApiCircuitBreaker(clock=lambda: 100)
            for _ in range(3):
                exchange.api_circuit_breaker.rate_limited()
            result = StopController(exchange, database, "BTC/USDT").request_stop()
            self.assertEqual(result.unresolved, 3)
            self.assertEqual(exchange.canceled, [])
            self.assertEqual(len(database.fetch_active_grids()), 3)
            self.assertIn("CircuitOpenError", database.fetch_transition_journal()[-1]["error_details"])


if __name__ == "__main__":
    unittest.main()
