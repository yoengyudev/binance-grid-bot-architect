import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram_bot import TelegramAlertError, TelegramNotifier, send_telegram_alert


class TelegramNotifierTests(unittest.TestCase):
    def test_post_alert_to_configured_chat(self) -> None:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(
            status_code=200, json=lambda: {"ok": True}
        )
        with patch("telegram_bot.httpx.AsyncClient", return_value=client):
            asyncio.run(send_telegram_alert("Grid active", token="secret-token", chat_id=42))

        client.post.assert_awaited_once_with(
            "https://api.telegram.org/botsecret-token/sendMessage",
            json={"chat_id": 42, "text": "Grid active"},
        )

    def test_http_error_does_not_expose_token(self) -> None:
        import httpx

        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.side_effect = httpx.ConnectError(
            "https://api.telegram.org/botsecret-token/sendMessage"
        )
        with patch("telegram_bot.httpx.AsyncClient", return_value=client):
            with self.assertRaises(TelegramAlertError) as caught:
                asyncio.run(send_telegram_alert("Grid active", token="secret-token", chat_id=42))

        self.assertNotIn("secret-token", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_bot_api_failure_is_reported(self) -> None:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(
            status_code=200, json=lambda: {"ok": False}
        )
        with patch("telegram_bot.httpx.AsyncClient", return_value=client):
            with self.assertRaises(TelegramAlertError):
                asyncio.run(send_telegram_alert("Grid active", token="secret-token", chat_id=42))

    def test_safety_and_breakout_alerts_use_one_way_sender(self) -> None:
        notifier = TelegramNotifier("secret-token", 42)
        with patch("telegram_bot.send_telegram_alert", new_callable=AsyncMock) as send:
            asyncio.run(notifier.notify_safety_pause())
            asyncio.run(notifier.notify_breakout_shift())

        self.assertEqual(send.await_count, 2)
        self.assertIn("SAFETY PAUSE ACTIVE", send.await_args_list[0].args[0])
        self.assertIn("BREAKOUT CONFIRMED", send.await_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
