import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch

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
        self.exchange.market.return_value = {
            "spot": True, "active": True, "base": "BTC", "quote": "USDT",
        }
        self.exchange.fetch_order.return_value = {
            "id": "seed", "status": "closed", "filled": 0.01,
            "average": 80000, "cost": 800, "fee": None,
        }
        self.exchange.fetch_open_orders.side_effect = (
            lambda _symbol: [] if self.exchange.cancel_all_orders.call_count
            else [{"id": "old-order"}]
        )
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

    def run_utility(self, answer: str = "Y", *, keep_btc_manual=False) -> int:
        preview = io.StringIO()
        with redirect_stdout(preview):
            result = reset_util.hard_reset(
                keep_btc_manual=keep_btc_manual,
                input_fn=lambda _prompt: answer, output=self.messages.append,
            )
        self.preview = preview.getvalue()
        return result

    def add_bot_inventory(self) -> None:
        self.database.insert_order(
            "seed", 0, "BUY", "80000", "0.01", order_type="MARKET",
        )
        self.database.mark_order_filled("seed")
        self.database.set_state("fill_snapshot:seed", json.dumps({
            "filled_base": "0.01", "filled_quote": "800",
            "base_fee": "0", "quote_fee": "0",
        }))

    def test_confirmed_reset_cancels_and_verifies_before_deleting(self) -> None:
        self.assertEqual(self.run_utility(), 0)
        calls = self.exchange.mock_calls
        self.assertLess(
            calls.index(call.set_sandbox_mode(True)),
            calls.index(call.load_markets()),
        )
        self.assertIn("old-order", self.preview)
        self.exchange.cancel_all_orders.assert_called_once_with("BTC/USDT")
        self.assertEqual(self.exchange.fetch_open_orders.call_count, 2)
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
        self.exchange.fetch_open_orders.side_effect = None
        self.exchange.fetch_open_orders.return_value = [{"id": "still-open"}]
        with self.assertRaisesRegex(reset_util.ResetError, "still open"):
            self.run_utility()
        self.assertTrue(self.database_path.exists())

        self.exchange.fetch_open_orders.return_value = [{"id": "still-open"}]
        self.exchange.cancel_all_orders.side_effect = ccxt.NetworkError("timeout")
        with self.assertRaises(ccxt.NetworkError):
            self.run_utility()
        self.assertTrue(self.database_path.exists())

    def test_no_open_orders_skips_cancel_request(self) -> None:
        self.exchange.fetch_open_orders.side_effect = None
        self.exchange.fetch_open_orders.return_value = []
        self.assertEqual(self.run_utility(), 0)
        self.exchange.cancel_all_orders.assert_not_called()
        self.assertFalse(self.database_path.exists())

    def test_tracked_btc_inventory_retains_database_after_cancel(self) -> None:
        self.add_bot_inventory()
        with self.assertRaisesRegex(reset_util.ResetError, "Bot-tracked BTC"):
            self.run_utility()
        self.exchange.cancel_all_orders.assert_called_once()
        self.assertTrue(self.database_path.exists())

    def test_reconciled_bot_btc_can_be_kept_as_manual_holding(self) -> None:
        self.add_bot_inventory()
        self.exchange.fetch_balance.return_value["BTC"].update({
            "free": "1.01", "total": "1.01",
        })
        self.assertEqual(self.run_utility(keep_btc_manual=True), 0)
        self.assertFalse(self.database_path.exists())
        self.assertTrue(any("Retaining 0.01 BTC" in message
                            for message in self.messages))

    def test_manual_holding_requires_free_reconciled_btc(self) -> None:
        self.add_bot_inventory()
        self.exchange.fetch_balance.return_value["BTC"].update({
            "free": "1", "used": "0.01", "total": "1.01",
        })
        with self.assertRaisesRegex(reset_util.ResetError, "BTC account balance"):
            self.run_utility(keep_btc_manual=True)
        self.assertTrue(self.database_path.exists())

    def test_fills_during_shutdown_are_reconciled_before_manual_adoption(self) -> None:
        self.add_bot_inventory()
        self.database.insert_order("late-buy", 1, "BUY", "80000", "0.005")
        self.exchange.fetch_order.side_effect = lambda reference, *_args: {
            "id": reference, "status": "closed",
            "filled": 0.01 if reference == "seed" else 0.005,
            "average": 80000,
            "cost": 800 if reference == "seed" else 400,
            "fee": None,
        }
        self.exchange.fetch_balance.return_value["BTC"].update({
            "free": "1.015", "total": "1.015",
        })
        self.assertEqual(self.run_utility(keep_btc_manual=True), 0)
        self.assertFalse(self.database_path.exists())
        self.assertTrue(any("Retaining 0.015 BTC" in message
                            for message in self.messages))

    def test_untracked_btc_above_baseline_retains_database(self) -> None:
        self.exchange.fetch_balance.return_value["BTC"]["total"] = "1.01"
        with self.assertRaisesRegex(reset_util.ResetError, "exceeds the saved"):
            self.run_utility()
        self.assertTrue(self.database_path.exists())


if __name__ == "__main__":
    unittest.main()
