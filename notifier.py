"""Best-effort Telegram alerts, isolated from trading and API worker threads."""

import asyncio
import logging
import os
import re
from typing import Optional

import httpx

from telegram_bot import TelegramNotifier as LegacyTelegramNotifier

LOGGER = logging.getLogger(__name__)
# httpx INFO request logs contain the token-bearing Telegram URL.
logging.getLogger("httpx").setLevel(logging.WARNING)
_loop: Optional[asyncio.AbstractEventLoop] = None
_tasks: set[asyncio.Task] = set()
MAX_PENDING_ALERTS = 256


def markdown_text(value: object) -> str:
    """Escape dynamic values for Telegram MarkdownV2."""
    return re.sub(r"([_\*\[\]\(\)~`>#+\-=|{}.!\\])", r"\\\1", str(value))


def _chat_id() -> str:
    return (os.getenv("TELEGRAM_CHAT_ID", "").strip()
            or os.getenv("TELEGRAM_OWNER_CHAT_ID", "").strip())


def notifications_enabled() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
                and _chat_id())


async def send_telegram_alert(message: str) -> None:
    """Send MarkdownV2; absent configuration and delivery failures are harmless."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = _chat_id()
    if not token or not chat_id or not message.strip():
        return
    if len(message) > 4096:
        LOGGER.warning("Telegram alert exceeds the message limit; dropped.")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message,
                      "parse_mode": "MarkdownV2", "disable_notification": False},
            )
            payload = response.json()
            if (response.status_code != 200 or not isinstance(payload, dict)
                    or payload.get("ok") is not True):
                LOGGER.warning("Telegram rejected an alert (HTTP %s).", response.status_code)
    except Exception:
        # Never log exception text: httpx URLs contain the bot token.
        LOGGER.warning("Telegram alert delivery failed.")


def bind_notification_loop() -> None:
    """Call once inside the service's running asyncio loop."""
    global _loop
    _loop = asyncio.get_running_loop()
    if not notifications_enabled():
        LOGGER.info("Telegram notifications disabled: token or chat ID is missing.")


def schedule_telegram_alert(message: str) -> bool:
    """Schedule from the event loop or a trading/API worker; never wait on HTTP."""
    loop = _loop
    if not notifications_enabled() or loop is None or loop.is_closed():
        return False

    def create() -> None:
        if len(_tasks) >= MAX_PENDING_ALERTS:
            LOGGER.warning("Telegram alert backlog full; dropped an alert.")
            return
        task = loop.create_task(send_telegram_alert(message), name="telegram-alert")
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)

    try:
        loop.call_soon_threadsafe(create)
    except RuntimeError:
        return False  # The loop closed during shutdown.
    return True


async def close_notifications() -> None:
    """Allow a bounded delivery window after trading has stopped."""
    global _loop
    _loop = None
    await asyncio.sleep(0)  # Materialize callbacks already submitted by workers.
    pending = set(_tasks)
    if pending:
        _, unfinished = await asyncio.wait(pending, timeout=2.0)
        for task in unfinished:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


class TelegramNotifier(LegacyTelegramNotifier):
    """Keep existing safety/grid alerts, but schedule all delivery in background."""

    def __init__(self) -> None:
        # Credentials are optional and resolved by send_telegram_alert.
        pass

    async def _send(self, message: str) -> None:
        schedule_telegram_alert(markdown_text(message))

    async def notify_startup(self, symbol: str) -> None:
        schedule_telegram_alert(f"*Bot START*\nPair: {markdown_text(symbol)}")

    async def notify_critical_error(self, error: Exception) -> None:
        schedule_telegram_alert(
            f"🚨 *ENGINE FAULT*\nType: {markdown_text(type(error).__name__)}"
        )
        error._telegram_alert_scheduled = True

    async def notify_hard_stop(self, liquidated: bool) -> None:
        outcome = "LIQUIDATED — inventory verified flat" if liquidated else "HALTED — review liquidation manually"
        schedule_telegram_alert(f"🚨 *HARD STOP*\n{markdown_text(outcome)}")
