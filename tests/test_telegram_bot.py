import asyncio
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, MessageHandler

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
            bot = TelegramBot("123456:ABCDEF", 12345, controller, grid,
                              action_pin="1234")
            self.assertTrue(any(
                isinstance(handler, CallbackQueryHandler)
                for handlers in bot.application.handlers.values()
                for handler in handlers
            ))
            self.assertTrue(any(
                isinstance(handler, MessageHandler)
                for handlers in bot.application.handlers.values()
                for handler in handlers
            ))
            replies = []
            edits = []
            acknowledgements = []
            events = []
            not_modified = [False]

            async def reply_text(message, **kwargs):
                replies.append((message, kwargs))

            async def edit_message_text(text, **kwargs):
                events.append("edit")
                if not_modified[0]:
                    raise BadRequest("Message is not modified")
                edits.append((text, kwargs))

            async def answer(*args, **kwargs):
                events.append("answer")
                acknowledgements.append((args, kwargs))

            def update(user_id, chat_id, action=None):
                return SimpleNamespace(
                    effective_user=SimpleNamespace(id=user_id),
                    effective_chat=SimpleNamespace(id=chat_id, type="private"),
                    effective_message=SimpleNamespace(reply_text=reply_text),
                    callback_query=(SimpleNamespace(
                        data=action, answer=answer,
                        edit_message_text=edit_message_text,
                        message=SimpleNamespace(message_id=77),
                    )
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
            self.assertEqual([len(row) for row in buttons], [1, 1])
            self.assertEqual(
                [button.text for row in buttons for button in row],
                ["👁️ View Analytics", "🔐 Execute Actions"],
            )
            self.assertEqual(
                [button.callback_data for row in buttons for button in row],
                ["menu:views", "menu:actions"],
            )

            asyncio.run(bot._handle_menu_callback(update(999, 12345, "menu:stop"), None))
            self.assertFalse(controller.stop_requested.is_set())
            self.assertEqual(replies, [])
            self.assertEqual(edits, [])
            self.assertEqual(acknowledgements[-1][1],
                             {"text": "Not authorized.", "show_alert": True})

            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:views"), None))
            self.assertEqual(
                [button.text for row in edits[-1][1]["reply_markup"].inline_keyboard
                 for button in row],
                ["📊 Bot Status", "📋 Open Orders", "🔙 Back"],
            )
            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:status"), None))
            self.assertIn("📊 <b>BOT STATUS &amp; ANALYTICS</b>", edits[-1][0])
            self.assertIn("• <b>Levels:</b> 20", edits[-1][0])
            self.assertEqual(edits[-1][1]["parse_mode"], ParseMode.HTML)
            self.assertEqual(edits[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data,
                             "menu:views")
            self.assertEqual(events[-2:], ["edit", "answer"])
            self.assertEqual(replies, [])

            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:root"), None))
            self.assertEqual(edits[-1][0], welcome)
            self.assertEqual(edits[-1][1]["parse_mode"], ParseMode.HTML)
            self.assertEqual([len(row) for row in edits[-1][1]["reply_markup"].inline_keyboard],
                             [1, 1])

            not_modified[0] = True
            answer_count = len(acknowledgements)
            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:root"), None))
            self.assertEqual(len(acknowledgements), answer_count + 1)
            self.assertEqual(events[-2:], ["edit", "answer"])
            not_modified[0] = False

            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:orders"), None))
            self.assertIn("📋 <b>OPEN ORDERS</b>", edits[-1][0])
            self.assertIn("• Buy <b>0.01 BTC</b> @ <b>99 USDT</b>", edits[-1][0])
            self.assertEqual(edits[-1][1]["parse_mode"], ParseMode.HTML)
            self.assertEqual(edits[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data,
                             "menu:views")

            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:actions"), None))
            self.assertIn("ENTER ACTION PIN", edits[-1][0])
            self.assertEqual([len(row) for row in edits[-1][1]["reply_markup"].inline_keyboard],
                             [3, 3, 3, 3])
            self.assertFalse(bot._actions_unlocked())
            for digit in "1234":
                asyncio.run(bot._handle_menu_callback(update(12345, 12345, f"pin:{digit}"), None))
            self.assertTrue(bot._actions_unlocked())
            self.assertIn("EXECUTE ACTIONS", edits[-1][0])
            self.assertEqual(
                [button.text for row in edits[-1][1]["reply_markup"].inline_keyboard
                 for button in row],
                ["⚙️ Set Grid Bounds", "🛡️ Set Stop-Loss", "🛑 Stop Bot",
                 "🔒 Lock Session Now", "🔙 Back"],
            )
            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:setgrid"), None))
            self.assertIn("Send the new bounds as <code>lower upper</code>", edits[-1][0])
            self.assertEqual(bot._pending_menu_input.action, "grid")
            self.assertEqual(edits[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data,
                             "menu:actions")
            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:setstop"), None))
            self.assertIn("Send the new stop-loss price", edits[-1][0])
            self.assertEqual(bot._pending_menu_input.action, "stop")
            self.assertEqual(edits[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data,
                             "menu:actions")
            self.assertFalse(controller.stop_requested.is_set())
            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:lock"), None))
            self.assertFalse(bot._actions_unlocked())
            self.assertIn("Session locked", edits[-1][0])
            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:stop"), None))
            self.assertIn("ENTER ACTION PIN", edits[-1][0])
            self.assertFalse(controller.stop_requested.is_set())
            for digit in "1234":
                asyncio.run(bot._handle_menu_callback(update(12345, 12345, f"pin:{digit}"), None))
            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:stop"), None))
            self.assertIsNone(bot._pending_menu_input)
            self.assertTrue(controller.stop_requested.is_set())
            self.assertIn("Trading stopped", edits[-1][0])
            self.assertEqual(replies, [])
            asyncio.run(bot._handle_menu_callback(update(12345, 12345, "menu:setgrid"), None))
            asyncio.run(bot._handle_start(update(12345, 12345), None))
            self.assertIsNone(bot._pending_menu_input)

    def test_pin_lockout_expiry_and_unconfigured_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = GridDatabase(Path(temporary_directory) / "grid.sqlite3")
            controller = StopController(FakeExchange(), database, "BTC/USDT")
            bot = TelegramBot("123456:ABCDEF", 12345, controller,
                              action_pin="1234")
            now = [10.0]
            bot._clock = lambda: now[0]
            edits = []
            answers = []

            async def edit(text, **_kwargs):
                edits.append(text)

            async def answer(**kwargs):
                answers.append(kwargs)

            def callback(action, message_id=77):
                return SimpleNamespace(
                    effective_user=SimpleNamespace(id=12345),
                    effective_chat=SimpleNamespace(id=12345, type="private"),
                    callback_query=SimpleNamespace(
                        data=action, message=SimpleNamespace(message_id=message_id),
                        edit_message_text=edit, answer=answer,
                    ),
                )

            def click(action, message_id=77):
                asyncio.run(bot._handle_menu_callback(callback(action, message_id), None))

            click("menu:actions")
            click("pin:1")
            click("pin:clear")
            self.assertEqual(bot._pin_buffer, "")
            click("pin:1", message_id=88)
            self.assertEqual(bot._pin_buffer, "")
            self.assertEqual(answers[-1]["show_alert"], True)

            for _ in range(3):
                for digit in "0000":
                    click(f"pin:{digit}")
            self.assertFalse(bot._actions_unlocked())
            self.assertIn("Too many attempts", edits[-1])
            click("pin:1")
            self.assertEqual(bot._pin_buffer, "")
            self.assertEqual(answers[-1]["show_alert"], True)

            now[0] = 71.0
            for digit in "1234":
                click(f"pin:{digit}")
            self.assertTrue(bot._actions_unlocked())
            self.assertEqual(bot.session_expiry, 371.0)
            now[0] = 372.0
            click("menu:stop")
            self.assertIn("ENTER ACTION PIN", edits[-1])
            self.assertFalse(controller.stop_requested.is_set())
            click("pin:cancel")
            self.assertIn("Welcome", edits[-1])

            unconfigured = TelegramBot("123456:ABCDEF", 12345, controller,
                                       action_pin="")
            asyncio.run(unconfigured._handle_menu_callback(callback("menu:actions"), None))
            self.assertIn("ACTIONS UNAVAILABLE", edits[-1])
            self.assertFalse(controller.stop_requested.is_set())

    def test_menu_input_deletes_text_and_edits_same_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = GridDatabase(Path(temporary_directory) / "grid.sqlite3")
            controller = StopController(FakeExchange(), database, "BTC/USDT")
            events = []
            edits = []

            class GridStub:
                def request_grid_reset(self, lower, upper):
                    events.append(("grid", lower, upper))
                    if lower == "bad":
                        raise ValueError("Enter a valid lower price.")

                def set_stop_loss(self, price):
                    events.append(("stop", price))
                    if price == "bad":
                        raise ValueError("Enter a valid stop-loss price.")
                    return Decimal(price)

            bot = TelegramBot("123456:ABCDEF", 12345, controller, GridStub(),
                              action_pin="1234")
            now = [100.0]
            bot._clock = lambda: now[0]
            bot.session_expiry = now[0] + 300

            async def query_edit(text, **kwargs):
                edits.append(("callback", text, kwargs))

            async def answer(**_kwargs):
                events.append("answer")

            def callback(action):
                return SimpleNamespace(
                    effective_user=SimpleNamespace(id=12345),
                    effective_chat=SimpleNamespace(id=12345, type="private"),
                    callback_query=SimpleNamespace(
                        data=action, message=SimpleNamespace(message_id=77),
                        edit_message_text=query_edit, answer=answer,
                    ),
                )

            async def bot_edit(**kwargs):
                edits.append(("message", kwargs["text"], kwargs))
                events.append("edit")

            context = SimpleNamespace(bot=SimpleNamespace(edit_message_text=bot_edit))

            def message(text, user_id=12345, delete_fails=False):
                async def delete():
                    events.append("delete")
                    if delete_fails:
                        raise RuntimeError("Delete failed")
                    return True

                return SimpleNamespace(
                    effective_user=SimpleNamespace(id=user_id),
                    effective_chat=SimpleNamespace(id=12345, type="private"),
                    effective_message=SimpleNamespace(text=text, delete=delete),
                )

            asyncio.run(bot._handle_menu_callback(callback("menu:setgrid"), None))
            self.assertEqual(bot._pending_menu_input.message_id, 77)
            self.assertIn("Send the new bounds", edits[-1][1])

            asyncio.run(bot._handle_menu_input(message("bad"), context))
            self.assertEqual(events[-2:], ["delete", "edit"])
            self.assertIn("Enter exactly two numbers", edits[-1][1])
            self.assertEqual(bot._pending_menu_input.action, "grid")

            asyncio.run(bot._handle_menu_input(message("bad 125"), context))
            self.assertEqual(events[-3:], ["delete", ("grid", "bad", "125"), "edit"])
            self.assertIn("Enter a valid lower price", edits[-1][1])
            self.assertEqual(bot._pending_menu_input.action, "grid")

            asyncio.run(bot._handle_menu_input(message("75 125"), context))
            self.assertEqual(events[-3:], ["delete", ("grid", "75", "125"), "edit"])
            self.assertIn("GRID BOUNDS UPDATED", edits[-1][1])
            self.assertEqual(edits[-1][2]["message_id"], 77)
            self.assertEqual(edits[-1][2]["reply_markup"].inline_keyboard[0][0].callback_data,
                             "menu:actions")
            self.assertIsNone(bot._pending_menu_input)

            asyncio.run(bot._handle_menu_callback(callback("menu:setstop"), None))
            asyncio.run(bot._handle_menu_input(message("70", user_id=999), context))
            self.assertEqual(bot._pending_menu_input.action, "stop")
            asyncio.run(bot._handle_menu_input(message("70", delete_fails=True), context))
            self.assertIn("Could not delete your message", edits[-1][1])
            self.assertEqual(bot._pending_menu_input.action, "stop")
            asyncio.run(bot._handle_menu_input(message("bad"), context))
            self.assertIn("Enter a valid stop-loss price", edits[-1][1])
            asyncio.run(bot._handle_menu_input(message("70"), context))
            self.assertEqual(events[-3:], ["delete", ("stop", "70"), "edit"])
            self.assertIn("STOP-LOSS UPDATED", edits[-1][1])
            self.assertIsNone(bot._pending_menu_input)

            asyncio.run(bot._handle_menu_callback(callback("menu:setgrid"), None))
            asyncio.run(bot._handle_menu_callback(callback("menu:back"), None))
            self.assertIsNone(bot._pending_menu_input)
            before = len(events)
            asyncio.run(bot._handle_menu_input(message("75 125"), context))
            self.assertEqual(len(events), before)

            asyncio.run(bot._handle_menu_callback(callback("menu:setgrid"), None))
            now[0] = 401.0
            asyncio.run(bot._handle_menu_input(message("75 125"), context))
            self.assertEqual(events[-2:], ["delete", "edit"])
            self.assertIn("ENTER ACTION PIN", edits[-1][1])
            self.assertIsNone(bot._pending_menu_input)

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
            bot = TelegramBot("123456:ABCDEF", 12345, controller,
                              action_pin="1234")
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
            self.assertFalse(controller.stop_requested.is_set())
            self.assertIn("Unlock Execute Actions", replies[-1])
            bot.session_expiry = bot._clock() + 300
            asyncio.run(bot._handle_stop(update(12345, 12345, "private"), None))
            self.assertTrue(controller.stop_requested.is_set())
            self.assertIn("Trading stopped", replies[-1])

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
            bot = TelegramBot("123456:ABCDEF", 12345, controller, grid,
                              action_pin="1234")
            bot.session_expiry = bot._clock() + 300
            replies = []
            reply_options = []

            async def reply_text(value, **kwargs):
                replies.append(value)
                reply_options.append(kwargs)

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
            self.assertIn("• <b>Levels:</b> 20", replies[-1])
            self.assertIn("• <b>Stop-Loss:</b> 70 USDT", replies[-1])
            self.assertIn("🟢 <b>BUY Limits:</b> 3 (Closest: 99 USDT)", replies[-1])
            self.assertIn("🔴 <b>SELL Limits:</b> 2 (Closest: 101 USDT)", replies[-1])
            self.assertEqual(reply_options[-1]["parse_mode"], ParseMode.HTML)
            grid.fail_orders = True
            asyncio.run(bot._handle_status(owner, None))
            self.assertIn("• <b>Bounds:</b> 75 - 125 USDT", replies[-1])
            self.assertIn("🟢 <b>BUY Limits:</b> Unavailable", replies[-1])
            grid.fail_orders = False
            grid.slow_price = True
            with patch("telegram_bot.STATUS_API_TIMEOUT_SECONDS", 0.01):
                asyncio.run(bot._handle_status(owner, None))
            self.assertIn("📈 <b>Current Price:</b> Unavailable", replies[-1])
            self.assertIn("🟢 <b>BUY Limits:</b> 3", replies[-1])
            asyncio.run(bot._handle_orders(owner, None))
            self.assertIn("🟢 <b>BUY LIMIT ORDERS</b>", replies[-1])
            self.assertIn("• Buy <b>0.01 BTC</b> @ <b>82,500 USDT</b>", replies[-1])
            self.assertIn("🔴 <b>SELL LIMIT ORDERS</b>", replies[-1])
            self.assertIn("• Sell <b>0.02 BTC</b> @ <b>87,500 USDT</b>", replies[-1])
            self.assertEqual(reply_options[-1]["parse_mode"], ParseMode.HTML)
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
        self.assertEqual(sum(message.count("Buy <b>0.01 BTC</b> @")
                             for message in messages), 150)
        self.assertIn("🔴 <b>SELL LIMIT ORDERS</b>\n• <i>No open SELL orders</i>",
                      messages[-1])


if __name__ == "__main__":
    unittest.main()
