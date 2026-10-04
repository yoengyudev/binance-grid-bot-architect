import asyncio
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from database import GridDatabase
from settings import FILL_SNAPSHOT_PREFIX


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
            buy_messages = [m for m in messages if "*BUY FILLED*" in m and notifier.markdown_text(buy["order_id"]) in m]
            sell_messages = [m for m in messages if "*SELL FILLED" in m and notifier.markdown_text(sell["order_id"]) in m]
            pnl_messages = [m for m in messages if "*SELL FILLED \u2014 Realized P&L*" in m]
            self.assertEqual(len(buy_messages), 1)
            self.assertEqual(len(sell_messages), 1)
            self.assertEqual(len(pnl_messages), 1)
            trade = bot.database.fetch_trade_history()[0]
            self.assertIn(notifier.markdown_text(trade["profit"]), pnl_messages[0])
            self.assertIn("Side: BUY", buy_messages[0])
            self.assertIn("Side: SELL", sell_messages[0])
            self.assertFalse(any('Order FILLED' in m or 'pending accounting' in m for m in messages))
            self.assertEqual(sum(notifier.markdown_text(sell['order_id']) in m for m in messages), 1)
            self.assertIn('Realized P&L: 0 USDT', buy_messages[0])

    def make_filled_sell(self, directory):
        bot, exchange = self.make_bot(directory)
        with patch.object(grid_engine, "schedule_telegram_alert", return_value=False):
            bot.run_cycle()
            bot.run_cycle()
            buy = bot.database.fetch_latest_orders_by_level()[1]
            exchange.fill(buy["client_order_id"])
            bot.run_cycle()
        sell = bot.database.fetch_latest_orders_by_level()[1]
        exchange.fill(sell["client_order_id"])
        return bot, exchange, sell

    def test_sell_waits_for_committed_accounting_and_retries_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange, sell = self.make_filled_sell(directory)
            with patch.object(grid_engine, "schedule_telegram_alert", return_value=True) as send:
                with self.assertLogs("engine", level="INFO") as logs:
                    bot._reconcile_order(sell)
                self.assertTrue(any("Order FILLED" in log for log in logs.output))
                send.assert_not_called()
                self.assertIsNone(bot.database.get_trade_by_sell_order_id(sell["order_id"]))

                with closing(sqlite3.connect(bot.database.path)) as connection, connection:
                    connection.execute("CREATE TRIGGER fail_trade BEFORE INSERT ON trade_history "
                                       "BEGIN SELECT RAISE(ABORT, 'accounting failure'); END")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "accounting failure"):
                    bot._record_filled_sell(sell)
                send.assert_not_called()
                self.assertEqual(bot.database.fetch_trade_history(), [])
                with closing(sqlite3.connect(bot.database.path)) as connection, connection:
                    connection.execute("DROP TRIGGER fail_trade")

                # Queue refusal must not claim that a completed alert was scheduled.
                send.return_value = False
                bot._record_filled_sell(sell)
                self.assertIsNone(bot.database.get_state(f"telegram_pnl_scheduled:{sell['order_id']}"))

                def verify_committed(message):
                    self.assertFalse(bot.database.in_transaction)
                    saved = GridDatabase(bot.database.path).get_trade_by_sell_order_id(sell["order_id"])
                    self.assertIsNotNone(saved)
                    self.assertIn(notifier.markdown_text(saved["profit"]), message)
                    return True

                send.reset_mock()
                send.side_effect = verify_committed
                bot._record_filled_sell(sell)
                bot.database = GridDatabase(bot.database.path)
                bot._record_filled_sell(sell)
                send.assert_called_once()
                self.assertIn("*SELL FILLED", send.call_args.args[0])

    def test_outer_sell_accounting_rollback_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange, sell = self.make_filled_sell(directory)
            parent = bot.database.get_order(sell["parent_order_id"])
            # Fetch before the transaction; the engine prohibits exchange I/O inside it.
            executions = {row["order_id"]: exchange.fetch_order(row["exchange_order_id"], bot.config.symbol)
                          for row in (parent, sell)}
            with patch.object(grid_engine, "schedule_telegram_alert", return_value=True) as send, \
                    patch.object(bot, "_fetch_order", side_effect=lambda row, **kwargs: executions[row["order_id"]]):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    with bot.database.transaction():
                        bot._record_filled_sell(sell)
                        send.assert_not_called()
                        raise RuntimeError("interrupted before outer commit")
                self.assertEqual(bot.database.fetch_trade_history(), [])
                self.assertEqual(bot.database.get_order(sell["order_id"])["status"], "OPEN")
                bot._record_filled_sell(sell)
                send.assert_called_once()

    def test_buy_waits_for_fee_reconciliation_and_deduplicates_after_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(directory)
            with patch.object(grid_engine, "schedule_telegram_alert", return_value=False):
                bot.run_cycle()
                bot.run_cycle()
            buy = bot.database.fetch_latest_orders_by_level()[1]
            exchange.fill(buy["client_order_id"])
            filled = Decimal(buy["amount"])
            base_fee = filled * Decimal("0.001")
            exchange.has = {"fetchOrderTrades": True}
            no_fee = {"order": buy["exchange_order_id"], "amount": str(filled)}
            execution = dict(no_fee, info={"qty": str(filled), "commission": str(base_fee),
                                           "commissionAsset": "BTC"})

            def verify_committed(message):
                self.assertFalse(bot.database.in_transaction)
                saved = GridDatabase(bot.database.path)
                self.assertEqual(saved.get_order(buy["order_id"])["status"], "FILLED")
                snapshot = json.loads(saved.get_state(FILL_SNAPSHOT_PREFIX + buy["order_id"]))
                self.assertEqual(snapshot["fee_source"], "trades")
                self.assertEqual(Decimal(snapshot["base_fee"]), base_fee)
                self.assertIn(f"Net inventory: {notifier.markdown_text(filled - base_fee)} BTC", message)
                self.assertIn("Realized P&L: 0 USDT", message)
                return True

            with patch.object(grid_engine, "schedule_telegram_alert", side_effect=verify_committed) as send, \
                    patch.object(exchange, "fetch_order_trades", side_effect=[[], [no_fee], [execution]], create=True) as trades:
                bot._reconcile_order(buy)
                send.assert_not_called()
                order = exchange.fetch_order(buy["exchange_order_id"], bot.config.symbol)
                self.assertIsNone(bot._net_buy_fill_base(buy, order))
                self.assertIsNone(bot._net_buy_fill_base(buy, order))
                send.assert_not_called()
                self.assertEqual(bot._net_buy_fill_base(buy, order), filled - base_fee)
                bot.database = GridDatabase(bot.database.path)
                self.assertEqual(bot._net_buy_fill_base(buy, order), filled - base_fee)
                send.assert_called_once()
                self.assertEqual(trades.call_count, 3)
                self.assertIsNone(bot.database.get_state(f"telegram_buy_pending:{buy['order_id']}"))

    def test_buy_fill_rollback_and_queue_refusal_do_not_mark_alert_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(directory)
            with patch.object(grid_engine, "schedule_telegram_alert", return_value=False):
                bot.run_cycle()
                bot.run_cycle()
            buy = bot.database.fetch_latest_orders_by_level()[1]
            exchange.fill(buy["client_order_id"])
            pending_key = f"telegram_buy_pending:{buy['order_id']}"
            scheduled_key = f"telegram_buy_scheduled:{buy['order_id']}"
            order = exchange.fetch_order(buy["exchange_order_id"], bot.config.symbol)
            with patch.object(grid_engine, "schedule_telegram_alert", return_value=False) as send:
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    with bot.database.transaction():
                        bot._persist_order_response(buy, order, "FILLED")
                        bot._net_buy_fill_base(buy, order)
                        send.assert_not_called()
                        raise RuntimeError("interrupted before outer commit")
                self.assertEqual(bot.database.get_order(buy["order_id"])["status"], "OPEN")
                self.assertIsNone(bot.database.get_state(pending_key))
                bot._reconcile_order(buy)
                bot._net_buy_fill_base(buy, order)
                self.assertEqual(bot.database.get_state(pending_key), "1")
                self.assertIsNone(bot.database.get_state(scheduled_key))
                send.reset_mock()
                send.return_value = True
                bot._net_buy_fill_base(buy, order)
                bot._net_buy_fill_base(buy, order)
                send.assert_called_once()
                self.assertEqual(bot.database.get_state(scheduled_key), "1")
                self.assertIsNone(bot.database.get_state(pending_key))

    def test_inventory_cleanup_uses_saved_buy_accounting_without_extra_network_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, exchange = self.make_bot(directory)
            with patch.object(grid_engine, "schedule_telegram_alert", return_value=False):
                bot.run_cycle()
                bot.run_cycle()
            buy = bot.database.fetch_latest_orders_by_level()[1]
            exchange.fill(buy["client_order_id"])
            exchange.has = {"fetchOrderTrades": True}
            with patch.object(grid_engine, "schedule_telegram_alert", return_value=True) as send, \
                    patch.object(exchange, "fetch_order_trades", create=True) as trades:
                bot._reconcile_order(buy)
                bot._carry_inventory(require_available=False)
                send.assert_not_called()  # Missing commissions must not produce a completed BUY alert.
                order = exchange.fetch_order(buy["exchange_order_id"], bot.config.symbol)
                fee = Decimal(buy["amount"]) * Decimal("0.001")
                bot._save_fill_snapshot(buy["order_id"], order, verified_fees=(fee, Decimal(0)))
                bot._carry_inventory(require_available=False)
                bot._carry_inventory(require_available=False)
                send.assert_called_once()
                self.assertIn("*BUY FILLED*", send.call_args.args[0])
                trades.assert_not_called()

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
