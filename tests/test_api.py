import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import main as grid_main
from database import GridDatabase


class StatusApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_is_read_only_and_allows_dashboard_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = GridDatabase(Path(directory) / "grid.sqlite3")
            transport = httpx.ASGITransport(app=grid_main.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                grid_main.app.state.grid_bot = None
                with patch.object(grid_main, "GridDatabase", return_value=database):
                    offline = await client.get("/api/bot/status")
                self.assertEqual(offline.json(), {
                    "status": "Offline", "pair": "BTC/USDT",
                    "safety_pause": "Normal", "grid_levels": 0,
                    "lower_bound": None, "upper_bound": None,
                    "wallet": {
                        "btc_held": None, "average_cost": None,
                        "unrealized_pnl": None,
                    },
                })

                database.insert_order("buy", 1, "BUY", "81000", "0.01")
                database.insert_order("sell", -1, "SELL", "90000", "0.01")
                database.insert_order(
                    "seed", 0, "BUY", "84000", "0.01", order_type="MARKET"
                )
                database.mark_order_filled("seed")
                database.set_state(grid_main.FILL_SNAPSHOT_PREFIX + "seed",
                                   '{"filled_base":"0.01","filled_quote":"840",'
                                   '"base_fee":"0","quote_fee":"0"}')
                database.insert_order("filled", 2, "BUY", "78000", "0.01")
                database.mark_order_filled("filled")
                database.set_state(grid_main.FILL_SNAPSHOT_PREFIX + "filled",
                                   '{"filled_base":"0.01","filled_quote":"780",'
                                   '"base_fee":"0","quote_fee":"0"}')
                database.set_state(grid_main.LAST_MARKET_PRICE_KEY, "85000")
                database.set_state(grid_main.SAFETY_MODE_KEY, grid_main.PAUSED_DOWNSIDE)
                grid_main.app.state.grid_bot = SimpleNamespace(
                    config=SimpleNamespace(symbol="BTC/USDT"), database=database
                )
                try:
                    response = await client.get(
                        "/api/bot/status",
                        headers={"Origin": "http://localhost:5173"},
                    )
                    database.clear_state(grid_main.SAFETY_MODE_KEY)
                    resumed = await client.get("/api/bot/status")
                finally:
                    grid_main.app.state.grid_bot = None

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        self.assertEqual(response.json(), {
            "status": "Online", "pair": "BTC/USDT",
            "safety_pause": "Active", "grid_levels": 2,
            "lower_bound": 81000.0, "upper_bound": 90000.0,
            "wallet": {
                "btc_held": 0.02, "average_cost": 81000.0,
                "unrealized_pnl": 80.0,
            },
        })
        self.assertEqual(resumed.json()["safety_pause"], "Normal")

    async def test_wallet_values_carry_and_partial_sell_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = GridDatabase(Path(directory) / "grid.sqlite3")
            database.insert_order(
                "carry-1", 0, "BUY", "84473.58", "0.00591",
                order_type="MARKET",
            )
            database.mark_order_filled("carry-1")
            database.set_state(
                "carry_inventory", '{"order_id":"carry-1","cost":"499.2388578"}'
            )
            database.set_state(grid_main.LAST_MARKET_PRICE_KEY, "85000")
            wallet = grid_main._portfolio_wallet(database)
            self.assertEqual(wallet["btc_held"], 0.00591)
            self.assertAlmostEqual(wallet["average_cost"], 84473.58)
            self.assertAlmostEqual(
                wallet["unrealized_pnl"], 0.00591 * 85000 - 499.2388578
            )

            database.insert_order(
                "sell-1", -1, "SELL", "90000", "0.002",
                parent_order_id="carry-1",
            )
            database.update_order_status("sell-1", "PARTIALLY_FILLED")
            database.set_state(
                grid_main.FILL_SNAPSHOT_PREFIX + "sell-1",
                '{"filled_base":"0.001","filled_quote":"90",'
                '"base_fee":"0.0001","quote_fee":"0"}',
            )
            database.set_state(grid_main.LAST_MARKET_PRICE_KEY, "80000")
            wallet = grid_main._portfolio_wallet(database)
            expected_held = 0.00481
            expected_cost = 499.2388578 * expected_held / 0.00591
            self.assertAlmostEqual(wallet["btc_held"], expected_held)
            self.assertAlmostEqual(wallet["average_cost"],
                                   expected_cost / expected_held)
            self.assertAlmostEqual(wallet["unrealized_pnl"],
                                   expected_held * 80000 - expected_cost)
            self.assertLess(wallet["unrealized_pnl"], 0)

            database.insert_order("unknown", 1, "BUY", "79000", "0.001")
            database.mark_order_filled("unknown")
            self.assertEqual(
                grid_main._portfolio_wallet(database),
                grid_main._unavailable_wallet(),
            )

    async def test_api_and_telegram_bot_run_concurrently(self) -> None:
        api_started = asyncio.Event()
        server_instances = []

        class FakeServer:
            def __init__(self, _config):
                self.should_exit = False
                server_instances.append(self)

            async def serve(self):
                api_started.set()
                while not self.should_exit:
                    await asyncio.sleep(0.01)

        class FakeBot:
            async def run(self, _telegram_bot):
                await api_started.wait()

        with (
            patch.object(grid_main.uvicorn, "Config", return_value=object()),
            patch.object(grid_main.uvicorn, "Server", FakeServer),
        ):
            await asyncio.wait_for(
                grid_main._run_services(FakeBot(), object()), timeout=2
            )

        self.assertTrue(api_started.is_set())
        self.assertTrue(server_instances[0].should_exit)
        self.assertIsNone(grid_main.app.state.grid_bot)

    async def test_api_bind_failure_does_not_stop_trading_loop(self) -> None:
        class FailingServer:
            def __init__(self, _config):
                self.should_exit = False

            async def serve(self):
                raise SystemExit(1)

        class FakeBot:
            ran = False

            async def run(self, _telegram_bot):
                self.ran = True

        bot = FakeBot()
        with (
            patch.object(grid_main.uvicorn, "Config", return_value=object()),
            patch.object(grid_main.uvicorn, "Server", FailingServer),
            patch.object(grid_main.LOGGER, "error") as logged,
        ):
            await grid_main._run_services(bot, object())

        self.assertTrue(bot.ran)
        logged.assert_called_once()
        self.assertIsNone(grid_main.app.state.grid_bot)


if __name__ == "__main__":
    unittest.main()
