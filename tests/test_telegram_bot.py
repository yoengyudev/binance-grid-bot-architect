import asyncio
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler

from database import GridDatabase
from telegram_bot import StopController, TelegramBot, _format_open_order_messages


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
    def test_start_menu_and_callbacks_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = GridDatabase(Path(temporary_directory) / "grid.sqlite3")
            controller = StopController(FakeExchange(), database, "BTC/USDT")
            grid = SimpleNamespace(
                grid_status=lambda: (Decimal("100"), Decimal("75"), Decimal("125"),
                                     20, Decimal("70")),
                open_order_summary=lambda _price: (1, 1, Decimal("99"), Decimal("101")),
                list_open_orders=lambda: (
                    "BTC", "USDT", [(Decimal("0.01"), Decimal("99"))],
                    [(Decimal("0.02"), Decimal("101"))],
                ),
            )
            bot = TelegramBot("123456:ABCDEF", 12345, controller, grid)
            self.assertTrue(any(
                isinstance(handler, CallbackQueryHandler)
                for handlers in bot.application.handlers.values()
                for handler in handlers
            ))
            replies = []
            acknowledgements = []

            async def reply_text(message, **kwargs):
                replies.append((message, kwargs))

            async def answer(*args, **kwargs):
                acknowledgements.append((args, kwargs))

            def update(user_id, chat_id, action=None):
                return SimpleNamespace(
                    effective_user=SimpleNamespace(id=user_id),
                    effective_chat=SimpleNamespace(id=chat_id, type="private"),
                    effective_message=SimpleNamespace(reply_text=reply_text),
                    callback_query=(SimpleNamespace(data=action, answer=answer)
                                    if action else None),
                )

            asyncio.run(bot._handle_start(update(999, 12345), None))
            self.assertEqual(replies, [])
            asyncio.run(bot._handle_start(update(12345, 12345), None))
            welcome, options = replies.pop()
            self.assertIn("<b>Welcome to BTC/USDT Grid Master</b>", welcome)
            self.assertIn("<i>Your automated trading engine is online.</i>", welcome)
            self.assertIn("<b>Current Mode:</b> Spot Testnet", welcome)
            self.assertIn("<b>Security:</b> Owner Access Only", welcome)
            self.assertEqual(options["parse_mode"], ParseMode.HTML)
            buttons = options["reply_markup"].inline_keyboard
            self.assertEqual([len(row) for row in buttons], [2, 2, 1])
            self.assertEqual(
                [button.text for row in buttons for button in row],
                ["📊 Bot Status", "📋 Open Orders", "⚙️ Set Grid Bounds",
                 "🛡️ Set Stop-Loss", "🛑 Stop Bot"],
            )
            self.assertEqual(
                [button.callback_data for row in buttons for button in row],
                ["menu:status", "menu:orders", "menu:setgrid", "menu:setstop",
                 "menu:stop"],
            )

            asyncio.run(bot._handle_menu_callback(update(999, 12345, "menu:stop"), None))
            self.assertFalse(controller.stop_requested.is_set())
            self.assertEqual(replies, [])
            self.assertEqual(acknowledgements[-1][1],
                             {"show_alert": True})

            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:status"), None))
            self.assertIn("Total grid levels: 20", replies.pop()[0])
            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:orders"), None))
            self.assertIn("Buy 0.01 BTC @ 99 USDT", replies.pop()[0])
            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:setgrid"), None))
            self.assertIn("/setgrid <lower> <upper>", replies.pop()[0])
            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:setstop"), None))
            self.assertIn("/setstop <price>", replies.pop()[0])
            self.assertFalse(controller.stop_requested.is_set())
            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:stop"), None))
            self.assertTrue(controller.stop_requested.is_set())
            self.assertIn("Trading stopped", replies.pop()[0])

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

    def test_status_orders_and_grid_controls_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = GridDatabase(Path(temporary_directory) / "grid.sqlite3")
            controller = StopController(FakeExchange(), database, "BTC/USDT")

            class GridStub:
                def __init__(self):
                    self.requests = []
                    self.stop_requests = []
                    self.status_calls = 0
                    self.order_list_calls = 0
                    self.fail_price = False
                    self.fail_orders = False
                    self.fail_order_list = False
                    self.slow_price = False
                    self.slow_order_list = False

                def grid_status(self):
                    self.status_calls += 1
                    if self.slow_price:
                        time.sleep(0.05)
                    if self.fail_price:
                        raise RuntimeError("Ticker unavailable")
                    return (Decimal("100"), Decimal("75"), Decimal("125"), 20,
                            Decimal("70"))

                def grid_configuration(self):
                    return (Decimal("75"), Decimal("125"), 20, Decimal("70"))

                def open_order_summary(self, _price):
                    if self.fail_orders:
                        raise RuntimeError("Exchange unavailable")
                    return (3, 2, Decimal("99"), Decimal("101"))

                def list_open_orders(self):
                    self.order_list_calls += 1
                    if self.slow_order_list:
                        time.sleep(0.05)
                    if self.fail_order_list:
                        raise RuntimeError("Exchange unavailable")
                    return (
                        "BTC", "USDT",
                        [(Decimal("0.01"), Decimal("82500"))],
                        [(Decimal("0.02"), Decimal("87500"))],
                    )

                def request_grid_reset(self, lower, upper):
                    self.requests.append((lower, upper))

                def set_stop_loss(self, price):
                    self.stop_requests.append(price)
                    if Decimal(price) >= Decimal("75"):
                        raise ValueError(
                            "❌ Rejected: Stop-loss must be lower than the current lower bound."
                        )
                    return Decimal(price)

            grid = GridStub()
            bot = TelegramBot("123456:ABCDEF", 12345, controller, grid)
            replies = []

            async def reply_text(value):
                replies.append(value)

            def update(user_id, chat_id, chat_type):
                return SimpleNamespace(
                    effective_user=SimpleNamespace(id=user_id),
                    effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
                    effective_message=SimpleNamespace(reply_text=reply_text),
                )

            outsider = update(999, 12345, "private")
            owner = update(12345, 12345, "private")
            asyncio.run(bot._handle_status(outsider, None))
            asyncio.run(bot._handle_orders(outsider, None))
            asyncio.run(bot._handle_setgrid(outsider, SimpleNamespace(args=["75", "125"])))
            asyncio.run(bot._handle_setstop(outsider, SimpleNamespace(args=["70"])))
            self.assertEqual(grid.status_calls, 0)
            self.assertEqual(grid.order_list_calls, 0)
            self.assertEqual(grid.requests, [])
            self.assertEqual(grid.stop_requests, [])
            self.assertEqual(replies, [])

            asyncio.run(bot._handle_status(owner, None))
            self.assertIn("Total grid levels: 20", replies[-1])
            self.assertIn("Stop-loss: 70 USDT", replies[-1])
            self.assertIn("BUY limit orders: 3", replies[-1])
            self.assertIn("SELL limit orders: 2", replies[-1])
            self.assertIn("Closest BUY: 99 USDT", replies[-1])
            self.assertIn("Closest SELL: 101 USDT", replies[-1])
            grid.fail_orders = True
            asyncio.run(bot._handle_status(owner, None))
            self.assertIn("Active bounds: 75–125 USDT", replies[-1])
            self.assertIn("Open orders unavailable", replies[-1])
            grid.fail_orders = False
            grid.slow_price = True
            with patch("telegram_bot.STATUS_API_TIMEOUT_SECONDS", 0.01):
                asyncio.run(bot._handle_status(owner, None))
            self.assertIn("current price: unavailable", replies[-1])
            self.assertIn("BUY limit orders: 3", replies[-1])
            asyncio.run(bot._handle_orders(owner, None))
            self.assertIn("🟢 BUY Orders:", replies[-1])
            self.assertIn("Buy 0.01 BTC @ 82,500 USDT", replies[-1])
            self.assertIn("🔴 SELL Orders:", replies[-1])
            self.assertIn("Sell 0.02 BTC @ 87,500 USDT", replies[-1])
            grid.fail_order_list = True
            asyncio.run(bot._handle_orders(owner, None))
            self.assertIn("Could not fetch open orders", replies[-1])
            grid.fail_order_list = False
            grid.slow_order_list = True
            with patch("telegram_bot.STATUS_API_TIMEOUT_SECONDS", 0.01):
                asyncio.run(bot._handle_orders(owner, None))
            self.assertIn("Could not fetch open orders", replies[-1])
            grid.slow_order_list = False
            asyncio.run(bot._handle_setgrid(owner, SimpleNamespace(args=["75"])))
            self.assertEqual(grid.requests, [])
            asyncio.run(bot._handle_setgrid(owner, SimpleNamespace(args=["75", "125"])))
            self.assertEqual(grid.requests, [("75", "125")])
            self.assertEqual(
                replies[-1],
                "✅ Grid bounds updated. Canceling old orders and rebuilding grid...",
            )

            asyncio.run(bot._handle_setstop(owner, SimpleNamespace(args=["75"])))
            self.assertEqual(
                replies[-1],
                "❌ Rejected: Stop-loss must be lower than the current lower bound.",
            )
            asyncio.run(bot._handle_setstop(owner, SimpleNamespace(args=["70"])))
            self.assertEqual(replies[-1], "✅ Stop-loss successfully updated to: $70")
            self.assertEqual(grid.stop_requests, ["75", "70"])

    def test_large_order_list_is_split_without_losing_orders(self) -> None:
        buys = [(Decimal("0.01"), Decimal(80000 + index)) for index in range(150)]
        messages = _format_open_order_messages("BTC", "USDT", buys, [])
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 4000 for message in messages))
        self.assertEqual(sum(message.count("Buy 0.01 BTC @") for message in messages), 150)
        self.assertIn("🔴 SELL Orders:\nNone", messages[-1])


if __name__ == "__main__":
    unittest.main()
