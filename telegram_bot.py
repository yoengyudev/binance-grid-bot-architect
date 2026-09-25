"""Owner-only Telegram alerts and grid controls."""

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
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
    ContextTypes, filters,
)

from database import GridDatabase


BASE_DIR = Path(__file__).resolve().parent
STATUS_API_TIMEOUT_SECONDS = 12
TELEGRAM_MESSAGE_CHUNK_LENGTH = 4000
LOGGER = logging.getLogger(__name__)


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
                 grid_bot: Optional[Any] = None) -> None:
        if not token or owner_chat_id <= 0:
            raise ValueError("A bot token and positive owner private chat ID are required.")
        self.owner_chat_id = owner_chat_id
        self.stop_controller = stop_controller
        self.grid_bot = grid_bot
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
                pattern=r"^menu:(?:status|orders|setgrid|setstop|stop|back)$",
            )
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
            [InlineKeyboardButton("📊 Bot Status", callback_data="menu:status"),
             InlineKeyboardButton("📋 Open Orders", callback_data="menu:orders")],
            [InlineKeyboardButton("⚙️ Set Grid Bounds", callback_data="menu:setgrid"),
             InlineKeyboardButton("🛡️ Set Stop-Loss", callback_data="menu:setstop")],
            [InlineKeyboardButton("🛑 Stop Bot", callback_data="menu:stop")],
        ])
        text = (
            "🤖 <b>Welcome to BTC/USDT Grid Master</b>\n\n"
            "<i>Your automated trading engine is online.</i>\n\n"
            "⚙️ <b>Current Mode:</b> Spot Testnet\n"
            "🛡️ <b>Security:</b> Owner Access Only\n\n"
            "👇 Please select an operation from the menu below:"
        )
        return text, keyboard

    @staticmethod
    def _back_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu:back")
        ]])

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
        text, keyboard = self._main_menu_view()
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

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
            if action == "menu:back":
                text, keyboard = self._main_menu_view()
                await self._edit_menu_message(query, text, keyboard, ParseMode.HTML)
                return
            if action == "menu:status":
                text = await self._status_text()
            elif action == "menu:orders":
                messages = await self._open_order_messages()
                text = messages[0]
                if len(messages) > 1:
                    text += "\n\n<i>More orders: send /orders to see the full list.</i>"
            elif action == "menu:setgrid":
                text = (
                    "⚙️ <b>SET GRID BOUNDS</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "• <b>Command:</b> <code>/setgrid &lt;lower&gt; &lt;upper&gt;</code>\n"
                    "• <b>Example:</b> <code>/setgrid 72000 95000</code>\n\n"
                    "🛡️ <i>Stop-loss must be below the new lower bound.</i>"
                )
            elif action == "menu:setstop":
                text = (
                    "🛡️ <b>SET STOP-LOSS</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "• <b>Command:</b> <code>/setstop &lt;price&gt;</code>\n\n"
                    "⚠️ <i>The price must be below the current lower grid bound.</i>"
                )
            elif action == "menu:stop":
                text = await self._stop_text()
            else:
                return
            await self._edit_menu_message(
                query, text, self._back_keyboard(), ParseMode.HTML,
            )
        finally:
            await query.answer(**answer_options)

    async def _handle_stop(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update, self.owner_chat_id):
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
        current_price = f"{price} USDT" if price is not None else "Unavailable"
        return (
            "📊 <b>BOT STATUS &amp; ANALYTICS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💰 <b>Market:</b> BTC/USDT\n"
            f"📈 <b>Current Price:</b> {escape(current_price)}\n\n"
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
