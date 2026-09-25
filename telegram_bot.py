"""Owner-only Telegram alerts and grid controls."""

import argparse
import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, RLock
from typing import Any, Optional, Tuple

from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, filters

from database import GridDatabase


BASE_DIR = Path(__file__).resolve().parent


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

    async def _handle_stop(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update, self.owner_chat_id):
            return
        self.stop_controller.stop_requested.set()
        try:
            result = await asyncio.to_thread(self.stop_controller.request_stop)
        except Exception:
            await update.effective_message.reply_text(
                "Trading paused. Order cancellation could not be verified; inspect local state."
            )
            return
        await update.effective_message.reply_text(
            "Trading stopped. "
            f"Canceled: {result.canceled}; already filled: {result.filled}; "
            f"unresolved: {result.unresolved}."
        )

    async def _handle_status(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update, self.owner_chat_id) or self.grid_bot is None:
            return
        try:
            price, lower, upper, levels, stop_loss = await asyncio.to_thread(
                self.grid_bot.grid_status
            )
        except Exception:
            await update.effective_message.reply_text("Could not fetch grid status. Check bot logs.")
            return
        await update.effective_message.reply_text(
            f"BTC/USDT current price: {price} USDT\n"
            f"Active bounds: {lower}–{upper} USDT\n"
            f"Total grid levels: {levels}\n"
            f"Stop-loss: {stop_loss} USDT"
        )

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
