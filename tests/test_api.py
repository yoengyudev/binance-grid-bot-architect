import asyncio
import unittest
from decimal import Decimal
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import main as grid_main


class StatusApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_is_read_only_and_allows_dashboard_origin(self) -> None:
        transport = httpx.ASGITransport(app=grid_main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            grid_main.app.state.grid_bot = None
            offline = await client.get("/api/bot/status")
            self.assertEqual(offline.json()["status"], "offline")

            fake_bot = SimpleNamespace(
                config=SimpleNamespace(symbol="BTC/USDT"),
                grid_configuration=lambda: (
                    Decimal("75000"), Decimal("89000"), 6, Decimal("74500")
                ),
                stop_controller=SimpleNamespace(stop_requested=Event()),
                is_paused=True,
                grid_needs_reset=False,
            )
            grid_main.app.state.grid_bot = fake_bot
            try:
                response = await client.get(
                    "/api/bot/status",
                    headers={"Origin": "http://localhost:5173"},
                )
            finally:
                grid_main.app.state.grid_bot = None

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        self.assertEqual(response.json(), {
            "status": "safety_pause",
            "pair": "BTC/USDT",
            "safety_pause": True,
            "lower_price": "75000",
            "upper_price": "89000",
            "grid_levels": 6,
            "pause_trigger": "74500",
        })

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
