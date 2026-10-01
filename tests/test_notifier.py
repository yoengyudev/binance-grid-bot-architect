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

    def test_sizing_pause_alert_explains_manual_reanchor(self) -> None:
        notifier = TelegramNotifier("secret-token", 42)
        with patch("telegram_bot.send_telegram_alert", new_callable=AsyncMock) as send:
            asyncio.run(notifier.notify_sizing_pause("Rounded order below 7 USDT."))
        message = send.await_args.args[0]
        self.assertIn("GRID SIZING PAUSE", message)
        self.assertIn("Rounded order below 7 USDT", message)
        self.assertIn("Re-anchor", message)

    def test_hard_stop_alerts_report_verified_or_unresolved_outcome(self) -> None:
        notifier = TelegramNotifier("secret-token", 42)
        with patch("telegram_bot.send_telegram_alert", new_callable=AsyncMock) as send:
            asyncio.run(notifier.notify_hard_stop(True))
            asyncio.run(notifier.notify_hard_stop(False))
        self.assertIn("LIQUIDATED", send.await_args_list[0].args[0])
        self.assertIn("HALTED", send.await_args_list[1].args[0])

    def test_saved_grid_and_recenter_alerts_are_distinct(self) -> None:
        notifier = TelegramNotifier("secret-token", 42)
        with patch("telegram_bot.send_telegram_alert", new_callable=AsyncMock) as send:
            asyncio.run(notifier.notify_grid_reset(
                "saved_pending", "78101.71", "89638.29"
            ))
            asyncio.run(notifier.notify_grid_reset(
                "manual_recenter", "79129.80", "88270.20"
            ))
        first = send.await_args_list[0].args[0]
        second = send.await_args_list[1].args[0]
        self.assertIn("Saved pending grid", first)
        self.assertIn("Grid re-anchored", second)
        self.assertIn("78101.71", first)
        self.assertIn("88270.20", second)


if __name__ == "__main__":
    unittest.main()
