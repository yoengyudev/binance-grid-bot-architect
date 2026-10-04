import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ccxt

import api as grid_api
import main as grid_main
import market_data as grid_market_data
import models as grid_models
import portfolio as grid_portfolio
import settings as grid_settings
import httpx
import jwt
from fastapi import Depends, FastAPI

from database import GridDatabase


class StatusApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Status now reads the account; keep test monitor sessions off the network.
        factory = patch.object(grid_api, "create_exchange", return_value=Mock())
        factory.start()
        self.addCleanup(factory.stop)

    async def test_status_reports_idle_and_halted_account_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = GridDatabase(Path(directory) / "grid_testnet.sqlite")
            bot = AccountExposureTests.make_bot(database, "0.75", "0.25")
            bot.exchange.fetch_open_orders.return_value = [{
                "symbol": "BTC/USDT", "side": "sell", "type": "limit",
                "amount": "0.5", "filled": "0.25", "remaining": "0.25",
            }]
            previous = grid_api.app.state.grid_bot
            grid_api.app.state.grid_bot = bot
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="http://testserver",
                ) as client:
                    for mode in (grid_settings.ENGINE_IDLE, grid_settings.ENGINE_HALTED):
                        database.set_state(grid_settings.ENGINE_STATUS_KEY, mode)
                        payload = (await self.authenticated_status(client)).json()
                        self.assertEqual(payload["engine_status"], mode)
                        self.assertEqual(payload["unprotected_btc_exposure"], 0.75)
                        self.assertTrue(payload["has_unprotected_exposure"])
                    grid_api.app.state.standby = True
                    payload = (await self.authenticated_status(client)).json()
                    self.assertEqual(payload["unprotected_btc_exposure"], 0.75)
                    self.assertTrue(payload["has_unprotected_exposure"])
            finally:
                grid_api.app.state.grid_bot = previous
                grid_api.app.state.standby = False

    async def test_configured_bounds_and_audit_survive_order_cycle_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = GridDatabase(Path(directory) / "grid_testnet.sqlite")
            audited_at = "2026-10-03T13:00:00+00:00"
            database.set_state(grid_settings.LAST_EXCHANGE_AUDIT_TIME_KEY, audited_at)
            database.set_state("active_grid_config", json.dumps({
                "lower": "75", "upper": "125",
            }))
            bot = SimpleNamespace(config=SimpleNamespace(
                symbol="BTC/USDT", lower_price=Decimal("80"),
                upper_price=Decimal("120"), stop_loss_price=Decimal("70"),
            ), database=database)
            previous = (grid_api.app.state.grid_bot, grid_api.app.state.standby,
                        grid_api.app.state.monitor_only)
            try:
                grid_api.app.state.grid_bot = bot
                grid_api.app.state.monitor_only = False
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="http://testserver",
                ) as client:
                    for standby in (False, True):
                        grid_api.app.state.standby = standby
                        payload = (await self.authenticated_status(client)).json()
                        self.assertEqual(payload["configured_lower_bound"], 80.0)
                        self.assertEqual(payload["configured_upper_bound"], 120.0)
                        self.assertIsNone(payload["lower_bound"])
                        self.assertEqual(payload["last_exchange_audit_time"], audited_at)
                    grid_api.app.state.standby = False
                    database.insert_order("buy", 1, "BUY", "90", "0.01")
                    payload = (await self.authenticated_status(client)).json()
                    self.assertEqual(payload["lower_bound"], 90.0)
                    self.assertEqual(payload["configured_lower_bound"], 80.0)
                    self.assertEqual(payload["configured_upper_bound"], 120.0)
                    grid_api.app.state.grid_bot = None
                    with patch.object(grid_api, "GridDatabase", return_value=database):
                        payload = (await self.authenticated_status(client)).json()
                    self.assertEqual(payload["configured_lower_bound"], 75.0)
                    self.assertEqual(payload["configured_upper_bound"], 125.0)
                    self.assertEqual(payload["last_exchange_audit_time"], audited_at)
                    grid_api.app.state.monitor_only = True
                    with patch.object(grid_api, "GridDatabase", side_effect=AssertionError("DB opened")):
                        payload = (await self.authenticated_status(client)).json()
                    self.assertIsNone(payload["last_exchange_audit_time"])
                    self.assertIsNone(payload["configured_lower_bound"])
                    self.assertIsNone(payload["configured_upper_bound"])
            finally:
                (grid_api.app.state.grid_bot, grid_api.app.state.standby,
                 grid_api.app.state.monitor_only) = previous

    async def authenticated_status(self, client, **kwargs):
        # Payload tests isolate status generation; cookie validation has its own tests.
        overrides = grid_api.app.dependency_overrides
        previous = overrides.copy()
        overrides[grid_api.get_current_user] = lambda: "admin"
        try:
            return await client.get("/api/bot/status", **kwargs)
        finally:
            overrides.clear()
            overrides.update(previous)

    async def test_status_exposes_recorded_realized_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = GridDatabase(Path(directory) / "grid_testnet.sqlite")
            database.record_trade("100", "110", "9.79", sell_order_id="sell-1")
            grid_api.app.state.grid_bot = SimpleNamespace(
                config=SimpleNamespace(symbol="BTC/USDT", stop_loss_price=70000),
                database=database,
            )
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="http://testserver",
                ) as client:
                    response = await self.authenticated_status(client)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["realized_pnl"], {
                    "today": 9.79, "seven_day": 9.79, "total": 9.79,
                })
            finally:
                grid_api.app.state.grid_bot = None

    async def test_live_order_ledger_requires_admin_and_uses_saved_fill_price(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = GridDatabase(Path(directory) / "grid_testnet.sqlite")
            database.insert_order("buy-open", 1, "BUY", "82000", "0.02",
                                  client_order_id="gridbot-open")
            database.insert_order("sell-filled", -1, "SELL", "88000", "0.01",
                                  client_order_id="gridbot-filled")
            database.mark_order_filled("sell-filled")
            database.set_state(grid_settings.FILL_SNAPSHOT_PREFIX + "sell-filled",
                               '{"filled_base":"0.009","filled_quote":"792",'
                               '"base_fee":"0","quote_fee":"0"}')
            grid_api.app.state.grid_bot = SimpleNamespace(database=database)
            try:
                with patch.dict(os.environ, {
                    "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
                    "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
                }):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=grid_api.app),
                        base_url="https://testserver",
                    ) as client:
                        self.assertEqual((await client.get("/api/orders/live")).status_code, 401)
                        login = await client.post(
                            "/api/auth/login",
                            json={"password": "unique-private-admin-password"},
                            headers={"Origin": "http://localhost:5173"},
                        )
                        self.assertEqual(login.status_code, 200)
                        response = await client.get("/api/orders/live")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.json()["open_limits"][0]["id"], "buy-open")
                filled = response.json()["filled_trades"][0]
                self.assertEqual(filled["id"], "sell-filled")
                self.assertEqual(filled["price"], "88000")
                self.assertEqual(filled["amount"], "0.009")
                self.assertEqual(filled["price_source"], "execution")
            finally:
                grid_api.app.state.grid_bot = None

    async def test_standby_reports_idle_and_rejects_trading_controls(self) -> None:
        standby_bot = Mock()
        standby_bot.config.symbol = "BTC/USDT"
        standby_bot.config.lower_price = Decimal("80")
        standby_bot.config.upper_price = Decimal("120")
        standby_bot.database.get_state.return_value = None
        standby_bot.database.realized_pnl_summary.return_value = {
            "today": Decimal(0), "seven_day": Decimal(0), "total": Decimal(0),
        }
        standby_bot.request_initial_grid.return_value = (Decimal("80"), Decimal("120"))
        grid_api.app.state.grid_bot = standby_bot
        grid_api.app.state.standby = True
        try:
            with patch.dict(os.environ, {
                "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
                "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
            }), patch.object(grid_api, "GridDatabase", side_effect=AssertionError("DB opened")):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="https://testserver",
                ) as client:
                    status_response = await self.authenticated_status(client)
                    self.assertEqual(status_response.status_code, 200)
                    self.assertEqual(status_response.json()["trading_state"], "IDLE")
                    self.assertEqual(status_response.json()["grid_levels"], 0)
                    self.assertTrue(status_response.json()["exact_grid_recenter_supported"])
                    login = await client.post(
                        "/api/auth/login",
                        json={"password": "unique-private-admin-password"},
                        headers={"Origin": "http://localhost:5173"},
                    )
                    self.assertEqual(login.status_code, 200)
                    pause = await client.post(
                        "/api/bot/pause", json={"active": True},
                        headers={"Origin": "http://localhost:5173"},
                    )
                    self.assertEqual(pause.status_code, 409)
                    self.assertEqual(pause.json()["detail"], "The bot is idle; no grid is active.")
                    start = await client.post(
                        "/api/bot/grid/recenter",
                        json={"center_price": 100, "width_percentage": 20,
                              "stop_loss_percentage": 30, "allocated_capital": 1000,
                              "grid_levels": 10},
                        headers={"Origin": "http://localhost:5173"},
                    )
                    self.assertEqual(start.status_code, 202)
                    self.assertEqual(start.json()["status"], "queued")
            standby_bot.run.assert_not_called()
            standby_bot.set_manual_pause.assert_not_called()
            standby_bot.request_initial_grid.assert_called_once()
        finally:
            grid_api.app.state.standby = False
            grid_api.app.state.grid_bot = None

    async def test_monitor_only_status_never_opens_database_or_enables_trading(self) -> None:
        grid_api.app.state.grid_bot = None
        grid_api.app.state.monitor_only = True
        try:
            with patch.object(grid_api, "GridDatabase", side_effect=AssertionError("DB opened")):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="http://testserver",
                ) as client:
                    response = await self.authenticated_status(client)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "Online")
            self.assertEqual(response.json()["trading_state"], "STOPPED")
            self.assertEqual(response.json()["grid_levels"], 0)
            self.assertEqual(response.json()["realized_pnl"], {
                "today": None, "seven_day": None, "total": None,
            })
            self.assertFalse(response.json()["exact_grid_recenter_supported"])
        finally:
            grid_api.app.state.monitor_only = False

    async def test_monitor_only_wallet_is_read_only_and_controls_remain_unavailable(self) -> None:
        exchange = Mock()
        exchange.fetch_balance.return_value = {"USDT": {"free": "9403.45"}}
        grid_api.app.state.grid_bot = None
        grid_api.app.state.monitor_only = True
        try:
            with patch.dict(os.environ, {
                "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
                "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
            }), patch.object(grid_api, "create_exchange", return_value=exchange):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="https://testserver",
                ) as client:
                    self.assertEqual((await client.get("/api/wallet-balance")).status_code, 401)
                    login = await client.post(
                        "/api/auth/login",
                        json={"password": "unique-private-admin-password"},
                        headers={"Origin": "http://localhost:5173"},
                    )
                    self.assertEqual(login.status_code, 200)
                    wallet = await client.get("/api/wallet-balance")
                    self.assertEqual(wallet.json(), {"available_usdt": "9403.45"})
                    pause = await client.post(
                        "/api/bot/pause", json={"active": True},
                        headers={"Origin": "http://localhost:5173"},
                    )
                    self.assertEqual(pause.status_code, 503)
            exchange.create_order.assert_not_called()
        finally:
            grid_api.app.state.monitor_only = False

    async def test_status_is_read_only_and_allows_dashboard_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = GridDatabase(Path(directory) / "grid_testnet.sqlite")
            transport = httpx.ASGITransport(app=grid_api.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                grid_api.app.state.grid_bot = None
                with patch.object(grid_api, "GridDatabase", return_value=database):
                    offline = await self.authenticated_status(client)
                self.assertEqual(offline.json(), {
                    "trading_environment": "TESTNET",
                    "status": "Offline", "pair": "BTC/USDT",
                    "safety_pause": "Normal", "pause_mode": None,
                    "trading_state": "IDLE", "engine_status": "IDLE",
                    "engine_fault": None, "pending_grid": None,
                    "can_reset_liquidation": False,
                    "order_cleanup_required": False, "unresolved_order_alarm": None,
                    "ghost_order_alarm": None,
                    "has_grid_run": False,
                    "grid_levels": 0,
                    "exact_grid_recenter_supported": True,
                    "lower_bound": None, "upper_bound": None,
                    "configured_lower_bound": None, "configured_upper_bound": None,
                    "last_exchange_audit_time": None,
                    "unprotected_btc_exposure": None, "has_unprotected_exposure": False,
                    "atr_value": None, "atr_percentage": None,
                    "bid_volume": None, "ask_volume": None,
                    "imbalance_ratio": None,
                    "current_hard_stop_loss": None,
                    "high_water_mark": None,
                    "wallet": {
                        "btc_held": None, "average_cost": None,
                        "unrealized_pnl": None,
                    },
                    "realized_pnl": {
                        "today": 0.0, "seven_day": 0.0, "total": 0.0,
                    },
                })
                database.require_order_cleanup("Cancellation could not be verified.")
                with patch.object(grid_api, "GridDatabase", return_value=database):
                    alarm = await self.authenticated_status(client)
                self.assertEqual(alarm.json()["engine_status"], "IDLE")
                self.assertTrue(alarm.json()["order_cleanup_required"])
                self.assertEqual(alarm.json()["engine_fault"],
                                 "Cancellation could not be verified.")
                database.complete_order_cleanup()
                database.clear_state(grid_settings.ENGINE_STATUS_KEY)

                saved_grid = json.dumps({
                    "phase": "placing", "source": "manual_recenter",
                    "center_price": "83700", "lower": "79129.8",
                    "upper": "88270.2", "buy_levels": 5, "sell_levels": 5,
                }, sort_keys=True)
                database.set_state("grid_reset", saved_grid)
                with patch.object(grid_api, "GridDatabase", return_value=database):
                    pending_status = await self.authenticated_status(client)
                preview = pending_status.json()["pending_grid"]
                self.assertEqual(preview["center"], "83700")
                self.assertEqual(preview["buy_levels"], 5)
                self.assertEqual(preview["token"], hashlib.sha256(
                    saved_grid.encode("utf-8")
                ).hexdigest())
                database.clear_state("grid_reset")

                database.insert_order("buy", 1, "BUY", "81000", "0.01")
                database.insert_order("sell", -1, "SELL", "90000", "0.01")
                database.insert_order(
                    "seed", 0, "BUY", "84000", "0.01", order_type="MARKET"
                )
                database.mark_order_filled("seed")
                database.set_state(grid_settings.FILL_SNAPSHOT_PREFIX + "seed",
                                   '{"filled_base":"0.01","filled_quote":"840",'
                                   '"base_fee":"0","quote_fee":"0"}')
                database.insert_order("filled", 2, "BUY", "78000", "0.01")
                database.mark_order_filled("filled")
                database.set_state(grid_settings.FILL_SNAPSHOT_PREFIX + "filled",
                                   '{"filled_base":"0.01","filled_quote":"780",'
                                   '"base_fee":"0","quote_fee":"0"}')
                database.set_state(grid_settings.LAST_MARKET_PRICE_KEY, "85000")
                database.set_state(grid_settings.SAFETY_MODE_KEY, grid_settings.PAUSED_DOWNSIDE)
                database.set_state(grid_settings.TRAILING_STOP_KEY,
                                   '{"high_water_mark":"85000",'
                                   '"stop_loss_distance":"15000"}')
                grid_api.app.state.grid_bot = SimpleNamespace(
                    config=SimpleNamespace(symbol="BTC/USDT",
                                           stop_loss_price=70000), database=database
                )
                grid_api.app.state.atr_snapshot = {
                    "atr_value": 850.5, "atr_percentage": 1.0,
                }
                grid_api.app.state.order_book_snapshot = {
                    "bid_volume": 15.2, "ask_volume": 4.1,
                    "imbalance_ratio": 15.2 / 4.1,
                }
                try:
                    response = await self.authenticated_status(
                        client,
                        headers={"Origin": "http://localhost:5173"},
                    )
                    database.clear_state(grid_settings.SAFETY_MODE_KEY)
                    resumed = await self.authenticated_status(client)
                    database.set_state(grid_settings.SAFETY_MODE_KEY, grid_settings.LIQUIDATED)
                    database.set_state(grid_settings.LIQUIDATION_KEY,
                                       '{"phase":"complete","residual_base":"0.0001"}')
                    liquidated = await self.authenticated_status(client)
                finally:
                    grid_api.app.state.grid_bot = None
                    grid_api.app.state.atr_snapshot = None
                    grid_api.app.state.order_book_snapshot = None

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "http://localhost:5173",
        )
        self.assertEqual(response.json(), {
            "trading_environment": "TESTNET",
            "status": "Online", "pair": "BTC/USDT",
            "safety_pause": "Active", "pause_mode": grid_settings.PAUSED_DOWNSIDE,
            "trading_state": "ACTIVE", "engine_status": "RUNNING",
            "engine_fault": None, "pending_grid": None,
            "can_reset_liquidation": False,
            "order_cleanup_required": False, "unresolved_order_alarm": None,
            "ghost_order_alarm": None,
            "has_grid_run": False,
            "grid_levels": 2,
            "exact_grid_recenter_supported": True,
            "lower_bound": 81000.0, "upper_bound": 90000.0,
            "configured_lower_bound": None, "configured_upper_bound": None,
            "last_exchange_audit_time": None,
            "unprotected_btc_exposure": None, "has_unprotected_exposure": False,
            "atr_value": 850.5, "atr_percentage": 1.0,
            "bid_volume": 15.2, "ask_volume": 4.1,
            "imbalance_ratio": 15.2 / 4.1,
            "current_hard_stop_loss": 70000.0,
            "high_water_mark": 85000.0,
            "wallet": {
                "btc_held": 0.02, "average_cost": 81000.0,
                "unrealized_pnl": 80.0,
            },
            "realized_pnl": {
                "today": 0.0, "seven_day": 0.0, "total": 0.0,
            },
        })
        self.assertEqual(resumed.json()["safety_pause"], "Normal")
        self.assertEqual(liquidated.json()["trading_state"], grid_settings.LIQUIDATED)
        self.assertEqual(liquidated.json()["wallet"]["btc_held"], 0.0001)

    async def test_hourly_atr_uses_15_candles_and_handles_missing_data(self) -> None:
        candles = [
            [index * 3_600_000, 100, 110, 90, 100, 1]
            for index in range(15)
        ]
        snapshot = grid_market_data._calculate_atr_snapshot(candles)
        self.assertIsNotNone(snapshot)
        self.assertAlmostEqual(snapshot["atr_value"], 20)
        self.assertAlmostEqual(snapshot["atr_percentage"], 20)
        self.assertIsNone(grid_market_data._calculate_atr_snapshot(candles[:14]))
        candles[-1][4] = None
        self.assertIsNone(grid_market_data._calculate_atr_snapshot(candles))

    async def test_order_book_uses_top_50_levels_and_rejects_empty_asks(self) -> None:
        book = {
            "bids": [[100 - index, 0.1] for index in range(50)] + [[49, 100]],
            "asks": [[101 + index, 0.2] for index in range(50)] + [[151, 100]],
        }
        snapshot = grid_market_data._calculate_order_book_snapshot(book)
        self.assertIsNotNone(snapshot)
        self.assertAlmostEqual(snapshot["bid_volume"], 5)
        self.assertAlmostEqual(snapshot["ask_volume"], 10)
        self.assertAlmostEqual(snapshot["imbalance_ratio"], 0.5)
        self.assertIsNone(grid_market_data._calculate_order_book_snapshot({
            "bids": book["bids"], "asks": [],
        }))
        self.assertIsNone(grid_market_data._calculate_order_book_snapshot({
            "bids": [[100, float("nan")]], "asks": book["asks"],
        }))

    async def test_order_book_timeout_does_not_clear_atr(self) -> None:
        grid_api.app.state.atr_snapshot = None
        grid_api.app.state.order_book_snapshot = {"bid_volume": 1}
        try:
            with (
                patch.object(grid_market_data, "_fetch_atr_snapshot", return_value={
                    "atr_value": 20, "atr_percentage": 1,
                }),
                patch.object(grid_market_data, "_fetch_order_book_snapshot",
                             side_effect=TimeoutError),
                patch.object(grid_market_data.asyncio, "sleep",
                             side_effect=asyncio.CancelledError),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await grid_market_data._refresh_market_data(object(), grid_api.app.state)
            self.assertEqual(grid_api.app.state.atr_snapshot["atr_value"], 20)
            self.assertIsNone(grid_api.app.state.order_book_snapshot)
        finally:
            grid_api.app.state.atr_snapshot = None
            grid_api.app.state.order_book_snapshot = None

    async def test_admin_login_uses_strict_httponly_cookie(self) -> None:
        protected_app = FastAPI()

        @protected_app.get("/protected")
        def protected(user: str = Depends(grid_api.get_current_user)) -> dict:
            return {"user": user}

        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "",
            "BOT_JWT_SECRET": "",
            "BOT_AUTH_MOCK_ENABLED": "1",
        }):
            grid_api.app.state.grid_bot = None
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="https://testserver",
                ) as auth_client,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=protected_app),
                    base_url="https://testserver",
                ) as protected_client,
            ):
                wrong = await auth_client.post(
                    "/api/auth/login", json={"password": "wrong"},
                    headers={"Origin": "http://localhost:5173"},
                )
                self.assertEqual(wrong.status_code, 401)
                login = await auth_client.post(
                    "/api/auth/login", json={"password": "admin123"},
                    headers={"Origin": "http://localhost:5173"},
                )
                self.assertEqual(login.status_code, 200)
                self.assertNotIn("access_token", login.json())
                self.assertEqual(login.json()["authenticated"], True)
                self.assertEqual(login.json()["expires_in"], 900)
                cookie = login.headers["set-cookie"]
                self.assertIn("httponly", cookie.lower())
                self.assertIn("secure", cookie.lower())
                self.assertIn("samesite=strict", cookie.lower())
                self.assertIn("path=/", cookie.lower())
                token = auth_client.cookies[grid_settings.AUTH_COOKIE_NAME]
                self.assertEqual((await protected_client.get("/protected")).status_code, 401)
                self.assertEqual(
                    (await protected_client.get(
                        "/protected", cookies={grid_settings.AUTH_COOKIE_NAME: "invalid"}
                    )).status_code, 401,
                )
                authorized = await protected_client.get(
                    "/protected", cookies={grid_settings.AUTH_COOKIE_NAME: token}
                )
                self.assertEqual(authorized.json(), {"user": "admin"})
                expired = jwt.encode({
                    "sub": "admin", "iss": grid_settings.JWT_ISSUER,
                    "aud": grid_settings.JWT_AUDIENCE,
                    "iat": datetime.now(timezone.utc) - timedelta(hours=1),
                    "nbf": datetime.now(timezone.utc) - timedelta(hours=1),
                    "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
                }, grid_api.app.state.preview_jwt_secret, algorithm="HS256")
                self.assertEqual(
                    (await protected_client.get(
                        "/protected", cookies={grid_settings.AUTH_COOKIE_NAME: expired}
                    )).status_code, 401,
                )
                forged = jwt.encode({
                    "sub": "admin", "iss": grid_settings.JWT_ISSUER,
                    "aud": grid_settings.JWT_AUDIENCE,
                    "iat": datetime.now(timezone.utc),
                    "nbf": datetime.now(timezone.utc),
                    "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
                }, "a-different-signing-secret-with-enough-length",
                    algorithm="HS256")
                self.assertEqual(
                    (await protected_client.get(
                        "/protected", cookies={grid_settings.AUTH_COOKIE_NAME: forged}
                    )).status_code, 401,
                )
                preflight = await auth_client.options(
                    "/api/auth/login",
                    headers={
                        "Origin": "http://localhost:5173",
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Headers": "content-type",
                    },
                )
                self.assertEqual(preflight.status_code, 200)
                self.assertEqual(
                    preflight.headers["access-control-allow-origin"],
                    "http://localhost:5173",
                )
                self.assertEqual(
                    preflight.headers["access-control-allow-credentials"], "true"
                )
                self.assertEqual((await auth_client.get("/api/auth/me")).status_code, 200)
                signed_out = await auth_client.post(
                    "/api/auth/logout", headers={"Origin": "http://localhost:5173"}
                )
                self.assertEqual(signed_out.status_code, 200)
                self.assertEqual((await auth_client.get("/api/auth/me")).status_code, 401)
                other_local_origin = await auth_client.options(
                    "/api/auth/login",
                    headers={
                        "Origin": "http://127.0.0.1:5173",
                        "Access-Control-Request-Method": "POST",
                    },
                )
                self.assertNotIn(
                    "access-control-allow-origin", other_local_origin.headers
                )
                blocked_origin = await auth_client.options(
                    "/api/auth/login",
                    headers={
                        "Origin": "https://example.invalid",
                        "Access-Control-Request-Method": "POST",
                    },
                )
                self.assertNotIn(
                    "access-control-allow-origin", blocked_origin.headers
                )

    async def test_frontend_origin_is_one_valid_environment_origin(self) -> None:
        with patch.dict(os.environ, {"FRONTEND_URL": "https://dashboard.example.test"}):
            self.assertEqual(
                grid_settings._configured_frontend_origin(),
                "https://dashboard.example.test",
            )
        for invalid in ("*", "https://*.example.test", "https://example.test/path",
                        "https://example.test,https://other.test", "ftp://example.test",
                        "http://example.test"):
            with patch.dict(os.environ, {"FRONTEND_URL": invalid}):
                with self.assertRaises(ValueError):
                    grid_settings._configured_frontend_origin()
        environment = dict(os.environ, FRONTEND_URL="https://dashboard.example.test")
        loaded = subprocess.run(
            [sys.executable, "-c", "import settings; import api; print(settings.FRONTEND_ORIGIN); "
             "print(api.app.user_middleware[0].kwargs['allow_origins'])"],
            cwd=Path(grid_main.__file__).parent,
            env=environment, capture_output=True, text=True, check=True,
        )
        self.assertEqual(loaded.stdout.splitlines(), [
            "https://dashboard.example.test",
            "['https://dashboard.example.test']",
        ])

    async def test_refresh_rotates_cookie_and_limits_expired_grace(self) -> None:
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "",
            "BOT_JWT_SECRET": "",
            "BOT_AUTH_MOCK_ENABLED": "1",
        }):
            grid_api.app.state.grid_bot = None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=grid_api.app),
                base_url="https://testserver",
            ) as client:
                origin = {"Origin": "http://localhost:5173"}
                self.assertEqual((await client.post(
                    "/api/auth/refresh", headers=origin
                )).status_code, 401)
                await client.post(
                    "/api/auth/login", json={"password": "admin123"}, headers=origin
                )
                refreshed = await client.post("/api/auth/refresh", headers=origin)
                self.assertEqual(refreshed.status_code, 200)
                self.assertEqual(refreshed.json()["expires_in"], 900)
                self.assertIn("max-age=960", refreshed.headers["set-cookie"].lower())
                self.assertEqual((await client.get("/api/auth/me")).status_code, 200)
                now = datetime.now(timezone.utc)
                def signed_cookie(age_seconds: int) -> str:
                    return jwt.encode({
                        "sub": "admin", "iss": grid_settings.JWT_ISSUER,
                        "aud": grid_settings.JWT_AUDIENCE,
                        "iat": now - timedelta(minutes=15),
                        "nbf": now - timedelta(minutes=15),
                        "exp": now - timedelta(seconds=age_seconds),
                    }, grid_api.app.state.preview_jwt_secret, algorithm="HS256")
                client.cookies.set(grid_settings.AUTH_COOKIE_NAME, signed_cookie(30))
                self.assertEqual((await client.get("/api/auth/me")).status_code, 401)
                self.assertEqual((await client.post(
                    "/api/auth/refresh", headers=origin
                )).status_code, 200)
                client.cookies.set(grid_settings.AUTH_COOKIE_NAME, signed_cookie(120))
                self.assertEqual((await client.post(
                    "/api/auth/refresh", headers=origin
                )).status_code, 401)
                self.assertEqual((await client.post(
                    "/api/auth/refresh", headers={"Origin": "https://other.test"}
                )).status_code, 403)

    async def test_live_bot_never_uses_mock_password(self) -> None:
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "",
            "BOT_JWT_SECRET": "",
            "BOT_AUTH_MOCK_ENABLED": "1",
        }):
            grid_api.app.state.grid_bot = object()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="https://testserver",
                ) as client:
                    response = await client.post(
                        "/api/auth/login", json={"password": "admin123"},
                        headers={"Origin": "http://localhost:5173"},
                    )
                self.assertEqual(response.status_code, 503)
            finally:
                grid_api.app.state.grid_bot = None

    async def test_live_bot_uses_private_admin_configuration(self) -> None:
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
            "BOT_AUTH_MOCK_ENABLED": "1",
        }):
            grid_api.app.state.grid_bot = object()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="http://testserver",
                ) as client:
                    mock = await client.post(
                        "/api/auth/login", json={"password": "admin123"},
                        headers={"Origin": "http://localhost:5173"},
                    )
                    private = await client.post(
                        "/api/auth/login",
                        json={"password": "unique-private-admin-password"},
                        headers={"Origin": "http://localhost:5173"},
                    )
                self.assertEqual(mock.status_code, 401)
                self.assertEqual(private.status_code, 200)
            finally:
                grid_api.app.state.grid_bot = None

    async def test_wallet_balance_requires_admin_and_handles_exchange_timeout(self) -> None:
        balance_reader = Mock(return_value=Decimal("123.4567"))
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
        }):
            grid_api.app.state.grid_bot = SimpleNamespace(_free_balance=balance_reader)
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="https://testserver",
                ) as client:
                    unauthenticated = await client.get("/api/wallet-balance")
                    self.assertEqual(unauthenticated.status_code, 401)
                    balance_reader.assert_not_called()

                    await client.post(
                        "/api/auth/login",
                        json={"password": "unique-private-admin-password"},
                        headers={"Origin": "http://localhost:5173"},
                    )
                    available = await client.get("/api/wallet-balance")
                    self.assertEqual(available.status_code, 200)
                    self.assertEqual(available.json(), {"available_usdt": "123.4567"})
                    self.assertEqual(available.headers["cache-control"], "no-store")
                    balance_reader.assert_called_once_with("USDT")

                    balance_reader.side_effect = ccxt.RequestTimeout("Exchange timed out")
                    unavailable = await client.get("/api/wallet-balance")
                    self.assertEqual(unavailable.status_code, 502)
                    self.assertEqual(unavailable.json()["detail"],
                                     "Could not fetch the Spot USDT balance.")

                    grid_api.app.state.grid_bot = None
                    offline = await client.get("/api/wallet-balance")
                    self.assertEqual(offline.status_code, 503)
            finally:
                grid_api.app.state.grid_bot = None

    async def test_pause_endpoint_requires_cookie_and_trusted_origin(self) -> None:
        calls = []
        bot = SimpleNamespace(set_manual_pause=lambda active: (
            calls.append(active) or (grid_settings.PAUSED_MANUAL if active else None)
        ))
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
        }):
            grid_api.app.state.grid_bot = bot
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="https://testserver",
                ) as client:
                    origin = {"Origin": "http://localhost:5173"}
                    unauthenticated = await client.post(
                        "/api/bot/pause", json={"active": True}, headers=origin
                    )
                    self.assertEqual(unauthenticated.status_code, 401)
                    self.assertEqual(calls, [])
                    await client.post(
                        "/api/auth/login", json={"password": "unique-private-admin-password"},
                        headers=origin,
                    )
                    bad_origin = await client.post(
                        "/api/bot/pause", json={"active": True},
                        headers={"Origin": "https://example.invalid"},
                    )
                    self.assertEqual(bad_origin.status_code, 403)
                    invalid = await client.post(
                        "/api/bot/pause", json={"active": "true"}, headers=origin
                    )
                    self.assertEqual(invalid.status_code, 422)
                    paused = await client.post(
                        "/api/bot/pause", json={"active": True}, headers=origin
                    )
                    self.assertEqual(paused.json(), {
                        "safety_pause": "Active", "mode": grid_settings.PAUSED_MANUAL,
                    })
                    resumed = await client.post(
                        "/api/bot/pause", json={"active": False}, headers=origin
                    )
                    self.assertEqual(resumed.json(), {
                        "safety_pause": "Normal", "mode": None,
                    })
                    self.assertEqual(calls, [True, False])
            finally:
                grid_api.app.state.grid_bot = None

    async def test_prepare_reset_requires_admin_and_calls_bot_cleanup(self) -> None:
        cleanup = Mock(return_value={
            "safety_pause": "Active", "mode": grid_settings.PAUSED_RESET,
            "remaining_exchange_orders": 0, "bot_btc_held": 0.00024,
            "factory_reset_ready": False,
        })
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
        }):
            grid_api.app.state.grid_bot = SimpleNamespace(prepare_factory_reset=cleanup)
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="https://testserver",
                ) as client:
                    origin = {"Origin": "http://localhost:5173"}
                    denied = await client.post("/api/bot/prepare-reset", headers=origin)
                    self.assertEqual(denied.status_code, 401)
                    await client.post(
                        "/api/auth/login",
                        json={"password": "unique-private-admin-password"}, headers=origin,
                    )
                    foreign = await client.post(
                        "/api/bot/prepare-reset",
                        headers={"Origin": "https://other.test"},
                    )
                    self.assertEqual(foreign.status_code, 403)
                    self.assertEqual(cleanup.call_count, 0)
                    prepared = await client.post("/api/bot/prepare-reset", headers=origin)
                    self.assertEqual(prepared.status_code, 200)
                    self.assertEqual(prepared.json()["mode"], grid_settings.PAUSED_RESET)
                    self.assertEqual(prepared.headers["cache-control"], "no-store")
                    cleanup.assert_called_once_with()
            finally:
                grid_api.app.state.grid_bot = None

    async def test_master_engine_routes_require_admin_and_trusted_origin(self) -> None:
        bot = Mock()
        bot.start_engine.return_value = {"engine_status": grid_settings.ENGINE_RUNNING}
        bot.stop_engine.return_value = {
            "engine_status": grid_settings.ENGINE_IDLE,
            "bot_orders_cleared": True, "remaining_exchange_orders": 0,
        }
        grid_api.app.state.grid_bot = bot
        grid_api.app.state.ready = True
        try:
            with patch.dict(os.environ, {
                "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
                "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
            }):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="https://testserver",
                ) as client:
                    origin = {"Origin": "http://localhost:5173"}
                    denied = await client.post("/api/engine/start", headers=origin)
                    self.assertEqual(denied.status_code, 401)
                    await client.post(
                        "/api/auth/login",
                        json={"password": "unique-private-admin-password"}, headers=origin,
                    )
                    foreign = await client.post(
                        "/api/engine/stop", headers={"Origin": "https://other.test"},
                    )
                    self.assertEqual(foreign.status_code, 403)
                    bot.stop_engine.assert_not_called()
                    started = await client.post("/api/engine/start", headers=origin)
                    self.assertEqual(started.json(), {"engine_status": grid_settings.ENGINE_RUNNING})
                    self.assertEqual(started.headers["cache-control"], "no-store")
                    covered = await client.post(
                        "/api/engine/start", headers=origin,
                        json={"auto_cover_inventory": True,
                              "pending_grid_token": "a" * 64},
                    )
                    self.assertEqual(covered.status_code, 200)
                    bot.start_engine.side_effect = grid_models.TradingHalt(
                        "State reconciliation failed; manual intervention is required."
                    )
                    conflict = await client.post("/api/engine/start", headers=origin)
                    self.assertEqual(conflict.status_code, 409)
                    self.assertIn("manual intervention", conflict.json()["detail"])
                    stopped = await client.post("/api/engine/stop", headers=origin)
                    self.assertTrue(stopped.json()["bot_orders_cleared"])
                    self.assertEqual(bot.start_engine.call_count, 3)
                    self.assertEqual(
                        bot.start_engine.call_args_list[0].kwargs,
                        {"auto_cover_inventory": False,
                         "pending_grid_token": None},
                    )
                    self.assertEqual(
                        bot.start_engine.call_args_list[1].kwargs,
                        {"auto_cover_inventory": True,
                         "pending_grid_token": "a" * 64},
                    )
                    bot.start_engine.side_effect = grid_models.GridStopConflict(
                        Decimal("78957.33"), Decimal("79789.70"),
                    )
                    stop_conflict = await client.post("/api/engine/start", headers=origin)
                    self.assertEqual(stop_conflict.status_code, 409)
                    detail = stop_conflict.json()["detail"]
                    self.assertEqual(detail["code"], "GRID_STOP_CONFLICT")
                    self.assertEqual(detail["action"], "REANCHOR_GRID")
                    self.assertEqual(detail["lowest_buy_price"], "78957.33")
                    self.assertEqual(detail["hard_stop_price"], "79789.70")
                    self.assertIn("Save and review", detail["message"])
                    bot.stop_engine.assert_called_once_with()
        finally:
            grid_api.app.state.ready = False
            grid_api.app.state.grid_bot = None
            grid_api.app.state.standby = False

    async def test_hard_stop_endpoint_raises_floor_without_grid_reset(self) -> None:
        bot = Mock()
        bot.set_stop_loss.return_value = Decimal("79500")
        grid_api.app.state.grid_bot = bot
        try:
            with patch.dict(os.environ, {
                "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
                "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
            }):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="https://testserver",
                ) as client:
                    origin = {"Origin": "http://localhost:5173"}
                    denied = await client.post(
                        "/api/bot/stop-loss", json={"price": 79500}, headers=origin,
                    )
                    self.assertEqual(denied.status_code, 401)
                    await client.post(
                        "/api/auth/login", json={"password": "unique-private-admin-password"},
                        headers=origin,
                    )
                    foreign = await client.post(
                        "/api/bot/stop-loss", json={"price": 79500},
                        headers={"Origin": "https://other.test"},
                    )
                    self.assertEqual(foreign.status_code, 403)
                    invalid = await client.post(
                        "/api/bot/stop-loss", json={"price": -1}, headers=origin,
                    )
                    self.assertEqual(invalid.status_code, 422)
                    updated = await client.post(
                        "/api/bot/stop-loss", json={"price": 79500}, headers=origin,
                    )
                    self.assertEqual(updated.status_code, 200)
                    self.assertEqual(updated.json(), {
                        "current_hard_stop_loss": 79500.0, "grid_reset": False,
                    })
                    self.assertEqual(updated.headers["cache-control"], "no-store")
                    bot.set_stop_loss.assert_called_once_with("79500.0")
                    bot.request_grid_reset.assert_not_called()
                    bot.request_manual_recenter.assert_not_called()
        finally:
            grid_api.app.state.grid_bot = None

    async def test_factory_reset_requires_admin_cookie_and_trusted_origin(self) -> None:
        reset = Mock()
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
        }):
            grid_api.app.state.grid_bot = SimpleNamespace(factory_reset=reset)
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="https://testserver",
                ) as client:
                    origin = {"Origin": "http://localhost:5173"}
                    denied = await client.post("/api/admin/factory-reset", headers=origin)
                    self.assertEqual(denied.status_code, 401)
                    await client.post(
                        "/api/auth/login", json={"password": "unique-private-admin-password"},
                        headers=origin,
                    )
                    foreign = await client.post(
                        "/api/admin/factory-reset",
                        headers={"Origin": "https://other.test"},
                    )
                    self.assertEqual(foreign.status_code, 403)
                    reset.side_effect = grid_models.TradingHalt("Cannot reset: Active orders exist. Please pause the bot first.")
                    blocked = await client.post("/api/admin/factory-reset", headers=origin)
                    self.assertEqual(blocked.status_code, 400)
                    reset.side_effect = None
                    accepted = await client.post("/api/admin/factory-reset", headers=origin)
                    self.assertEqual(accepted.json(), {
                        "status": "reset", "trading_state": grid_settings.LIQUIDATED,
                    })
                    self.assertEqual(reset.call_count, 2)
            finally:
                grid_api.app.state.grid_bot = None

    async def test_recenter_insufficient_capital_returns_http_400(self) -> None:
        detail = (
            "Insufficient Capital: Grid requires 498.42 USDT for BUY limits, "
            "but only 100 USDT is available in the Spot wallet."
        )

        def reject_recenter(*_args):
            raise grid_models.InsufficientGridCapital(detail)

        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
        }):
            grid_api.app.state.grid_bot = SimpleNamespace(
                request_manual_recenter=reject_recenter
            )
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="https://testserver",
                ) as client:
                    origin = {"Origin": "http://localhost:5173"}
                    await client.post(
                        "/api/auth/login",
                        json={"password": "unique-private-admin-password"},
                        headers=origin,
                    )
                    response = await client.post(
                        "/api/bot/grid/recenter",
                        json={"center_price": 102, "half_width_percentage": 25},
                        headers=origin,
                    )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"detail": detail})
            finally:
                grid_api.app.state.grid_bot = None

    async def test_recenter_endpoint_authenticates_and_queues_only(self) -> None:
        calls = []
        bot = SimpleNamespace(request_manual_recenter=lambda *values: (
            calls.append(values) or ("76000", "88000")
        ))
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
        }):
            grid_api.app.state.grid_bot = bot
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="https://testserver",
                ) as client:
                    origin = {"Origin": "http://localhost:5173"}
                    payload = {"center_price": 82000.0,
                               "half_width_percentage": 15.0}
                    self.assertEqual((await client.post(
                        "/api/bot/grid/recenter", json=payload, headers=origin
                    )).status_code, 401)
                    await client.post(
                        "/api/auth/login", json={"password": "unique-private-admin-password"},
                        headers=origin,
                    )
                    self.assertEqual((await client.post(
                        "/api/bot/grid/recenter", json=payload,
                        headers={"Origin": "https://other.test"}
                    )).status_code, 403)
                    for bad in (
                        {"center_price": -1, "half_width_percentage": 15},
                        {"center_price": 82000, "half_width_percentage": 100},
                        {"center_price": 82000, "half_width_percentage": "nan"},
                    ):
                        self.assertEqual((await client.post(
                            "/api/bot/grid/recenter", json=bad, headers=origin
                        )).status_code, 422)
                    self.assertEqual(calls, [])
                    accepted = await client.post(
                        "/api/bot/grid/recenter", json=payload, headers=origin
                    )
                    self.assertEqual(accepted.status_code, 202)
                    self.assertEqual(accepted.json(), {
                        "status": "queued", "lower_bound": 76000.0,
                        "upper_bound": 88000.0,
                    })
                    self.assertEqual(calls, [("82000.0", "15.0")])
                    for bad in (
                        {"center_price": 82000, "width_percentage": 15},
                        {"center_price": 82000, "stop_loss_percentage": 25},
                        {"center_price": 82000, "width_percentage": 15,
                         "stop_loss_percentage": 25, "half_width_percentage": 15},
                        {"center_price": 82000, "width_percentage": 15,
                         "stop_loss_percentage": 25, "allocated_capital": 100},
                        {"center_price": 82000, "width_percentage": 15,
                         "stop_loss_percentage": 25, "grid_levels": 14},
                        {"center_price": 82000, "width_percentage": 15,
                         "stop_loss_percentage": 25, "allocated_capital": 90,
                         "grid_levels": 14},
                        {"center_price": 82000, "width_percentage": 15,
                         "stop_loss_percentage": 25, "allocated_capital": 1000,
                         "grid_levels": 14.5},
                    ):
                        self.assertEqual((await client.post(
                            "/api/bot/grid/recenter", json=bad, headers=origin
                        )).status_code, 422)
                    accepted_new = await client.post(
                        "/api/bot/grid/recenter",
                        json={"center_price": 82000, "width_percentage": 15,
                              "stop_loss_percentage": 25}, headers=origin,
                    )
                    self.assertEqual(accepted_new.status_code, 202)
                    self.assertEqual(calls[-1], ("82000.0", "15.0", "25.0"))
                    accepted_exact = await client.post(
                        "/api/bot/grid/recenter",
                        json={"center_price": 82000, "width_percentage": 15,
                              "stop_loss_percentage": 25,
                              "allocated_capital": 1000, "grid_levels": 14},
                        headers=origin,
                    )
                    self.assertEqual(accepted_exact.status_code, 202)
                    self.assertEqual(calls[-1], (
                        "82000.0", "15.0", "25.0", "1000.0", 14,
                    ))
            finally:
                grid_api.app.state.grid_bot = None

    async def test_recenter_endpoint_rejects_a_lower_active_trailing_floor(self) -> None:
        class ActiveBot:
            def __init__(self):
                self.database = SimpleNamespace(get_state=lambda _: None)
                self.queued = False

            def grid_configuration(self):
                return Decimal("70000"), Decimal("90000"), 6, Decimal("75000")

            def request_manual_recenter(self, *_args):
                self.queued = True
                return Decimal("70000"), Decimal("90000")

        bot = ActiveBot()
        with (patch.dict(os.environ, {
                "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
                "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
              }), patch.object(grid_api, "GridBot", ActiveBot)):
            grid_api.app.state.grid_bot = bot
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_api.app),
                    base_url="https://testserver",
                ) as client:
                    origin = {"Origin": "http://localhost:5173"}
                    await client.post(
                        "/api/auth/login",
                        json={"password": "unique-private-admin-password"},
                        headers=origin,
                    )
                    response = await client.post(
                        "/api/bot/grid/recenter",
                        json={"center_price": 82000, "width_percentage": 15,
                              "stop_loss_percentage": 20},
                        headers=origin,
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["detail"],
                                     grid_settings.RISK_OVERRIDE_DENIED)
                    self.assertFalse(bot.queued)
            finally:
                grid_api.app.state.grid_bot = None

    async def test_wallet_values_carry_and_partial_sell_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = GridDatabase(Path(directory) / "grid_testnet.sqlite")
            database.insert_order(
                "carry-1", 0, "BUY", "84473.58", "0.00591",
                order_type="MARKET",
            )
            database.mark_order_filled("carry-1")
            database.set_state(
                "carry_inventory", '{"order_id":"carry-1","cost":"499.2388578"}'
            )
            database.set_state(grid_settings.LAST_MARKET_PRICE_KEY, "85000")
            wallet = grid_portfolio._portfolio_wallet(database)
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
                grid_settings.FILL_SNAPSHOT_PREFIX + "sell-1",
                '{"filled_base":"0.001","filled_quote":"90",'
                '"base_fee":"0.0001","quote_fee":"0"}',
            )
            database.set_state(grid_settings.LAST_MARKET_PRICE_KEY, "80000")
            wallet = grid_portfolio._portfolio_wallet(database)
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
                grid_portfolio._portfolio_wallet(database),
                grid_portfolio._unavailable_wallet(),
            )
            database.set_state("halt_reason", grid_settings.MANUAL_ACCOUNT_EXIT_REASON)
            self.assertEqual(grid_portfolio._portfolio_wallet(database), {
                "btc_held": 0.0, "average_cost": None, "unrealized_pnl": 0.0,
            })

    async def test_api_and_trading_loop_run_concurrently(self) -> None:
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
            _runner_lease_lost = False
            database = Mock()

            def acquire_runner_lease(self):
                pass

            def renew_runner_lease(self):
                return True

            def release_runner_lease(self):
                pass

            def _reconcile_state(self):
                return {"resumable": False, "unhedged_btc": Decimal(0)}

            async def run(self, _notifier):
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
        self.assertIsNone(grid_api.app.state.grid_bot)

    async def test_api_bind_failure_does_not_stop_trading_loop(self) -> None:
        class FailingServer:
            def __init__(self, _config):
                self.should_exit = False

            async def serve(self):
                raise SystemExit(1)

        class FakeBot:
            ran = False
            _runner_lease_lost = False
            database = Mock()

            def acquire_runner_lease(self):
                pass

            def renew_runner_lease(self):
                return True

            def release_runner_lease(self):
                pass

            def _reconcile_state(self):
                return {"resumable": False, "unhedged_btc": Decimal(0)}

            async def run(self, _notifier):
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
        self.assertIsNone(grid_api.app.state.grid_bot)


class AccountExposureTests(unittest.TestCase):
    @staticmethod
    def make_bot(database, free="1", used="0"):
        exchange = Mock()
        exchange.market.return_value = {
            "base": "BTC", "quote": "USDT", "spot": True,
            "limits": {"amount": {"min": 0.001}, "cost": {"min": 10}},
        }
        exchange.fetch_balance.return_value = {"BTC": {"free": free, "used": used}}
        exchange.fetch_open_orders.return_value = []
        exchange.fetch_ticker.return_value = {"last": "100"}
        exchange.amount_to_precision.side_effect = lambda symbol, amount: str(
            Decimal(amount).quantize(Decimal("0.00000001"), rounding="ROUND_DOWN")
        )
        stop_requested = Mock()
        stop_requested.is_set.return_value = False
        return SimpleNamespace(
            exchange=exchange, exchange_lock=RLock(), database=database,
            config=SimpleNamespace(symbol="BTC/USDT", stop_loss_price=Decimal("70")),
            stop_controller=SimpleNamespace(stop_requested=stop_requested),
            _is_bot_order=lambda order, tracked: order.get("clientOrderId", "").startswith("gridbot-test"),
        )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = GridDatabase(Path(temporary.name) / "grid_testnet.sqlite")
        self.bot = self.make_bot(self.database)
        previous = grid_api.app.state.standby, grid_api.app.state.monitor_only
        grid_api.app.state.standby = False
        grid_api.app.state.monitor_only = False

        def restore():
            grid_api.app.state.standby, grid_api.app.state.monitor_only = previous
        self.addCleanup(restore)

    def payload(self, engine=grid_settings.ENGINE_IDLE):
        return grid_api._unprotected_exposure_payload(self.bot, self.database, engine)

    def test_live_remaining_sell_quantities_and_other_btc_pairs_are_subtracted(self):
        self.bot.exchange.fetch_balance.return_value = {"BTC": {"free": "1.4", "used": "0.6"}}
        self.bot.exchange.fetch_open_orders.return_value = [
            {"symbol": "BTC/USDT", "side": "sell", "type": "limit",
             "amount": "1", "filled": "0.6"},
            {"symbol": "BTC/USDC", "side": "sell", "type": "limit_maker", "remaining": "0.2"},
            {"symbol": "BTC/USDT", "side": "buy", "type": "limit", "remaining": "10"},
            {"symbol": "ETH/USDT", "side": "sell", "type": "limit", "remaining": "10"},
            {"symbol": "BTC/USDT", "side": "sell", "type": "market", "remaining": "10"},
            {"symbol": "BTC/USDT", "side": "sell", "type": "limit",
             "status": "closed", "remaining": "10"},
        ]
        # A stale ledger SELL must not reduce the exchange-derived exposure.
        self.database.insert_order("stale", -1, "SELL", "120", "100")
        self.assertEqual(self.payload(), {
            "unprotected_btc_exposure": 1.4, "has_unprotected_exposure": True,
        })

    def test_running_stops_protect_bot_lots_but_not_manual_btc(self):
        self.database.set_state("grid_run", "{}")
        self.database.insert_order("seed", 0, "BUY", "100", "1.5", order_type="MARKET")
        self.database.mark_order_filled("seed")
        self.database.set_state("carry_inventory", json.dumps({"order_id": "seed", "cost": "150"}))
        self.bot.exchange.fetch_balance.return_value = {"BTC": {"free": "1", "used": "0.5"}}
        self.bot.exchange.fetch_open_orders.return_value = [{
            "symbol": "BTC/USDT", "side": "sell", "type": "limit", "remaining": "0.5",
            "clientOrderId": "gridbot-test-sell",
        }]
        self.assertEqual(self.payload(grid_settings.ENGINE_RUNNING), {
            "unprotected_btc_exposure": 1.0, "has_unprotected_exposure": False,
        })
        self.assertTrue(self.payload(grid_settings.ENGINE_IDLE)["has_unprotected_exposure"])
        self.assertTrue(self.payload(grid_settings.ENGINE_HALTED)["has_unprotected_exposure"])
        self.bot.stop_controller.stop_requested.is_set.return_value = True
        self.assertTrue(self.payload(grid_settings.ENGINE_RUNNING)["has_unprotected_exposure"])
        self.bot.stop_controller.stop_requested.is_set.return_value = False
        self.bot.exchange.fetch_balance.return_value["BTC"]["free"] = "1.25"
        self.assertTrue(self.payload(grid_settings.ENGINE_RUNNING)["has_unprotected_exposure"])
        self.database.set_state(grid_settings.ENGINE_FAULT_KEY, "offline")
        self.assertTrue(self.payload(grid_settings.ENGINE_RUNNING)["has_unprotected_exposure"])

    def test_flat_and_fully_sell_locked_wallets_have_no_exposure(self):
        self.bot.exchange.fetch_balance.return_value = {"BTC": {"free": "0", "used": "0"}}
        self.assertEqual(self.payload(), {"unprotected_btc_exposure": 0.0,
                                          "has_unprotected_exposure": False})
        self.bot.exchange.fetch_balance.return_value["BTC"]["used"] = "1"
        self.bot.exchange.fetch_open_orders.return_value = [{
            "symbol": "BTC/USDT", "side": "sell", "type": "limit", "remaining": "1",
        }]
        self.assertEqual(self.payload(), {"unprotected_btc_exposure": 0.0,
                                          "has_unprotected_exposure": False})
        self.bot.exchange.fetch_open_orders.return_value[0]["remaining"] = "2"
        self.assertEqual(self.payload()["unprotected_btc_exposure"], 0.0)

    def test_dust_and_lot_steps_are_applied_without_a_single_order_maximum_cap(self):
        market = self.bot.exchange.market.return_value
        market["info"] = {"filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001", "maxQty": "0.05"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "10", "applyToMarket": True},
        ]}
        self.bot.exchange.fetch_ticker.return_value = {"last": "80000"}
        self.bot.exchange.fetch_balance.return_value["BTC"]["free"] = "0.00000001"
        self.assertEqual(self.payload()["unprotected_btc_exposure"], 0.0)
        self.bot.exchange.fetch_balance.return_value["BTC"]["free"] = "0.12345678"
        self.assertEqual(self.payload()["unprotected_btc_exposure"], 0.123)
        self.bot.exchange.fetch_balance.return_value["BTC"]["free"] = "2"
        self.assertEqual(self.payload()["unprotected_btc_exposure"], 2.0)

    def test_notional_rules_that_exclude_market_sells_do_not_hide_tradable_btc(self):
        market = self.bot.exchange.market.return_value
        market["limits"]["amount"]["min"] = 0.00001
        market["info"] = {"filters": [
            {"filterType": "MIN_NOTIONAL", "minNotional": "10", "applyToMarket": False},
        ]}
        self.bot.exchange.fetch_ticker.return_value = {"last": "80000"}
        self.bot.exchange.fetch_balance.return_value["BTC"]["free"] = "0.00005"
        self.assertEqual(self.payload()["unprotected_btc_exposure"], 0.00005)

    def test_exchange_failures_and_missing_balances_are_unavailable_not_zero(self):
        self.bot.exchange.fetch_balance.side_effect = ccxt.RequestTimeout("offline")
        unavailable = {"unprotected_btc_exposure": None, "has_unprotected_exposure": False}
        self.assertEqual(self.payload(), unavailable)
        self.bot.exchange.fetch_balance.side_effect = None
        for balance in ({"free": "1"}, {"free": "NaN", "used": "0"},
                        {"free": "-1", "used": "0"}):
            self.bot.exchange.fetch_balance.return_value = {"BTC": balance}
            self.assertEqual(self.payload(), unavailable)

    def test_monitor_only_can_report_account_exposure_without_a_grid_database(self):
        grid_api.app.state.monitor_only = True
        with patch.object(grid_api, "_credentials"), patch.object(
            grid_api, "create_exchange", return_value=self.bot.exchange,
        ):
            self.assertEqual(grid_api._unprotected_exposure_payload(None, None, grid_settings.ENGINE_IDLE), {
                "unprotected_btc_exposure": 1.0, "has_unprotected_exposure": True,
            })


if __name__ == "__main__":
    unittest.main()
