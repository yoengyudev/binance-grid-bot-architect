"""One-way Telegram alerts; no message polling or command handlers."""

import argparse
import asyncio
import os
from pathlib import Path
from typing import Optional, Tuple

import httpx
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
TELEGRAM_MESSAGE_LIMIT = 4096


class TelegramAlertError(RuntimeError):
    """A sanitized delivery error that never contains the bot token."""


def load_telegram_credentials() -> Tuple[str, int]:
    """Read the outbound bot token and destination chat from .env."""
    load_dotenv(dotenv_path=BASE_DIR / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_text = os.getenv("TELEGRAM_OWNER_CHAT_ID", "").strip()
    if not token:
        raise ValueError("Set TELEGRAM_BOT_TOKEN in .env.")
    try:
        chat_id = int(chat_text)
    except ValueError as error:
        raise ValueError("Set TELEGRAM_OWNER_CHAT_ID to your private chat ID.") from error
    if chat_id <= 0:
        raise ValueError("TELEGRAM_OWNER_CHAT_ID must be a positive private chat ID.")
    return token, chat_id


async def send_telegram_alert(
    message: str,
    *,
    token: Optional[str] = None,
    chat_id: Optional[int] = None,
) -> None:
    """Send one text alert through the Telegram Bot API over HTTPS."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Telegram alert must contain text.")
    if len(message) > TELEGRAM_MESSAGE_LIMIT:
        raise ValueError("Telegram alert exceeds the text limit.")
    if token is None or chat_id is None:
        token, chat_id = load_telegram_credentials()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
            )
        payload = response.json()
        if response.status_code != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
            raise TelegramAlertError("Telegram rejected the alert.")
    except (httpx.HTTPError, ValueError) as error:
        # httpx exceptions may include the token-bearing URL; never propagate it.
        raise TelegramAlertError("Telegram alert delivery failed.") from None


class TelegramNotifier:
    """Small collection of trading alerts sent to the configured owner."""

    def __init__(self, token: str, owner_chat_id: int) -> None:
        if not token or owner_chat_id <= 0:
            raise ValueError("A bot token and positive private chat ID are required.")
        self._token = token
        self._chat_id = owner_chat_id

    async def _send(self, message: str) -> None:
        await send_telegram_alert(message, token=self._token, chat_id=self._chat_id)

    async def notify_startup(self, symbol: str) -> None:
        await self._send(f"Grid bot started on Binance Spot Testnet: {symbol}.")

    async def notify_order_filled(self, order_id: str, side: str, amount: str, price: str) -> None:
        await self._send(f"Order filled: {side} {amount} at {price} (ID {order_id}).")

    async def notify_safety_pause(self) -> None:
        await self._send(
            "⚠️ SAFETY PAUSE ACTIVE: BUY cancellations are being verified; "
            "SELL limits remain open."
        )

    async def notify_sizing_pause(self, reason: str) -> None:
        await self._send(
            "GRID SIZING PAUSE: New orders are blocked and bot BUY limits are "
            f"being canceled. Existing SELL limits remain open. {reason} "
            "Re-anchor with adequate order sizes to resume."
        )

    async def notify_safety_recovery(self) -> None:
        await self._send(
            "✅ SAFETY PAUSE LIFTED: Market recovered. Remaining BUY levels will "
            "restore as price rises above each limit."
        )

    async def notify_safety_resume(self) -> None:
        await self._send(
            "✅ SAFETY PAUSE LIFTED: Market recovered. BUY orders automatically restored."
        )

    async def notify_breakout_shift(self) -> None:
        await self._send(
            "🚀 BREAKOUT CONFIRMED: Market held for 4 hours. "
            "Grid auto-shifted to new price floor."
        )

    async def notify_critical_error(self, error: Exception) -> None:
        # Exchange exception text can include sensitive request details.
        await self._send(f"Critical bot error: {type(error).__name__}.")

    async def notify_grid_reset(self, event: str = None,
                                lower: str = None, upper: str = None) -> None:
        if event == "saved_pending":
            message = "✅ Saved pending grid activated."
        elif event == "manual_recenter":
            message = "✅ Grid re-anchored and active."
        elif event == "engine_resume":
            message = "✅ Saved grid rebuilt and active."
        else:
            message = "✅ New grid successfully placed and active."
        if lower is not None and upper is not None:
            message += f" Bounds: {lower} to {upper} USDT."
        await self._send(message)

    async def notify_hard_stop(self, liquidated: bool) -> None:
        if liquidated:
            await self._send(
                "HARD STOP LIQUIDATED: All open BTC/USDT orders were canceled, "
                "tradable bot-tracked BTC was market-sold, and trading is locked "
                "until admin reset. Check the dashboard for any unsellable dust."
            )
        else:
            await self._send(
                "HARD STOP HALTED: Liquidation could not be verified. "
                "Trading is locked. Inspect orders and BTC balance manually."
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send one Telegram test alert")
    parser.add_argument("--test-notification", action="store_true")
    arguments = parser.parse_args()
    if not arguments.test_notification:
        parser.error("Use --test-notification to send one alert to the configured chat.")
    try:
        asyncio.run(send_telegram_alert("Grid bot notification test."))
    except (TelegramAlertError, ValueError) as error:
        parser.error(str(error))
