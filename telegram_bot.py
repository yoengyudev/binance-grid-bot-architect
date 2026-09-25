"""Owner-only Telegram alerts and grid controls."""

import argparse
import asyncio
import hmac
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from html import escape
from pathlib import Path
from threading import Event, Lock, RLock
from typing import Any, Optional, Tuple

from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

from database import GridDatabase


BASE_DIR = Path(__file__).resolve().parent
STATUS_API_TIMEOUT_SECONDS = 12
TELEGRAM_MESSAGE_CHUNK_LENGTH = 4000
ACTION_SESSION_SECONDS = 300
PIN_LOCKOUT_SECONDS = 60
LOGGER = logging.getLogger(__name__)


def _format_balance_amount(value: Optional[Decimal], minimum_decimals: int) -> str:
    if value is None:
        return "Unavailable"
    amount = format(value, ",f")
    _, separator, fractional = amount.partition(".")
    if not separator:
        amount += "."
    return amount + "0" * max(0, minimum_decimals - len(fractional))


def _format_open_order_messages(
    base: str, quote: str, buys: Any, sells: Any
) -> list[str]:
    """Format live orders as HTML without exceeding Telegram's message limit."""
    messages: list[str] = []
    title = "📋 <b>OPEN ORDERS</b>\n━━━━━━━━━━━━━━━━━━"
    current = title
    for heading, side, orders in (
        ("🟢 <b>BUY LIMIT ORDERS</b>", "Buy", buys),
        ("🔴 <b>SELL LIMIT ORDERS</b>", "Sell", sells),
    ):
        if len(current) + len(heading) + 2 > TELEGRAM_MESSAGE_CHUNK_LENGTH:
            messages.append(current)
            current = title
        current += "\n\n" + heading
        lines = []
        for amount, price in orders:
            amount_text = (f"{format(amount.normalize(), ',f')} {escape(base)}"
                           if amount is not None else "amount unavailable")
            price_text = (f"{format(price.normalize(), ',f')} {escape(quote)}"
                          if price is not None else "price unavailable")
            lines.append(f"• {side} <b>{amount_text}</b> @ <b>{price_text}</b>")
        for line in lines or [f"• <i>No open {side.upper()} orders</i>"]:
            if len(current) + len(line) + 1 > TELEGRAM_MESSAGE_CHUNK_LENGTH:
                messages.append(current)
                current = f"{title}\n\n{heading} <i>(continued)</i>"
            current += "\n" + line
    messages.append(current)
    return messages


def load_telegram_credentials() -> Tuple[str, int]:
    """Read secrets from .env without printing them."""
    load_dotenv(dotenv_path=BASE_DIR / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    owner_text = os.getenv("TELEGRAM_OWNER_CHAT_ID", "").strip()
    if not token:
        raise ValueError("Set TELEGRAM_BOT_TOKEN in .env.")
    try:
        owner_id = int(owner_text)
    except ValueError as error:
        raise ValueError("Set TELEGRAM_OWNER_CHAT_ID to your positive private chat ID.") from error
    if owner_id <= 0:
        raise ValueError("TELEGRAM_OWNER_CHAT_ID must be a positive private chat ID.")
    return token, owner_id


@dataclass(frozen=True)
class StopResult:
    canceled: int = 0
    filled: int = 0
    unresolved: int = 0


@dataclass(frozen=True)
class PendingMenuInput:
    action: str
    chat_id: int
    message_id: int


class _PendingMenuInputFilter(filters.MessageFilter):
    """Match owner text only while a settings prompt awaits a reply."""

    def __init__(self, telegram_bot: "TelegramBot") -> None:
        super().__init__(name="PendingMenuInput")
        self.telegram_bot = telegram_bot

    def filter(self, message: Any) -> bool:
        pending = self.telegram_bot._pending_menu_input
        return pending is not None and message.chat_id == pending.chat_id


class StopController:
    """Pause the future trading loop and cancel only orders tracked by this bot."""

    def __init__(
        self,
        exchange: Any,
        database: GridDatabase,
        symbol: str,
        exchange_lock: Optional[Any] = None,
    ) -> None:
        self.exchange = exchange
        self.database = database
        self.symbol = symbol
        self.stop_requested = Event()
        self._lock = Lock()
        self.exchange_lock = exchange_lock or RLock()

    def _exchange_call(self, method: Any, *args: Any) -> Any:
        with self.exchange_lock:
            return method(*args)

    def request_stop(self) -> StopResult:
        """Signal stop before making any exchange calls; retry unresolved rows later."""
        self.stop_requested.set()
        return self.cancel_tracked_orders()

    def cancel_tracked_orders(self) -> StopResult:
        """Cancel tracked orders without pausing the trading loop."""
        canceled = filled = unresolved = 0
        with self._lock:
            for order in self.database.fetch_active_grids():
                order_id = order["order_id"]
                reference = order.get("exchange_order_id") or order_id
                params = (
                    {"origClientOrderId": order["client_order_id"]}
                    if order.get("client_order_id") else None
                )
                try:
                    try:
                        if params is None:
                            response = self._exchange_call(
                                self.exchange.cancel_order, reference, self.symbol
                            )
                        else:
                            response = self._exchange_call(
                                self.exchange.cancel_order, reference, self.symbol, params
                            )
                    except Exception:
                        # The order may have filled just before cancellation.
                        response = None

                    status = response.get("status") if isinstance(response, dict) else None
                    if status not in ("canceled", "closed"):
                        if params is None:
                            current = self._exchange_call(
                                self.exchange.fetch_order, reference, self.symbol
                            )
                        else:
                            current = self._exchange_call(
                                self.exchange.fetch_order, reference, self.symbol, params
                            )
                        status = current.get("status") if isinstance(current, dict) else None

                    if status == "canceled":
                        self.database.update_order_status(order_id, "CANCELED")
                        canceled += 1
                    elif status == "closed":
                        self.database.mark_order_filled(order_id)
                        filled += 1
                    else:
                        unresolved += 1
                except Exception:
                    # Keep the row active so the next run can reconcile it.
                    unresolved += 1
        return StopResult(canceled, filled, unresolved)


class TelegramBot:
    """Use inside the Phase 4 asyncio loop for alerts and polling."""

    def __init__(self, token: str, owner_chat_id: int, stop_controller: StopController,
                 grid_bot: Optional[Any] = None,
                 action_pin: Optional[str] = None) -> None:
        if not token or owner_chat_id <= 0:
            raise ValueError("A bot token and positive owner private chat ID are required.")
        self.owner_chat_id = owner_chat_id
        self.stop_controller = stop_controller
        self.grid_bot = grid_bot
        self._pending_menu_input: Optional[PendingMenuInput] = None
        self._action_pin = (
            action_pin if action_pin is not None
            else os.getenv("TELEGRAM_ACTION_PIN", "").strip()
        )
        if self._action_pin and not (
            len(self._action_pin) == 4
            and self._action_pin.isascii()
            and self._action_pin.isdigit()
        ):
            LOGGER.error("TELEGRAM_ACTION_PIN must contain exactly four ASCII digits.")
            self._action_pin = ""
        self._clock = time.monotonic
        self.session_expiry = 0.0
        self._pin_buffer = ""
        self._pin_chat_id: Optional[int] = None
        self._pin_message_id: Optional[int] = None
        self._failed_pin_attempts = 0
        self._pin_locked_until = 0.0
        self.application: Application = ApplicationBuilder().token(token).build()
        owner_filter = (
            filters.User(user_id=owner_chat_id)
            & filters.Chat(chat_id=owner_chat_id)
            & filters.ChatType.PRIVATE
        )
        self.application.add_handler(
            CommandHandler("start", self._handle_start,
                           filters=owner_filter, has_args=False)
        )
        self.application.add_handler(
            CallbackQueryHandler(
                self._handle_menu_callback,
                pattern=(
                    r"^(?:menu:(?:root|views|actions|status|orders|setgrid|"
                    r"setstop|stop|lock|back)|pin:(?:[0-9]|clear|cancel))$"
                ),
            )
        )
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND & owner_filter
                           & _PendingMenuInputFilter(self),
                           self._handle_menu_input)
        )
        self.application.add_handler(
            CommandHandler("stop", self._handle_stop, filters=owner_filter, has_args=False)
        )
        if grid_bot is not None:
            self.application.add_handler(
                CommandHandler("status", self._handle_status,
                               filters=owner_filter, has_args=False)
            )
            self.application.add_handler(
                CommandHandler("setgrid", self._handle_setgrid, filters=owner_filter)
            )
            self.application.add_handler(
                CommandHandler("setstop", self._handle_setstop, filters=owner_filter)
            )
            self.application.add_handler(
                CommandHandler("orders", self._handle_orders,
                               filters=owner_filter, has_args=False)
            )
        # Keep this last in group 0: PTB runs only the first matching handler
        # in a group, so recognized commands and active inputs stay intact.
        self.application.add_handler(
            MessageHandler(filters.ALL, self._handle_unexpected_message), group=0
        )
        self._initialized = False

    @staticmethod
    def _is_owner(update: Update, owner_chat_id: int) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        return bool(
            user is not None
            and chat is not None
            and user.id == owner_chat_id
            and chat.id == owner_chat_id
            and chat.type == "private"
        )

    @staticmethod
    def _main_menu_view() -> Tuple[str, InlineKeyboardMarkup]:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👁️ View Analytics", callback_data="menu:views")],
            [InlineKeyboardButton("🔐 Execute Actions", callback_data="menu:actions")],
        ])
        text = (
            "🤖 <b>Welcome to BTC/USDT Grid Master</b>\n\n"
            "<i>Your automated trading engine is online.</i>\n\n"
            "⚙️ <b>Current Mode:</b> Spot Testnet\n"
            "🛡️ <b>Security:</b> Owner Access Only\n\n"
            "👇 Please select a section from the menu below:"
        )
        return text, keyboard

    @staticmethod
    def _views_menu_view() -> Tuple[str, InlineKeyboardMarkup]:
        return (
            "👁️ <b>VIEW ANALYTICS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Choose the information you want to see.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Bot Status", callback_data="menu:status"),
                 InlineKeyboardButton("📋 Open Orders", callback_data="menu:orders")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu:root")],
            ]),
        )

    def _actions_menu_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        remaining = max(0, int(self.session_expiry - self._clock()))
        return (
            "🔐 <b>EXECUTE ACTIONS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🕒 <b>Session:</b> {remaining} seconds remaining\n\n"
            "Choose an action below.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Set Grid Bounds", callback_data="menu:setgrid"),
                 InlineKeyboardButton("🛡️ Set Stop-Loss", callback_data="menu:setstop")],
                [InlineKeyboardButton("🛑 Stop Bot", callback_data="menu:stop")],
                [InlineKeyboardButton("🔒 Lock Session Now", callback_data="menu:lock")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu:root")],
            ]),
        )

    @staticmethod
    def _back_keyboard(target: str = "menu:root") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back", callback_data=target)
        ]])

    @staticmethod
    def _pin_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(str(digit), callback_data=f"pin:{digit}")
             for digit in (1, 2, 3)],
            [InlineKeyboardButton(str(digit), callback_data=f"pin:{digit}")
             for digit in (4, 5, 6)],
            [InlineKeyboardButton(str(digit), callback_data=f"pin:{digit}")
             for digit in (7, 8, 9)],
            [InlineKeyboardButton("Clear", callback_data="pin:clear"),
             InlineKeyboardButton("0", callback_data="pin:0"),
             InlineKeyboardButton("Cancel", callback_data="pin:cancel")],
        ])

    def _pin_prompt_view(self, error: Optional[str] = None) -> str:
        masked = "●" * len(self._pin_buffer) + "○" * (4 - len(self._pin_buffer))
        notice = f"\n\n⚠️ {escape(error)}" if error else ""
        if self._clock() < self._pin_locked_until:
            remaining = max(1, int(self._pin_locked_until - self._clock()))
            notice = f"\n\n🔒 Too many attempts. Try again in {remaining} seconds."
        return (
            "🔐 <b>ENTER ACTION PIN</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Use the keypad to enter your four-digit PIN.\n\n"
            f"<b>PIN:</b> {masked}{notice}"
        )

    def _actions_unlocked(self) -> bool:
        return bool(self._action_pin and self._clock() < self.session_expiry)

    def _reset_pin_entry(self) -> None:
        self._pin_buffer = ""
        self._pin_chat_id = None
        self._pin_message_id = None

    @staticmethod
    def _input_prompt(action: str, error: Optional[str] = None) -> str:
        if action == "grid":
            title = "⚙️ <b>SET GRID BOUNDS</b>"
            instruction = "👇 Send the new bounds as <code>lower upper</code>."
            example = "<i>Example: 72000 95000</i>"
            rule = "🛡️ Stop-loss must be below the new lower bound."
        else:
            title = "🛡️ <b>SET STOP-LOSS</b>"
            instruction = "👇 Send the new stop-loss price as a number."
            example = "<i>Example: 64000</i>"
            rule = "⚠️ The price must be below the current lower grid bound."
        warning = f"\n\n❌ <b>Update rejected:</b> {escape(error)}" if error else ""
        return (
            f"{title}\n━━━━━━━━━━━━━━━━━━\n"
            f"{instruction}\n{example}\n\n{rule}{warning}"
        )

    async def _edit_menu_message(self, query: Any, text: str,
                                 keyboard: InlineKeyboardMarkup,
                                 parse_mode: Optional[str] = None) -> None:
        try:
            await query.edit_message_text(
                text=text, reply_markup=keyboard, parse_mode=parse_mode,
            )
        except BadRequest as error:
            if "message is not modified" not in str(error).lower():
                raise

    async def _handle_start(self, update: Update,
                            _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update, self.owner_chat_id):
            return
        self._pending_menu_input = None
        self._reset_pin_entry()
        text, keyboard = self._main_menu_view()
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    async def _show_pin_challenge(self, update: Update) -> None:
        query = update.callback_query
        self._pending_menu_input = None
        self._reset_pin_entry()
        if not self._action_pin:
            await self._edit_menu_message(
                query,
                "🔒 <b>ACTIONS UNAVAILABLE</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "The action PIN is not configured on the server.",
                self._back_keyboard("menu:root"), ParseMode.HTML,
            )
            return
        await self._edit_menu_message(
            query, self._pin_prompt_view(), self._pin_keyboard(), ParseMode.HTML,
        )
        self._pin_chat_id = update.effective_chat.id
        self._pin_message_id = query.message.message_id

    async def _handle_pin_callback(self, update: Update) -> dict[str, Any]:
        query = update.callback_query
        if (query.message is None or self._pin_chat_id != update.effective_chat.id
                or self._pin_message_id != query.message.message_id):
            return {"text": "Open Execute Actions again.", "show_alert": True}
        action = query.data
        if action == "pin:cancel":
            self._reset_pin_entry()
            text, keyboard = self._main_menu_view()
            await self._edit_menu_message(query, text, keyboard, ParseMode.HTML)
            return {}
        if self._clock() < self._pin_locked_until:
            await self._edit_menu_message(
                query, self._pin_prompt_view(), self._pin_keyboard(), ParseMode.HTML,
            )
            return {"text": "Try again after the lockout.", "show_alert": True}
        if action == "pin:clear":
            self._pin_buffer = ""
        else:
            self._pin_buffer += action.removeprefix("pin:")
            if len(self._pin_buffer) == 4:
                if hmac.compare_digest(self._pin_buffer, self._action_pin):
                    self.session_expiry = self._clock() + ACTION_SESSION_SECONDS
                    self._failed_pin_attempts = 0
                    self._pin_locked_until = 0.0
                    self._reset_pin_entry()
                    text, keyboard = self._actions_menu_view()
                    await self._edit_menu_message(query, text, keyboard, ParseMode.HTML)
                    return {}
                self._pin_buffer = ""
                self._failed_pin_attempts += 1
                if self._failed_pin_attempts >= 3:
                    self._pin_locked_until = self._clock() + PIN_LOCKOUT_SECONDS
                    self._failed_pin_attempts = 0
                    error = None
                else:
                    error = "Incorrect PIN. Try again."
                await self._edit_menu_message(
                    query, self._pin_prompt_view(error),
                    self._pin_keyboard(), ParseMode.HTML,
                )
                return {}
        await self._edit_menu_message(
            query, self._pin_prompt_view(), self._pin_keyboard(), ParseMode.HTML,
        )
        return {}

    async def _handle_menu_callback(self, update: Update,
                                    _context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        answer_options = {}
        try:
            if not self._is_owner(update, self.owner_chat_id):
                answer_options = {"text": "Not authorized.", "show_alert": True}
                return
            action = query.data
            if action.startswith("pin:"):
                answer_options = await self._handle_pin_callback(update)
                return
            if action in ("menu:root", "menu:back"):
                self._pending_menu_input = None
                self._reset_pin_entry()
                text, keyboard = self._main_menu_view()
                await self._edit_menu_message(query, text, keyboard, ParseMode.HTML)
                return
            if action == "menu:views":
                self._pending_menu_input = None
                self._reset_pin_entry()
                text, keyboard = self._views_menu_view()
                await self._edit_menu_message(query, text, keyboard, ParseMode.HTML)
                return
            if action == "menu:lock":
                self.session_expiry = 0.0
                self._pending_menu_input = None
                self._reset_pin_entry()
                text, keyboard = self._main_menu_view()
                await self._edit_menu_message(
                    query, "🔒 <b>Session locked.</b>\n\n" + text,
                    keyboard, ParseMode.HTML,
                )
                return
            if action == "menu:actions":
                self._pending_menu_input = None
                if not self._actions_unlocked():
                    if query.message is None:
                        answer_options = {"text": "Menu unavailable.", "show_alert": True}
                        return
                    await self._show_pin_challenge(update)
                    return
                self._reset_pin_entry()
                text, keyboard = self._actions_menu_view()
                await self._edit_menu_message(query, text, keyboard, ParseMode.HTML)
                return
            if action in ("menu:setgrid", "menu:setstop", "menu:stop"):
                if not self._actions_unlocked():
                    if query.message is None:
                        answer_options = {"text": "Menu unavailable.", "show_alert": True}
                        return
                    await self._show_pin_challenge(update)
                    return
                self._reset_pin_entry()
            if action in ("menu:setgrid", "menu:setstop"):
                if self.grid_bot is None or query.message is None:
                    answer_options = {"text": "Menu unavailable.", "show_alert": True}
                    return
                input_action = "grid" if action == "menu:setgrid" else "stop"
                await self._edit_menu_message(
                    query, self._input_prompt(input_action),
                    self._back_keyboard("menu:actions"), ParseMode.HTML,
                )
                self._pending_menu_input = PendingMenuInput(
                    input_action, update.effective_chat.id, query.message.message_id,
                )
                return
            self._pending_menu_input = None
            if action == "menu:status":
                text = await self._status_text()
            elif action == "menu:orders":
                messages = await self._open_order_messages()
                text = messages[0]
                if len(messages) > 1:
                    text += "\n\n<i>More orders: send /orders to see the full list.</i>"
            elif action == "menu:stop":
                text = await self._stop_text()
            else:
                return
            back_target = (
                "menu:views" if action in ("menu:status", "menu:orders")
                else "menu:actions"
            )
            await self._edit_menu_message(
                query, text, self._back_keyboard(back_target), ParseMode.HTML,
            )
        finally:
            await query.answer(**answer_options)

    async def _edit_pending_menu(self, context: ContextTypes.DEFAULT_TYPE,
                                 pending: PendingMenuInput, text: str,
                                 keyboard: Optional[InlineKeyboardMarkup] = None) -> None:
        try:
            await context.bot.edit_message_text(
                chat_id=pending.chat_id,
                message_id=pending.message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard or self._back_keyboard("menu:actions"),
            )
        except BadRequest as error:
            if "message is not modified" not in str(error).lower():
                LOGGER.warning("Could not edit input prompt: %s", type(error).__name__)
                self._pending_menu_input = None
        except Exception as error:
            LOGGER.warning("Could not edit input prompt: %s", type(error).__name__)

    async def _handle_menu_input(self, update: Update,
                                 context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update, self.owner_chat_id):
            return
        pending = self._pending_menu_input
        message = update.effective_message
        if pending is None:
            return
        if (pending is None or self.grid_bot is None or message is None
                or update.effective_chat.id != pending.chat_id):
            return
        try:
            deleted = await message.delete()
            if deleted is False:
                raise RuntimeError("Telegram did not delete the input message.")
        except Exception as error:
            LOGGER.warning("Could not delete menu input: %s", type(error).__name__)
            await self._edit_pending_menu(
                context, pending,
                self._input_prompt(
                    pending.action,
                    "Could not delete your message. Delete it manually, then try again.",
                ),
            )
            return

        if not self._actions_unlocked():
            self._pending_menu_input = None
            self._reset_pin_entry()
            if self._action_pin:
                self._pin_chat_id = pending.chat_id
                self._pin_message_id = pending.message_id
                await self._edit_pending_menu(
                    context, pending, self._pin_prompt_view(), self._pin_keyboard(),
                )
            else:
                await self._edit_pending_menu(
                    context, pending,
                    "🔒 <b>ACTIONS UNAVAILABLE</b>\n"
                    "The action PIN is not configured on the server.",
                    self._back_keyboard("menu:root"),
                )
            return

        values = (message.text or "").split()
        if pending.action == "grid":
            if len(values) != 2:
                error_text = "Enter exactly two numbers: lower upper."
            else:
                try:
                    await asyncio.to_thread(
                        self.grid_bot.request_grid_reset, values[0], values[1],
                    )
                except (ValueError, RuntimeError) as error:
                    error_text = str(error)
                except Exception as error:
                    LOGGER.warning("Grid input failed: %s", type(error).__name__)
                    error_text = "Grid update failed. Please try again."
                else:
                    self._pending_menu_input = None
                    await self._edit_pending_menu(
                        context, pending,
                        "✅ <b>GRID BOUNDS UPDATED</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"• <b>Lower:</b> {escape(values[0])} USDT\n"
                        f"• <b>Upper:</b> {escape(values[1])} USDT\n\n"
                        "The bot will cancel old orders and rebuild the grid.",
                    )
                    return
        else:
            if len(values) != 1:
                error_text = "Enter exactly one numeric stop-loss price."
            else:
                try:
                    price = await asyncio.to_thread(self.grid_bot.set_stop_loss, values[0])
                except (ValueError, RuntimeError) as error:
                    error_text = str(error)
                except Exception as error:
                    LOGGER.warning("Stop-loss input failed: %s", type(error).__name__)
                    error_text = "Stop-loss update failed. Please try again."
                else:
                    self._pending_menu_input = None
                    await self._edit_pending_menu(
                        context, pending,
                        "✅ <b>STOP-LOSS UPDATED</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"• <b>New price:</b> {escape(format(price, 'f'))} USDT\n\n"
                        "Existing grid orders were left in place.",
                    )
                    return
        await self._edit_pending_menu(
            context, pending, self._input_prompt(pending.action, error_text),
        )

    async def _handle_unexpected_message(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Silently remove unmatched owner messages from the private chat."""
        if not self._is_owner(update, self.owner_chat_id):
            return
        message = update.effective_message
        if message is None:
            return
        try:
            await message.delete()
        except Exception as error:
            LOGGER.warning("Could not delete unexpected message: %s",
                           type(error).__name__)

    async def _handle_stop(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update, self.owner_chat_id):
            return
        self._pending_menu_input = None
        if not self._actions_unlocked():
            await update.effective_message.reply_text(
                "🔒 Unlock Execute Actions from /start before using /stop."
            )
            return
        await update.effective_message.reply_text(await self._stop_text())

    async def _stop_text(self) -> str:
        self.stop_controller.stop_requested.set()
        try:
            result = await asyncio.to_thread(self.stop_controller.request_stop)
        except Exception:
            return "Trading paused. Order cancellation could not be verified; inspect local state."
        return (
            "Trading stopped. "
            f"Canceled: {result.canceled}; already filled: {result.filled}; "
            f"unresolved: {result.unresolved}."
        )

    async def _handle_status(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update, self.owner_chat_id) or self.grid_bot is None:
            return
        await update.effective_message.reply_text(
            await self._status_text(), parse_mode=ParseMode.HTML,
        )

    async def _status_text(self) -> str:
        if self.grid_bot is None:
            return "Bot status unavailable."
        try:
            price, lower, upper, levels, stop_loss = await asyncio.wait_for(
                asyncio.to_thread(self.grid_bot.grid_status),
                timeout=STATUS_API_TIMEOUT_SECONDS,
            )
        except Exception as error:
            LOGGER.warning("Status price request failed: %s", type(error).__name__)
            price = None
            try:
                lower, upper, levels, stop_loss = self.grid_bot.grid_configuration()
            except Exception:
                return "⚠️ <b>Grid settings unavailable.</b> Check bot logs."
        try:
            buy_count, sell_count, closest_buy, closest_sell = await asyncio.wait_for(
                asyncio.to_thread(self.grid_bot.open_order_summary, price),
                timeout=STATUS_API_TIMEOUT_SECONDS,
            )
            buy_price = (f"{closest_buy} USDT" if closest_buy is not None else
                         "None" if buy_count == 0 else "Unavailable")
            sell_price = (f"{closest_sell} USDT" if closest_sell is not None else
                          "None" if sell_count == 0 else "Unavailable")
        except Exception as error:
            LOGGER.warning("Status open-order request failed: %s", type(error).__name__)
            buy_count = sell_count = "Unavailable"
            buy_price = sell_price = "Unavailable"
        try:
            balances = await asyncio.wait_for(
                asyncio.to_thread(self.grid_bot.wallet_balances),
                timeout=STATUS_API_TIMEOUT_SECONDS,
            )
        except Exception as error:
            LOGGER.warning("Status balance request failed: %s", type(error).__name__)
            balances = {}
        usdt = balances.get("USDT") or {}
        btc = balances.get("BTC") or {}
        usdt_free = _format_balance_amount(usdt.get("free"), 2)
        usdt_used = _format_balance_amount(usdt.get("used"), 2)
        btc_free = _format_balance_amount(btc.get("free"), 8)
        btc_used = _format_balance_amount(btc.get("used"), 8)
        current_price = f"{price} USDT" if price is not None else "Unavailable"
        return (
            "📊 <b>BOT STATUS &amp; ANALYTICS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💰 <b>Market:</b> BTC/USDT\n"
            f"📈 <b>Current Price:</b> {escape(current_price)}\n\n"
            "💼 <b>Wallet Balance</b>\n"
            f"• <b>Free USDT:</b> {usdt_free} | <b>Locked:</b> {usdt_used}\n"
            f"• <b>Free BTC:</b> {btc_free} | <b>Locked:</b> {btc_used}\n\n"
            "⚙️ <b>Grid Configuration</b>\n"
            f"• <b>Bounds:</b> {escape(str(lower))} - {escape(str(upper))} USDT\n"
            f"• <b>Levels:</b> {escape(str(levels))}\n"
            f"• <b>Stop-Loss:</b> {escape(str(stop_loss))} USDT\n\n"
            "📋 <b>Live Order Summary</b>\n"
            f"• 🟢 <b>BUY Limits:</b> {escape(str(buy_count))} "
            f"(Closest: {escape(buy_price)})\n"
            f"• 🔴 <b>SELL Limits:</b> {escape(str(sell_count))} "
            f"(Closest: {escape(sell_price)})\n"
            "━━━━━━━━━━━━━━━━━━"
        )

    async def _handle_orders(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update, self.owner_chat_id) or self.grid_bot is None:
            return
        for message in await self._open_order_messages():
            await update.effective_message.reply_text(
                message, parse_mode=ParseMode.HTML,
            )

    async def _open_order_messages(self) -> list[str]:
        if self.grid_bot is None:
            return ["Open orders unavailable."]
        try:
            base, quote, buys, sells = await asyncio.wait_for(
                asyncio.to_thread(self.grid_bot.list_open_orders),
                timeout=STATUS_API_TIMEOUT_SECONDS,
            )
        except Exception as error:
            LOGGER.warning("Orders request failed: %s", type(error).__name__)
            return [
                "Could not fetch open orders (exchange request failed or timed out). "
                "Please try again."
            ]
        return _format_open_order_messages(base, quote, buys, sells)

    async def _handle_setgrid(self, update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update, self.owner_chat_id) or self.grid_bot is None:
            return
        self._pending_menu_input = None
        if not self._actions_unlocked():
            await update.effective_message.reply_text(
                "🔒 Unlock Execute Actions from /start before using /setgrid."
            )
            return
        args = context.args or []
        if len(args) != 2:
            await update.effective_message.reply_text("Usage: /setgrid <lower> <upper>")
            return
        try:
            await asyncio.to_thread(self.grid_bot.request_grid_reset, args[0], args[1])
        except (ValueError, RuntimeError) as error:
            await update.effective_message.reply_text(f"Grid update rejected: {error}")
            return
        except Exception:
            await update.effective_message.reply_text("Grid update failed. Check bot logs.")
            return
        await update.effective_message.reply_text(
            "✅ Grid bounds updated. Canceling old orders and rebuilding grid..."
        )

    async def _handle_setstop(self, update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update, self.owner_chat_id) or self.grid_bot is None:
            return
        self._pending_menu_input = None
        if not self._actions_unlocked():
            await update.effective_message.reply_text(
                "🔒 Unlock Execute Actions from /start before using /setstop."
            )
            return
        args = context.args or []
        if len(args) != 1:
            await update.effective_message.reply_text("Usage: /setstop <price>")
            return
        try:
            price = await asyncio.to_thread(self.grid_bot.set_stop_loss, args[0])
        except ValueError as error:
            message = str(error)
            if not message.startswith("❌ Rejected:"):
                message = f"❌ Rejected: {message}"
            await update.effective_message.reply_text(message)
            return
        except RuntimeError as error:
            await update.effective_message.reply_text(f"❌ Rejected: {error}")
            return
        except Exception:
            await update.effective_message.reply_text("Stop-loss update failed. Check bot logs.")
            return
        await update.effective_message.reply_text(
            f"✅ Stop-loss successfully updated to: ${format(price, 'f')}"
        )

    async def start(self) -> None:
        await self.application.initialize()
        self._initialized = True
        if self.application.updater is None:
            raise RuntimeError("Telegram polling is unavailable.")
        await self.application.updater.start_polling(drop_pending_updates=True)
        await self.application.start()

    async def stop(self) -> None:
        if not self._initialized:
            return
        if self.application.updater is not None and self.application.updater.running:
            await self.application.updater.stop()
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()
        self._initialized = False

    async def _send(self, message: str) -> None:
        await self.application.bot.send_message(chat_id=self.owner_chat_id, text=message)

    async def notify_startup(self, symbol: str) -> None:
        await self._send(f"Grid bot started on Binance Spot Testnet: {symbol}.")

    async def notify_order_filled(self, order_id: str, side: str, amount: str, price: str) -> None:
        await self._send(f"Order filled: {side} {amount} at {price} (ID {order_id}).")

    async def notify_stop_loss(self, symbol: str, price: str) -> None:
        await self._send(f"Stop-loss triggered for {symbol} at {price}.")

    async def notify_critical_error(self, error: Exception) -> None:
        # Do not send raw exception text: exchange errors may contain request details.
        await self._send(f"Critical bot error: {type(error).__name__}.")

    async def notify_grid_reset(self) -> None:
        await self._send("✅ New grid successfully placed and active.")


async def _send_test_notification() -> None:
    token, owner_chat_id = load_telegram_credentials()
    async with Bot(token=token) as bot:
        await bot.send_message(chat_id=owner_chat_id, text="Grid bot Phase 3 notification test.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 3 Telegram notification check")
    parser.add_argument("--test-notification", action="store_true")
    arguments = parser.parse_args()
    if not arguments.test_notification:
        parser.error("Use --test-notification to send one message to the configured owner.")
    try:
        asyncio.run(_send_test_notification())
    except ValueError as error:
        parser.error(str(error))
