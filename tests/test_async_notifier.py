import asyncio
import os
import tempfile
from decimal import Decimal
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


import engine as grid_engine
import httpx
import notifier

CONFIG = {'TELEGRAM_BOT_TOKEN': 'secret-token', 'TELEGRAM_CHAT_ID': '-100123'}


class AsyncTelegramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await notifier.close_notifications()

    async def test_missing_configuration_never_opens_http_client(self):
        for env in ({}, {'TELEGRAM_BOT_TOKEN': 'secret-token'}, {'TELEGRAM_CHAT_ID': '42'}):
            with patch.dict(os.environ, env, clear=True), patch('notifier.httpx.AsyncClient') as client:
                await notifier.send_telegram_alert('*Test*')
                notifier.bind_notification_loop()
                self.assertFalse(notifier.schedule_telegram_alert('*Test*'))
                client.assert_not_called()

    async def test_markdown_message_and_group_chat_are_sent(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(status_code=200, json=lambda: {'ok': True})
        with patch.dict(os.environ, CONFIG, clear=True), patch('notifier.httpx.AsyncClient', return_value=client):
            await notifier.send_telegram_alert('*BUY FILLED*')
        client.post.assert_awaited_once_with('https://api.telegram.org/botsecret-token/sendMessage',
            json={'chat_id': '-100123', 'text': '*BUY FILLED*',
                  'parse_mode': 'MarkdownV2', 'disable_notification': False})

    async def test_existing_owner_chat_configuration_is_supported(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(status_code=200, json=lambda: {'ok': True})
        env = {'TELEGRAM_BOT_TOKEN': 'secret-token', 'TELEGRAM_OWNER_CHAT_ID': '42'}
        with patch.dict(os.environ, env, clear=True), patch('notifier.httpx.AsyncClient', return_value=client):
            self.assertTrue(notifier.notifications_enabled())
            await notifier.send_telegram_alert('*Test*')
        self.assertEqual(client.post.await_args.kwargs['json']['chat_id'], '42')

    async def test_network_failure_never_logs_token_or_raises(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.side_effect = httpx.ConnectError('https://api.telegram.org/botsecret-token/sendMessage')
        with patch.dict(os.environ, CONFIG, clear=True), patch('notifier.httpx.AsyncClient', return_value=client):
            with self.assertLogs('notifier', level='WARNING') as logs:
                await notifier.send_telegram_alert('*Test*')
        self.assertNotIn('secret-token', '\n'.join(logs.output))

    async def test_rejected_api_response_is_safe(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(status_code=200, json=lambda: {'ok': False})
        with patch.dict(os.environ, CONFIG, clear=True), patch('notifier.httpx.AsyncClient', return_value=client):
            with self.assertLogs('notifier', level='WARNING'):
                await notifier.send_telegram_alert('*Test*')

    async def test_legacy_hooks_and_worker_calls_do_not_wait_for_delivery(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_send(message):
            started.set()
            await release.wait()
        with patch.dict(os.environ, CONFIG, clear=True), patch('notifier.send_telegram_alert', side_effect=slow_send) as send:
            notifier.bind_notification_loop()
            service = notifier.TelegramNotifier()
            await asyncio.wait_for(service.notify_startup('BTC/USDT'), timeout=0.5)
            await asyncio.wait_for(started.wait(), timeout=0.5)
            self.assertTrue(notifier._tasks)
            self.assertTrue(await asyncio.wait_for(asyncio.to_thread(
                notifier.schedule_telegram_alert, '*Worker alert*'), timeout=0.5))
            await asyncio.sleep(0)
            release.set()
            await notifier.close_notifications()
            self.assertEqual(send.await_count, 2)
            self.assertFalse(notifier._tasks)

    async def test_dynamic_markdown_values_are_escaped(self):
        self.assertEqual(notifier.markdown_text('1.25 - BTC_[x]!'), r'1\.25 \- BTC\_\[x\]\!')

    async def test_pending_alerts_are_bounded(self):
        release = asyncio.Event()
        async def slow_send(message):
            await release.wait()
        with patch.dict(os.environ, CONFIG, clear=True), patch('notifier.send_telegram_alert', side_effect=slow_send) as send, patch('notifier.MAX_PENDING_ALERTS', 1):
            notifier.bind_notification_loop()
            notifier.schedule_telegram_alert('*First*')
            await asyncio.sleep(0)
            with self.assertLogs('notifier', level='WARNING'):
                notifier.schedule_telegram_alert('*Second*')
                await asyncio.sleep(0)
            release.set()
            await notifier.close_notifications()
            self.assertEqual(send.await_count, 1)


class EngineAlertIntegrationTests(unittest.TestCase):
    def make_bot(self, directory):
        from tests.fakes import make_bot
        return make_bot(Path(directory) / "grid_testnet.sqlite")

    def test_confirmed_fills_and_recorded_pnl_are_alerted_once(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(grid_engine, "schedule_telegram_alert", return_value=True) as send:
            bot, exchange = self.make_bot(directory)
            bot.run_cycle()
            bot.run_cycle()
            buy = bot.database.fetch_latest_orders_by_level()[1]
            exchange.fill(buy["client_order_id"])
            bot.run_cycle()
            sell = bot.database.fetch_latest_orders_by_level()[1]
            exchange.fill(sell["client_order_id"])
            bot.run_cycle()
            bot.run_cycle()
            messages = [call.args[0] for call in send.call_args_list]
            buy_messages = [m for m in messages if "*Order FILLED*" in m and notifier.markdown_text(buy["order_id"]) in m]
            sell_messages = [m for m in messages if "*Order FILLED*" in m and notifier.markdown_text(sell["order_id"]) in m]
            pnl_messages = [m for m in messages if "*SELL FILLED \u2014 Realized P&L*" in m]
            self.assertEqual(len(buy_messages), 1)
            self.assertEqual(len(sell_messages), 1)
            self.assertEqual(len(pnl_messages), 1)
            trade = bot.database.fetch_trade_history()[0]
            self.assertIn(notifier.markdown_text(trade["profit"]), pnl_messages[0])
            self.assertIn("Side: BUY", buy_messages[0])
            self.assertIn("Side: SELL", sell_messages[0])

    def test_liquidation_alert_precedes_market_sale(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(directory)
            bot.run_cycle()
            bot.run_cycle()
            events = []
            def schedule(message):
                events.append((message, exchange.market_sell_calls))
                return True
            with patch.object(grid_engine, "schedule_telegram_alert", side_effect=schedule):
                exchange.price = Decimal("69")
                bot.run_cycle()
            triggers = [(message, count) for message, count in events if "HARD STOP TRIGGERED" in message]
            self.assertEqual(len(triggers), 1)
            self.assertEqual(triggers[0][1], 0)
            self.assertEqual(exchange.market_sell_calls, 1)
            self.assertTrue(any("Realized P&L" in message for message, _ in events))


if __name__ == '__main__':
    unittest.main()
