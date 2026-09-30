import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ccxt
import httpx
import jwt
from fastapi import Depends, FastAPI

import main as grid_main
from database import GridDatabase


class StatusApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_order_ledger_requires_admin_and_uses_saved_fill_price(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = GridDatabase(Path(directory) / "grid.sqlite3")
            database.insert_order("buy-open", 1, "BUY", "82000", "0.02",
                                  client_order_id="gridbot-open")
            database.insert_order("sell-filled", -1, "SELL", "88000", "0.01",
                                  client_order_id="gridbot-filled")
            database.mark_order_filled("sell-filled")
            database.set_state(grid_main.FILL_SNAPSHOT_PREFIX + "sell-filled",
                               '{"filled_base":"0.009","filled_quote":"792",'
                               '"base_fee":"0","quote_fee":"0"}')
            grid_main.app.state.grid_bot = SimpleNamespace(database=database)
            try:
                with patch.dict(os.environ, {
                    "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
                    "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
                }):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=grid_main.app),
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
                grid_main.app.state.grid_bot = None

    async def test_standby_reports_idle_and_rejects_trading_controls(self) -> None:
        standby_bot = Mock()
        standby_bot.config.symbol = "BTC/USDT"
        standby_bot.request_initial_grid.return_value = (Decimal("80"), Decimal("120"))
        grid_main.app.state.grid_bot = standby_bot
        grid_main.app.state.standby = True
        try:
            with patch.dict(os.environ, {
                "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
                "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
            }), patch.object(grid_main, "GridDatabase", side_effect=AssertionError("DB opened")):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
                    base_url="https://testserver",
                ) as client:
                    status_response = await client.get("/api/bot/status")
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
            grid_main.app.state.standby = False
            grid_main.app.state.grid_bot = None

    async def test_monitor_only_status_never_opens_database_or_enables_trading(self) -> None:
        grid_main.app.state.grid_bot = None
        grid_main.app.state.monitor_only = True
        try:
            with patch.object(grid_main, "GridDatabase", side_effect=AssertionError("DB opened")):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
                    base_url="http://testserver",
                ) as client:
                    response = await client.get("/api/bot/status")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "Online")
            self.assertEqual(response.json()["trading_state"], "STOPPED")
            self.assertEqual(response.json()["grid_levels"], 0)
            self.assertFalse(response.json()["exact_grid_recenter_supported"])
        finally:
            grid_main.app.state.monitor_only = False

    async def test_monitor_only_wallet_is_read_only_and_controls_remain_unavailable(self) -> None:
        exchange = Mock()
        exchange.fetch_balance.return_value = {"USDT": {"free": "9403.45"}}
        grid_main.app.state.grid_bot = None
        grid_main.app.state.monitor_only = True
        try:
            with patch.dict(os.environ, {
                "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
                "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
            }), patch.object(grid_main, "create_exchange", return_value=exchange):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
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
            grid_main.app.state.monitor_only = False

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
                    "safety_pause": "Normal", "pause_mode": None,
                    "trading_state": "IDLE", "engine_status": "IDLE",
                    "engine_fault": None, "has_grid_run": False,
                    "grid_levels": 0,
                    "exact_grid_recenter_supported": True,
                    "lower_bound": None, "upper_bound": None,
                    "atr_value": None, "atr_percentage": None,
                    "bid_volume": None, "ask_volume": None,
                    "imbalance_ratio": None,
                    "current_hard_stop_loss": None,
                    "high_water_mark": None,
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
                database.set_state(grid_main.TRAILING_STOP_KEY,
                                   '{"high_water_mark":"85000",'
                                   '"stop_loss_distance":"15000"}')
                grid_main.app.state.grid_bot = SimpleNamespace(
                    config=SimpleNamespace(symbol="BTC/USDT",
                                           stop_loss_price=70000), database=database
                )
                grid_main.app.state.atr_snapshot = {
                    "atr_value": 850.5, "atr_percentage": 1.0,
                }
                grid_main.app.state.order_book_snapshot = {
                    "bid_volume": 15.2, "ask_volume": 4.1,
                    "imbalance_ratio": 15.2 / 4.1,
                }
                try:
                    response = await client.get(
                        "/api/bot/status",
                        headers={"Origin": "http://localhost:5173"},
                    )
                    database.clear_state(grid_main.SAFETY_MODE_KEY)
                    resumed = await client.get("/api/bot/status")
                    database.set_state(grid_main.SAFETY_MODE_KEY, grid_main.LIQUIDATED)
                    database.set_state(grid_main.LIQUIDATION_KEY,
                                       '{"phase":"complete","residual_base":"0.0001"}')
                    liquidated = await client.get("/api/bot/status")
                finally:
                    grid_main.app.state.grid_bot = None
                    grid_main.app.state.atr_snapshot = None
                    grid_main.app.state.order_book_snapshot = None

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "http://localhost:5173",
        )
        self.assertEqual(response.json(), {
            "status": "Online", "pair": "BTC/USDT",
            "safety_pause": "Active", "pause_mode": grid_main.PAUSED_DOWNSIDE,
            "trading_state": "ACTIVE", "engine_status": "RUNNING",
            "engine_fault": None, "has_grid_run": False,
            "grid_levels": 2,
            "exact_grid_recenter_supported": True,
            "lower_bound": 81000.0, "upper_bound": 90000.0,
            "atr_value": 850.5, "atr_percentage": 1.0,
            "bid_volume": 15.2, "ask_volume": 4.1,
            "imbalance_ratio": 15.2 / 4.1,
            "current_hard_stop_loss": 70000.0,
            "high_water_mark": 85000.0,
            "wallet": {
                "btc_held": 0.02, "average_cost": 81000.0,
                "unrealized_pnl": 80.0,
            },
        })
        self.assertEqual(resumed.json()["safety_pause"], "Normal")
        self.assertEqual(liquidated.json()["trading_state"], grid_main.LIQUIDATED)
        self.assertEqual(liquidated.json()["wallet"]["btc_held"], 0.0001)

    async def test_hourly_atr_uses_15_candles_and_handles_missing_data(self) -> None:
        candles = [
            [index * 3_600_000, 100, 110, 90, 100, 1]
            for index in range(15)
        ]
        snapshot = grid_main._calculate_atr_snapshot(candles)
        self.assertIsNotNone(snapshot)
        self.assertAlmostEqual(snapshot["atr_value"], 20)
        self.assertAlmostEqual(snapshot["atr_percentage"], 20)
        self.assertIsNone(grid_main._calculate_atr_snapshot(candles[:14]))
        candles[-1][4] = None
        self.assertIsNone(grid_main._calculate_atr_snapshot(candles))

    async def test_order_book_uses_top_50_levels_and_rejects_empty_asks(self) -> None:
        book = {
            "bids": [[100 - index, 0.1] for index in range(50)] + [[49, 100]],
            "asks": [[101 + index, 0.2] for index in range(50)] + [[151, 100]],
        }
        snapshot = grid_main._calculate_order_book_snapshot(book)
        self.assertIsNotNone(snapshot)
        self.assertAlmostEqual(snapshot["bid_volume"], 5)
        self.assertAlmostEqual(snapshot["ask_volume"], 10)
        self.assertAlmostEqual(snapshot["imbalance_ratio"], 0.5)
        self.assertIsNone(grid_main._calculate_order_book_snapshot({
            "bids": book["bids"], "asks": [],
        }))
        self.assertIsNone(grid_main._calculate_order_book_snapshot({
            "bids": [[100, float("nan")]], "asks": book["asks"],
        }))

    async def test_order_book_timeout_does_not_clear_atr(self) -> None:
        grid_main.app.state.atr_snapshot = None
        grid_main.app.state.order_book_snapshot = {"bid_volume": 1}
        try:
            with (
                patch.object(grid_main, "_fetch_atr_snapshot", return_value={
                    "atr_value": 20, "atr_percentage": 1,
                }),
                patch.object(grid_main, "_fetch_order_book_snapshot",
                             side_effect=TimeoutError),
                patch.object(grid_main.asyncio, "sleep",
                             side_effect=asyncio.CancelledError),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await grid_main._refresh_market_data(object())
            self.assertEqual(grid_main.app.state.atr_snapshot["atr_value"], 20)
            self.assertIsNone(grid_main.app.state.order_book_snapshot)
        finally:
            grid_main.app.state.atr_snapshot = None
            grid_main.app.state.order_book_snapshot = None

    async def test_admin_login_uses_strict_httponly_cookie(self) -> None:
        protected_app = FastAPI()

        @protected_app.get("/protected")
        def protected(user: str = Depends(grid_main.get_current_user)) -> dict:
            return {"user": user}

        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "",
            "BOT_JWT_SECRET": "",
            "BOT_AUTH_MOCK_ENABLED": "1",
        }):
            grid_main.app.state.grid_bot = None
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
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
                token = auth_client.cookies[grid_main.AUTH_COOKIE_NAME]
                self.assertEqual((await protected_client.get("/protected")).status_code, 401)
                self.assertEqual(
                    (await protected_client.get(
                        "/protected", cookies={grid_main.AUTH_COOKIE_NAME: "invalid"}
                    )).status_code, 401,
                )
                authorized = await protected_client.get(
                    "/protected", cookies={grid_main.AUTH_COOKIE_NAME: token}
                )
                self.assertEqual(authorized.json(), {"user": "admin"})
                expired = jwt.encode({
                    "sub": "admin", "iss": grid_main.JWT_ISSUER,
                    "aud": grid_main.JWT_AUDIENCE,
                    "iat": datetime.now(timezone.utc) - timedelta(hours=1),
                    "nbf": datetime.now(timezone.utc) - timedelta(hours=1),
                    "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
                }, grid_main.app.state.preview_jwt_secret, algorithm="HS256")
                self.assertEqual(
                    (await protected_client.get(
                        "/protected", cookies={grid_main.AUTH_COOKIE_NAME: expired}
                    )).status_code, 401,
                )
                forged = jwt.encode({
                    "sub": "admin", "iss": grid_main.JWT_ISSUER,
                    "aud": grid_main.JWT_AUDIENCE,
                    "iat": datetime.now(timezone.utc),
                    "nbf": datetime.now(timezone.utc),
                    "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
                }, "a-different-signing-secret-with-enough-length",
                    algorithm="HS256")
                self.assertEqual(
                    (await protected_client.get(
                        "/protected", cookies={grid_main.AUTH_COOKIE_NAME: forged}
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
                grid_main._configured_frontend_origin(),
                "https://dashboard.example.test",
            )
        for invalid in ("*", "https://*.example.test", "https://example.test/path",
                        "https://example.test,https://other.test", "ftp://example.test",
                        "http://example.test"):
            with patch.dict(os.environ, {"FRONTEND_URL": invalid}):
                with self.assertRaises(ValueError):
                    grid_main._configured_frontend_origin()
        environment = dict(os.environ, FRONTEND_URL="https://dashboard.example.test")
        loaded = subprocess.run(
            [sys.executable, "-c", "import main; print(main.FRONTEND_ORIGIN); "
             "print(main.app.user_middleware[0].kwargs['allow_origins'])"],
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
            grid_main.app.state.grid_bot = None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=grid_main.app),
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
                        "sub": "admin", "iss": grid_main.JWT_ISSUER,
                        "aud": grid_main.JWT_AUDIENCE,
                        "iat": now - timedelta(minutes=15),
                        "nbf": now - timedelta(minutes=15),
                        "exp": now - timedelta(seconds=age_seconds),
                    }, grid_main.app.state.preview_jwt_secret, algorithm="HS256")
                client.cookies.set(grid_main.AUTH_COOKIE_NAME, signed_cookie(30))
                self.assertEqual((await client.get("/api/auth/me")).status_code, 401)
                self.assertEqual((await client.post(
                    "/api/auth/refresh", headers=origin
                )).status_code, 200)
                client.cookies.set(grid_main.AUTH_COOKIE_NAME, signed_cookie(120))
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
            grid_main.app.state.grid_bot = object()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
                    base_url="https://testserver",
                ) as client:
                    response = await client.post(
                        "/api/auth/login", json={"password": "admin123"},
                        headers={"Origin": "http://localhost:5173"},
                    )
                self.assertEqual(response.status_code, 503)
            finally:
                grid_main.app.state.grid_bot = None

    async def test_live_bot_uses_private_admin_configuration(self) -> None:
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
            "BOT_AUTH_MOCK_ENABLED": "1",
        }):
            grid_main.app.state.grid_bot = object()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
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
                grid_main.app.state.grid_bot = None

    async def test_wallet_balance_requires_admin_and_handles_exchange_timeout(self) -> None:
        balance_reader = Mock(return_value=Decimal("123.4567"))
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
        }):
            grid_main.app.state.grid_bot = SimpleNamespace(_free_balance=balance_reader)
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
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

                    grid_main.app.state.grid_bot = None
                    offline = await client.get("/api/wallet-balance")
                    self.assertEqual(offline.status_code, 503)
            finally:
                grid_main.app.state.grid_bot = None

    async def test_pause_endpoint_requires_cookie_and_trusted_origin(self) -> None:
        calls = []
        bot = SimpleNamespace(set_manual_pause=lambda active: (
            calls.append(active) or (grid_main.PAUSED_MANUAL if active else None)
        ))
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
        }):
            grid_main.app.state.grid_bot = bot
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
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
                        "safety_pause": "Active", "mode": grid_main.PAUSED_MANUAL,
                    })
                    resumed = await client.post(
                        "/api/bot/pause", json={"active": False}, headers=origin
                    )
                    self.assertEqual(resumed.json(), {
                        "safety_pause": "Normal", "mode": None,
                    })
                    self.assertEqual(calls, [True, False])
            finally:
                grid_main.app.state.grid_bot = None

    async def test_prepare_reset_requires_admin_and_calls_bot_cleanup(self) -> None:
        cleanup = Mock(return_value={
            "safety_pause": "Active", "mode": grid_main.PAUSED_RESET,
            "remaining_exchange_orders": 0, "bot_btc_held": 0.00024,
            "factory_reset_ready": False,
        })
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
        }):
            grid_main.app.state.grid_bot = SimpleNamespace(prepare_factory_reset=cleanup)
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
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
                    self.assertEqual(prepared.json()["mode"], grid_main.PAUSED_RESET)
                    self.assertEqual(prepared.headers["cache-control"], "no-store")
                    cleanup.assert_called_once_with()
            finally:
                grid_main.app.state.grid_bot = None

    async def test_master_engine_routes_require_admin_and_trusted_origin(self) -> None:
        bot = Mock()
        bot.start_engine.return_value = {"engine_status": grid_main.ENGINE_RUNNING}
        bot.stop_engine.return_value = {
            "engine_status": grid_main.ENGINE_IDLE,
            "bot_orders_cleared": True, "remaining_exchange_orders": 0,
        }
        grid_main.app.state.grid_bot = bot
        grid_main.app.state.ready = True
        try:
            with patch.dict(os.environ, {
                "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
                "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
            }):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
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
                    self.assertEqual(started.json(), {"engine_status": grid_main.ENGINE_RUNNING})
                    self.assertEqual(started.headers["cache-control"], "no-store")
                    stopped = await client.post("/api/engine/stop", headers=origin)
                    self.assertTrue(stopped.json()["bot_orders_cleared"])
                    bot.start_engine.assert_called_once_with()
                    bot.stop_engine.assert_called_once_with()
        finally:
            grid_main.app.state.ready = False
            grid_main.app.state.grid_bot = None
            grid_main.app.state.standby = False

    async def test_hard_stop_endpoint_raises_floor_without_grid_reset(self) -> None:
        bot = Mock()
        bot.set_stop_loss.return_value = Decimal("79500")
        grid_main.app.state.grid_bot = bot
        try:
            with patch.dict(os.environ, {
                "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
                "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
            }):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
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
            grid_main.app.state.grid_bot = None

    async def test_factory_reset_requires_admin_cookie_and_trusted_origin(self) -> None:
        reset = Mock()
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
        }):
            grid_main.app.state.grid_bot = SimpleNamespace(factory_reset=reset)
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
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
                    reset.side_effect = grid_main.TradingHalt("Cannot reset: Active orders exist. Please pause the bot first.")
                    blocked = await client.post("/api/admin/factory-reset", headers=origin)
                    self.assertEqual(blocked.status_code, 400)
                    reset.side_effect = None
                    accepted = await client.post("/api/admin/factory-reset", headers=origin)
                    self.assertEqual(accepted.json(), {
                        "status": "reset", "trading_state": grid_main.LIQUIDATED,
                    })
                    self.assertEqual(reset.call_count, 2)
            finally:
                grid_main.app.state.grid_bot = None

    async def test_recenter_insufficient_capital_returns_http_400(self) -> None:
        detail = (
            "Insufficient Capital: Grid requires 498.42 USDT for BUY limits, "
            "but only 100 USDT is available in the Spot wallet."
        )

        def reject_recenter(*_args):
            raise grid_main.InsufficientGridCapital(detail)

        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
        }):
            grid_main.app.state.grid_bot = SimpleNamespace(
                request_manual_recenter=reject_recenter
            )
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
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
                grid_main.app.state.grid_bot = None

    async def test_recenter_endpoint_authenticates_and_queues_only(self) -> None:
        calls = []
        bot = SimpleNamespace(request_manual_recenter=lambda *values: (
            calls.append(values) or ("76000", "88000")
        ))
        with patch.dict(os.environ, {
            "BOT_ADMIN_PASSWORD": "unique-private-admin-password",
            "BOT_JWT_SECRET": "a-random-private-signing-secret-32-chars",
        }):
            grid_main.app.state.grid_bot = bot
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
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
                grid_main.app.state.grid_bot = None

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
              }), patch.object(grid_main, "GridBot", ActiveBot)):
            grid_main.app.state.grid_bot = bot
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=grid_main.app),
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
                                     grid_main.RISK_OVERRIDE_DENIED)
                    self.assertFalse(bot.queued)
            finally:
                grid_main.app.state.grid_bot = None

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
        self.assertIsNone(grid_main.app.state.grid_bot)

    async def test_api_bind_failure_does_not_stop_trading_loop(self) -> None:
        class FailingServer:
            def __init__(self, _config):
                self.should_exit = False

            async def serve(self):
                raise SystemExit(1)

        class FakeBot:
            ran = False

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
        self.assertIsNone(grid_main.app.state.grid_bot)


if __name__ == "__main__":
    unittest.main()
