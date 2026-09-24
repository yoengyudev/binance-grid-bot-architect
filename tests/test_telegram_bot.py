import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from database import GridDatabase
from telegram_bot import StopController, TelegramBot


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


class TelegramBotTests(unittest.TestCase):
    def test_stop_pauses_and_reconciles_tracked_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = GridDatabase(Path(temporary_directory) / "grid.sqlite3")
            for order_id in ("cancel-me", "filled-before-cancel", "unknown-outcome"):
                database.insert_order(order_id, 0, "BUY", "100", "0.1")
            exchange = FakeExchange()
            controller = StopController(exchange, database, "BTC/USDT")

            result = controller.request_stop()

            self.assertTrue(controller.stop_requested.is_set())
            self.assertEqual((result.canceled, result.filled, result.unresolved), (1, 1, 1))
            self.assertEqual(database.get_order("cancel-me")["status"], "CANCELED")
            self.assertEqual(database.get_order("filled-before-cancel")["status"], "FILLED")
            self.assertEqual(
                [order["order_id"] for order in database.fetch_active_grids()],
                ["unknown-outcome"],
            )
            self.assertEqual(len(exchange.canceled), 3)

    def test_stop_command_rejects_other_users_and_chats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = GridDatabase(Path(temporary_directory) / "grid.sqlite3")
            controller = StopController(FakeExchange(), database, "BTC/USDT")
            bot = TelegramBot("123456:ABCDEF", 12345, controller)
            replies = []

            async def reply_text(text: str) -> None:
                replies.append(text)

            def update(user_id: int, chat_id: int, chat_type: str):
                return SimpleNamespace(
                    effective_user=SimpleNamespace(id=user_id),
                    effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
                    effective_message=SimpleNamespace(reply_text=reply_text),
                )

            asyncio.run(bot._handle_stop(update(999, 12345, "private"), None))
            asyncio.run(bot._handle_stop(update(12345, -12345, "group"), None))
            self.assertFalse(controller.stop_requested.is_set())
            self.assertEqual(replies, [])

            asyncio.run(bot._handle_stop(update(12345, 12345, "private"), None))
            self.assertTrue(controller.stop_requested.is_set())
            self.assertIn("Trading stopped", replies[0])


if __name__ == "__main__":
    unittest.main()
