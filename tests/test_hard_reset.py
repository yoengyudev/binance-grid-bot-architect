import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ccxt

import hard_reset as reset_util
from database import GridDatabase


class HardResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database_path = Path(self.directory.name) / "bot.sqlite3"
        self.database = GridDatabase(self.database_path)
        self.database.set_state("grid_run", json.dumps({
            "baseline_base": "1", "anchor": "80000", "fingerprint": "test",
        }))
        self.exchange = Mock()
        self.exchange.market.return_value = {"spot": True, "active": True}
        self.exchange.fetch_open_orders.return_value = []
        self.exchange.fetch_balance.return_value = {
            "BTC": {"free": "1", "used": "0", "total": "1"},
        }
        self.messages = []
        environment = patch.dict("os.environ", {
            "GRID_BOT_DB_PATH": str(self.database_path),
            "BINANCE_TESTNET_API_KEY": "test-key",
            "BINANCE_TESTNET_API_SECRET": "test-secret",
        })
        environment.start()
        self.addCleanup(environment.stop)
        for target, value in (
            ("hard_reset.load_dotenv", None),
            ("hard_reset.load_config", {
                "grid": {"symbol": "BTC/USDT", "investment_quote": 100},
            }),
            ("hard_reset._require_stopped_service", None),
            ("hard_reset.create_exchange", self.exchange),
        ):
            fixture = patch(target, return_value=value)
            fixture.start()
            self.addCleanup(fixture.stop)

    def run_utility(self, answer: str = "Y") -> int:
        return reset_util.hard_reset(
            input_fn=lambda _prompt: answer, output=self.messages.append,
        )

    def test_confirmed_reset_cancels_and_verifies_before_deleting(self) -> None:
        self.assertEqual(self.run_utility(), 0)
        self.exchange.cancel_all_orders.assert_called_once_with("BTC/USDT")
        self.exchange.fetch_open_orders.assert_called_once_with("BTC/USDT")
        self.assertFalse(self.database_path.exists())
        self.assertIn(
            "Successfully deleted the SQLite database file.", self.messages,
        )

    def test_declined_reset_leaves_orders_and_database_untouched(self) -> None:
        self.assertEqual(self.run_utility("n"), 0)
        self.exchange.cancel_all_orders.assert_not_called()
        self.assertTrue(self.database_path.exists())

    def test_running_service_blocks_reset_before_exchange_call(self) -> None:
        with patch.object(
            reset_util, "_require_stopped_service",
            side_effect=reset_util.ResetError("Stop the bot service."),
        ):
            with self.assertRaisesRegex(reset_util.ResetError, "Stop the bot"):
                self.run_utility()
        self.exchange.cancel_all_orders.assert_not_called()
        self.assertTrue(self.database_path.exists())

    def test_unconfirmed_exchange_cleanup_retains_database(self) -> None:
        self.exchange.fetch_open_orders.return_value = [{"id": "still-open"}]
        with self.assertRaisesRegex(reset_util.ResetError, "still open"):
            self.run_utility()
        self.assertTrue(self.database_path.exists())

        self.exchange.fetch_open_orders.return_value = []
        self.exchange.cancel_all_orders.side_effect = ccxt.NetworkError("timeout")
        with self.assertRaises(ccxt.NetworkError):
            self.run_utility()
        self.assertTrue(self.database_path.exists())

    def test_tracked_btc_inventory_retains_database_after_cancel(self) -> None:
        self.database.insert_order(
            "seed", 0, "BUY", "80000", "0.01", order_type="MARKET",
        )
        self.database.mark_order_filled("seed")
        self.database.set_state("fill_snapshot:seed", json.dumps({
            "filled_base": "0.01", "filled_quote": "800",
            "base_fee": "0", "quote_fee": "0",
        }))
        with self.assertRaisesRegex(reset_util.ResetError, "Bot-tracked BTC"):
            self.run_utility()
        self.exchange.cancel_all_orders.assert_called_once()
        self.assertTrue(self.database_path.exists())

    def test_untracked_btc_above_baseline_retains_database(self) -> None:
        self.exchange.fetch_balance.return_value["BTC"]["total"] = "1.01"
        with self.assertRaisesRegex(reset_util.ResetError, "exceeds the saved"):
            self.run_utility()
        self.assertTrue(self.database_path.exists())


if __name__ == "__main__":
    unittest.main()
