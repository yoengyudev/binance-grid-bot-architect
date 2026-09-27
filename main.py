"""Phase 4: Binance Spot Testnet geometric grid bot."""

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import ccxt
import jwt
import pandas as pd
import pandas_ta
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, FiniteFloat, StrictBool, StrictInt

from database import GridDatabase
from exchange_handler import config_path, create_exchange, load_config
from stop_controller import StopController
from telegram_bot import TelegramNotifier, load_telegram_credentials


BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)
MAX_LEVELS = 50
ORDER_CLIENT_PREFIX = "gridbot"
ORDER_CLIENT_PREFIX_KEY = "order_client_prefix"
SELL_AMOUNT_BUFFER = Decimal("0.002")
RESET_SPACING_PERCENT = Decimal("2.5")
SAFETY_MODE_KEY = "safety_mode"
PAUSED_DOWNSIDE = "PAUSED_DOWNSIDE"
PAUSED_MANUAL = "PAUSED_MANUAL"
LIQUIDATING = "LIQUIDATING"
LIQUIDATED = "LIQUIDATED"
LIQUIDATION_HALTED = "HALTED"
LIQUIDATION_KEY = "hard_stop_liquidation"
RISK_OVERRIDE_DENIED = (
    "Risk Override Denied: The requested hard stop is lower than the active "
    "trailing floor. The stop loss can only move up."
)
LIQUIDATION_NOTICE_KEY = "hard_stop_notification_pending"
SAFETY_PAUSE_NOTICE_KEY = "safety_pause_notice_pending"
SAFETY_RECOVERY_NOTICE_KEY = "safety_recovery_notice_pending"
SAFETY_RESUME_NOTICE_KEY = "safety_resume_notice_pending"
LAST_MARKET_PRICE_KEY = "last_market_price"
TRAILING_STOP_KEY = "trailing_stop"
FILL_SNAPSHOT_PREFIX = "fill_snapshot:"
BREAKOUT_TIMER_KEY = "upper_breakout_timer"
BREAKOUT_WIDTH_KEY = "breakout_width_percent"
BREAKOUT_NOTICE_KEY = "breakout_notification_pending"
BREAKOUT_COOLDOWN_SECONDS = 4 * 60 * 60
ATR_REFRESH_SECONDS = 60
ORDER_BOOK_REFRESH_SECONDS = 10
MOCK_ADMIN_PASSWORD = "admin123"
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "grid-bot"
JWT_AUDIENCE = "grid-dashboard"
JWT_LIFETIME_SECONDS = 15 * 60
JWT_REFRESH_GRACE_SECONDS = 60
AUTH_COOKIE_NAME = "__Host-grid_admin_session"
load_dotenv(dotenv_path=BASE_DIR / ".env")


def _configured_frontend_origin() -> str:
    value = os.getenv("FRONTEND_URL", "http://localhost:5173").strip()
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or
            parsed.username or parsed.password or parsed.path not in ("", "/") or
            parsed.query or parsed.fragment or "*" in value):
        raise ValueError("FRONTEND_URL must be one exact HTTP(S) origin.")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("FRONTEND_URL has an invalid port.") from error
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ValueError("A non-local FRONTEND_URL must use HTTPS.")
    return value.rstrip("/")


FRONTEND_ORIGIN = _configured_frontend_origin()
OpenOrderEntry = Tuple[Optional[Decimal], Optional[Decimal]]

app = FastAPI(title="Grid Bot Status API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.state.grid_bot = None
app.state.atr_snapshot = None
app.state.order_book_snapshot = None
app.state.preview_jwt_secret = secrets.token_urlsafe(48)


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class PauseRequest(BaseModel):
    active: StrictBool


class RecenterRequest(BaseModel):
    center_price: FiniteFloat = Field(gt=0)
    width_percentage: Optional[FiniteFloat] = Field(default=None, gt=0, lt=100)
    stop_loss_percentage: Optional[FiniteFloat] = Field(default=None, gt=0, lt=100)
    half_width_percentage: Optional[FiniteFloat] = Field(default=None, gt=0, lt=100)
    allocated_capital: Optional[FiniteFloat] = Field(default=None, gt=0)
    grid_levels: Optional[StrictInt] = Field(default=None, ge=2, le=2 * MAX_LEVELS)


def _require_dashboard_origin(request: Request) -> None:
    """Cookie-authenticated writes must come from the trusted dashboard origin."""
    if request.headers.get("origin") != FRONTEND_ORIGIN:
        raise HTTPException(status_code=403, detail="Untrusted dashboard origin.")


def _auth_configuration() -> Tuple[str, str]:
    """Require private credentials for a live bot; allow explicit mock previews."""
    password = os.getenv("BOT_ADMIN_PASSWORD")
    secret = os.getenv("BOT_JWT_SECRET")
    if password and secret and len(secret) >= 32:
        return password, secret
    if os.getenv("BOT_AUTH_MOCK_ENABLED") == "1" and app.state.grid_bot is None:
        return MOCK_ADMIN_PASSWORD, app.state.preview_jwt_secret
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Admin authentication is not configured.",
    )


def _issue_admin_cookie(response: Response, secret: str) -> None:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": "admin",
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(seconds=JWT_LIFETIME_SECONDS),
        },
        secret,
        algorithm=JWT_ALGORITHM,
    )
    response.set_cookie(
        key=AUTH_COOKIE_NAME, value=token,
        max_age=JWT_LIFETIME_SECONDS + JWT_REFRESH_GRACE_SECONDS,
        path="/", secure=True, httponly=True, samesite="strict",
    )


@app.post("/api/auth/login")
def login(request: LoginRequest, response: Response,
          _: None = Depends(_require_dashboard_origin)) -> Dict[str, Any]:
    password, secret = _auth_configuration()
    if not hmac.compare_digest(request.password, password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
        )
    _issue_admin_cookie(response, secret)
    return {"authenticated": True, "expires_in": JWT_LIFETIME_SECONDS}


def _decode_admin_cookie(request: Request, *, refresh: bool = False) -> str:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token.",
    )
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        raise unauthorized
    try:
        _, secret = _auth_configuration()
        claims = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            leeway=JWT_REFRESH_GRACE_SECONDS if refresh else 0,
            options={"require": ["sub", "iss", "aud", "iat", "nbf", "exp"]},
        )
    except (jwt.InvalidTokenError, HTTPException):
        raise unauthorized from None
    if claims.get("sub") != "admin":
        raise unauthorized
    return "admin"


def get_current_user(request: Request) -> str:
    """Require a currently valid admin JWT for control endpoints."""
    return _decode_admin_cookie(request)


@app.post("/api/auth/refresh")
def refresh_session(request: Request, response: Response,
                    _: None = Depends(_require_dashboard_origin)) -> Dict[str, Any]:
    _decode_admin_cookie(request, refresh=True)
    _, secret = _auth_configuration()
    _issue_admin_cookie(response, secret)
    return {"authenticated": True, "expires_in": JWT_LIFETIME_SECONDS}


@app.get("/api/auth/me")
def auth_me(_: str = Depends(get_current_user)) -> Dict[str, bool]:
    return {"authenticated": True}


@app.post("/api/auth/logout")
def logout(response: Response, _: None = Depends(_require_dashboard_origin)) -> Dict[str, bool]:
    response.delete_cookie(
        key=AUTH_COOKIE_NAME, path="/", secure=True,
        httponly=True, samesite="strict",
    )
    return {"authenticated": False}


def _unavailable_wallet() -> Dict[str, Optional[float]]:
    return {"btc_held": None, "average_cost": None, "unrealized_pnl": None}


def _portfolio_wallet(database: GridDatabase) -> Dict[str, Optional[float]]:
    """Value the current run's remaining BTC lots from persisted fill snapshots."""
    try:
        liquidation = database.get_state(LIQUIDATION_KEY)
        if liquidation:
            state = json.loads(liquidation)
            if state.get("phase") == "complete":
                return {"btc_held": float(state.get("residual_base", "0")),
                        "average_cost": None, "unrealized_pnl": None}
            return _unavailable_wallet()
        carry_text = database.get_state("carry_inventory")
        carry = json.loads(carry_text) if carry_text else {}
        rows = database.fetch_all_orders()
        buys: Dict[str, Tuple[Decimal, Decimal]] = {}
        sold_by_buy: Dict[str, Decimal] = {}
        for row in rows:
            order_id = row["order_id"]
            if order_id == carry.get("order_id"):
                filled = Decimal(row["amount"])
                quote_cost = Decimal(carry["cost"])
                base_fee = quote_fee = Decimal(0)
            else:
                snapshot_text = database.get_state(FILL_SNAPSHOT_PREFIX + order_id)
                if snapshot_text:
                    snapshot = json.loads(snapshot_text)
                    filled = Decimal(snapshot["filled_base"])
                    quote_cost = Decimal(snapshot["filled_quote"])
                    base_fee = Decimal(snapshot["base_fee"])
                    quote_fee = Decimal(snapshot["quote_fee"])
                elif row["status"] == "OPEN":
                    filled = quote_cost = base_fee = quote_fee = Decimal(0)
                else:
                    # A historical fill cannot be valued from its limit price.
                    return _unavailable_wallet()
            if any(not value.is_finite() or value < 0 for value in
                   (filled, quote_cost, base_fee, quote_fee)):
                return _unavailable_wallet()
            if row["side"] == "BUY":
                acquired = filled - base_fee
                spent = quote_cost + quote_fee
                if acquired < 0 or (acquired > 0 and spent <= 0):
                    return _unavailable_wallet()
                buys[order_id] = (acquired, spent)
            elif filled > 0:
                parent_id = row["parent_order_id"]
                if not parent_id:
                    return _unavailable_wallet()
                sold_by_buy[parent_id] = (
                    sold_by_buy.get(parent_id, Decimal(0)) + filled + base_fee
                )

        held = remaining_cost = Decimal(0)
        for order_id, (acquired, spent) in buys.items():
            sold = sold_by_buy.pop(order_id, Decimal(0))
            if sold > acquired:
                return _unavailable_wallet()
            remaining = acquired - sold
            if remaining > 0:
                held += remaining
                remaining_cost += spent * remaining / acquired
        if sold_by_buy:
            return _unavailable_wallet()
        if held == 0:
            return {"btc_held": 0.0, "average_cost": None, "unrealized_pnl": 0.0}

        price_text = database.get_state(LAST_MARKET_PRICE_KEY)
        price = Decimal(price_text) if price_text else None
        if price is not None and (not price.is_finite() or price <= 0):
            price = None
        return {
            "btc_held": float(held),
            "average_cost": float(remaining_cost / held),
            "unrealized_pnl": float(held * price - remaining_cost) if price else None,
        }
    except (KeyError, TypeError, ValueError, InvalidOperation, ZeroDivisionError):
        LOGGER.warning("Portfolio exposure is unavailable from saved fill data.")
        return _unavailable_wallet()


def _calculate_atr_snapshot(candles: List[List[float]]) -> Optional[Dict[str, float]]:
    """Calculate 14-period ATR from 15 hourly OHLCV candles."""
    if len(candles) < 15:
        return None
    frame = pd.DataFrame(
        candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    for column in ("high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame[["high", "low", "close"]].isna().any().any():
        return None
    close = float(frame["close"].iloc[-1])
    if not math.isfinite(close) or close <= 0:
        return None
    values = pandas_ta.atr(
        high=frame["high"], low=frame["low"], close=frame["close"], length=14
    )
    if values is None or values.empty:
        return None
    atr_value = float(values.iloc[-1])
    if not math.isfinite(atr_value) or atr_value < 0:
        return None
    return {"atr_value": atr_value, "atr_percentage": atr_value / close * 100}


def _fetch_atr_snapshot(bot: "GridBot") -> Optional[Dict[str, float]]:
    with bot.exchange_lock:
        candles = bot.exchange.fetch_ohlcv(
            bot.config.symbol, timeframe="1h", limit=15
        )
    return _calculate_atr_snapshot(candles)


def _calculate_order_book_snapshot(book: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Sum BTC depth at the first 50 price levels on each side."""
    def total_volume(levels: Any) -> float:
        if not isinstance(levels, list) or not levels:
            raise ValueError("Order book side is empty or invalid.")
        amounts = []
        for level in levels[:50]:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                raise ValueError("Order book level is invalid.")
            amount = float(level[1])
            if not math.isfinite(amount) or amount < 0:
                raise ValueError("Order book amount is invalid.")
            amounts.append(amount)
        return math.fsum(amounts)

    try:
        bid_volume = total_volume(book["bids"])
        ask_volume = total_volume(book["asks"])
        if bid_volume <= 0 or ask_volume <= 0:
            return None
        ratio = bid_volume / ask_volume
        if not math.isfinite(ratio):
            return None
        return {
            "bid_volume": bid_volume,
            "ask_volume": ask_volume,
            "imbalance_ratio": ratio,
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _fetch_order_book_snapshot(bot: "GridBot") -> Optional[Dict[str, float]]:
    with bot.exchange_lock:
        book = bot.exchange.fetch_order_book(bot.config.symbol, limit=50)
    return _calculate_order_book_snapshot(book)


async def _refresh_market_data(bot: "GridBot") -> None:
    """Cache read-only market data without blocking the API or trading loop."""
    next_atr_refresh = 0.0
    while True:
        if time.monotonic() >= next_atr_refresh:
            try:
                app.state.atr_snapshot = await asyncio.to_thread(_fetch_atr_snapshot, bot)
            except Exception as error:
                app.state.atr_snapshot = None
                LOGGER.warning("Hourly ATR unavailable: %s", type(error).__name__)
            next_atr_refresh = time.monotonic() + ATR_REFRESH_SECONDS
        try:
            app.state.order_book_snapshot = await asyncio.to_thread(
                _fetch_order_book_snapshot, bot
            )
        except Exception as error:
            app.state.order_book_snapshot = None
            LOGGER.warning("Order book imbalance unavailable: %s", type(error).__name__)
        await asyncio.sleep(ORDER_BOOK_REFRESH_SECONDS)


@app.get("/api/bot/status")
def bot_status() -> Dict[str, Any]:
    """Summarize persisted grid orders; only a running bot is marked online."""
    bot = app.state.grid_bot
    database = bot.database if bot is not None else GridDatabase()
    orders = [
        order for order in database.fetch_active_grids()
        if order["order_type"] == "LIMIT"
    ]
    prices = [Decimal(order["price"]) for order in orders]
    safety_mode = database.get_state(SAFETY_MODE_KEY)
    atr = app.state.atr_snapshot if bot is not None else None
    order_book = app.state.order_book_snapshot if bot is not None else None
    stop_value = getattr(bot.config, "stop_loss_price", None) if bot is not None else None
    if stop_value is None:
        active_text = database.get_state("active_grid_config")
        if active_text:
            try:
                stop_value = Decimal(json.loads(active_text)["stop_loss_price"])
            except (KeyError, ValueError, InvalidOperation, TypeError):
                stop_value = None
    high_water_mark = None
    trailing_text = database.get_state(TRAILING_STOP_KEY)
    if trailing_text:
        try:
            high_water_mark = Decimal(json.loads(trailing_text)["high_water_mark"])
        except (KeyError, ValueError, InvalidOperation, TypeError):
            pass
    return {
        "status": "Online" if bot is not None else "Offline",
        "pair": bot.config.symbol if bot is not None else "BTC/USDT",
        "safety_pause": (
            "Active" if safety_mode is not None
            else "Normal"
        ),
        "pause_mode": safety_mode,
        "trading_state": safety_mode if safety_mode in (
            LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED) else "ACTIVE",
        "grid_levels": len(orders),
        "exact_grid_recenter_supported": True,
        "lower_bound": float(min(prices)) if prices else None,
        "upper_bound": float(max(prices)) if prices else None,
        "wallet": _portfolio_wallet(database) if bot is not None
                  else _unavailable_wallet(),
        "atr_value": atr["atr_value"] if atr is not None else None,
        "atr_percentage": atr["atr_percentage"] if atr is not None else None,
        "bid_volume": order_book["bid_volume"] if order_book is not None else None,
        "ask_volume": order_book["ask_volume"] if order_book is not None else None,
        "imbalance_ratio": order_book["imbalance_ratio"] if order_book is not None else None,
        "current_hard_stop_loss": float(stop_value) if stop_value is not None else None,
        "high_water_mark": (float(high_water_mark)
                            if high_water_mark is not None else None),
    }


@app.post("/api/bot/pause")
def set_bot_pause(payload: PauseRequest, _: None = Depends(_require_dashboard_origin),
                  __: str = Depends(get_current_user)) -> Dict[str, Any]:
    bot = app.state.grid_bot
    if bot is None:
        raise HTTPException(status_code=503, detail="The trading bot is offline.")
    try:
        mode = bot.set_manual_pause(payload.active)
    except TradingHalt as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (ccxt.BaseError, OSError, RuntimeError) as error:
        LOGGER.exception("Manual safety pause could not finish.")
        raise HTTPException(status_code=502, detail="Exchange pause update failed; state remains paused.") from error
    return {"safety_pause": "Active" if mode else "Normal", "mode": mode}


@app.post("/api/bot/grid/recenter", status_code=202)
def recenter_grid(payload: RecenterRequest,
                  _: None = Depends(_require_dashboard_origin),
                  __: str = Depends(get_current_user)) -> Dict[str, Any]:
    if (payload.allocated_capital is None) != (payload.grid_levels is None):
        raise HTTPException(
            status_code=422,
            detail="Provide allocated_capital and grid_levels together.",
        )
    if (payload.allocated_capital is not None and
            Decimal(str(payload.allocated_capital)) / payload.grid_levels < Decimal("7")):
        raise HTTPException(status_code=422, detail="Order size must be at least 7 USDT.")
    new_format = (payload.width_percentage is not None or
                  payload.stop_loss_percentage is not None)
    if new_format:
        if (payload.width_percentage is None or
                payload.stop_loss_percentage is None or
                payload.half_width_percentage is not None):
            raise HTTPException(
                status_code=422,
                detail="Provide width_percentage and stop_loss_percentage together.",
            )
        width = payload.width_percentage
        stop_distance = payload.stop_loss_percentage
    elif payload.half_width_percentage is not None:
        width = payload.half_width_percentage
        stop_distance = None
    else:
        raise HTTPException(status_code=422, detail="Grid width is required.")
    bot = app.state.grid_bot
    if bot is None:
        raise HTTPException(status_code=503, detail="The trading bot is offline.")
    if (stop_distance is not None and isinstance(bot, GridBot) and
            bot.database.get_state(SAFETY_MODE_KEY) not in
            (LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED)):
        requested_stop = Decimal(str(payload.center_price)) * (
            1 - Decimal(str(stop_distance)) / 100
        )
        if requested_stop < bot.grid_configuration()[3]:
            raise HTTPException(status_code=400, detail=RISK_OVERRIDE_DENIED)
    try:
        if payload.allocated_capital is not None:
            lower, upper = bot.request_manual_recenter(
                str(payload.center_price), str(width),
                str(stop_distance) if stop_distance is not None else None,
                str(payload.allocated_capital), payload.grid_levels,
            )
        elif stop_distance is None:
            lower, upper = bot.request_manual_recenter(str(payload.center_price), str(width))
        else:
            lower, upper = bot.request_manual_recenter(
                str(payload.center_price), str(width), str(stop_distance)
            )
    except ValueError as error:
        code = 400 if str(error) == RISK_OVERRIDE_DENIED else 422
        raise HTTPException(status_code=code, detail=str(error)) from error
    except TradingHalt as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (ccxt.BaseError, OSError, RuntimeError) as error:
        LOGGER.exception("Manual grid recenter request failed.")
        raise HTTPException(status_code=502, detail="Could not queue the grid reset.") from error
    return {"status": "queued", "lower_bound": float(lower),
            "upper_bound": float(upper)}


class TradingHalt(RuntimeError):
    """An unsafe or unresolved state that requires the bot to stop."""


class UncertainOrderError(TradingHalt):
    """The exchange may have accepted an order without returning its ID."""


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Set grid.{name} to a positive number in config.json.")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"grid.{name} must be a valid number.") from error
    if not result.is_finite() or result <= 0:
        raise ValueError(f"grid.{name} must be positive and finite.")
    return result


def _order_decimal(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value if value is not None else default))


@dataclass(frozen=True)
class GridConfig:
    symbol: str
    investment_quote: Decimal
    lower_price: Decimal
    upper_price: Decimal
    spacing_percent: Decimal
    initial_inventory_percent: Decimal
    stop_loss_price: Decimal
    poll_seconds: int
    auto_center_percent: Optional[Decimal] = None
    buy_grid_levels: Optional[int] = None
    sell_grid_levels: Optional[int] = None

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "GridConfig":
        raw = load_config(path)
        grid = raw["grid"]
        investment = _decimal(grid.get("investment_quote"), "investment_quote")
        lower = _decimal(grid.get("lower_price"), "lower_price")
        upper = _decimal(grid.get("upper_price"), "upper_price")
        spacing = _decimal(grid.get("spacing_percent"), "spacing_percent")
        inventory_percent = _decimal(
            grid.get("initial_inventory_percent", 50), "initial_inventory_percent"
        )
        stop_loss = _decimal(grid.get("stop_loss_price"), "stop_loss_price")
        poll_seconds = grid.get("poll_seconds", 10)
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, int):
            raise ValueError("grid.poll_seconds must be an integer.")
        if not 2 <= poll_seconds <= 3600:
            raise ValueError("grid.poll_seconds must be between 2 and 3600.")
        if not 0 < stop_loss < lower < upper:
            raise ValueError("Require stop_loss_price < lower_price < upper_price.")
        if spacing >= 100:
            raise ValueError("grid.spacing_percent must be below 100.")
        if inventory_percent >= 100:
            raise ValueError("grid.initial_inventory_percent must be below 100.")
        configured_width = grid.get("auto_center_percent")
        width = (
            _decimal(configured_width, "auto_center_percent")
            if configured_width is not None
            else (upper - lower) * 100 / (upper + lower)
        )
        if width >= 100:
            raise ValueError("grid.auto_center_percent must be below 100.")
        return cls(raw["grid"]["symbol"], investment, lower, upper,
                   spacing, inventory_percent, stop_loss, poll_seconds, width)

    def fingerprint(self) -> str:
        parameters = {
            "symbol": self.symbol,
            "investment_quote": str(self.investment_quote),
            "lower_price": str(self.lower_price),
            "upper_price": str(self.upper_price),
            "spacing_percent": str(self.spacing_percent),
            "initial_inventory_percent": str(self.initial_inventory_percent),
            "stop_loss_price": str(self.stop_loss_price),
        }
        if self.buy_grid_levels is not None and self.sell_grid_levels is not None:
            parameters["buy_grid_levels"] = self.buy_grid_levels
            parameters["sell_grid_levels"] = self.sell_grid_levels
        encoded = json.dumps(parameters, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def breakout_fingerprint(self) -> str:
        """Grid geometry stays stable when the trailing stop rises."""
        parameters = {
            "symbol": self.symbol,
            "investment_quote": str(self.investment_quote),
            "lower_price": str(self.lower_price),
            "upper_price": str(self.upper_price),
            "spacing_percent": str(self.spacing_percent),
            "initial_inventory_percent": str(self.initial_inventory_percent),
        }
        if self.buy_grid_levels is not None and self.sell_grid_levels is not None:
            parameters["buy_grid_levels"] = self.buy_grid_levels
            parameters["sell_grid_levels"] = self.sell_grid_levels
        encoded = json.dumps(parameters, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def geometric_levels(anchor: Decimal, lower: Decimal, spacing_percent: Decimal) -> List[Decimal]:
    """Each next buy price is a percentage below the previous level."""
    ratio = Decimal(1) - spacing_percent / Decimal(100)
    prices: List[Decimal] = []
    price = anchor * ratio
    while price >= lower:
        prices.append(price)
        if len(prices) > MAX_LEVELS:
            raise ValueError(f"Grid exceeds the {MAX_LEVELS}-level safety cap.")
        price *= ratio
    if not prices:
        raise ValueError("Bounds contain no buy level at this spacing.")
    return prices


def geometric_upper_levels(
    anchor: Decimal, upper: Decimal, spacing_percent: Decimal
) -> List[Decimal]:
    """Each next sell price is a percentage above the previous level."""
    ratio = Decimal(1) + spacing_percent / Decimal(100)
    prices: List[Decimal] = []
    price = anchor * ratio
    while price <= upper:
        prices.append(price)
        if len(prices) > MAX_LEVELS:
            raise ValueError(f"Upper grid exceeds the {MAX_LEVELS}-level safety cap.")
        price *= ratio
    if not prices:
        raise ValueError("Bounds contain no upper sell level at this spacing.")
    return prices


def exact_geometric_levels(
    anchor: Decimal, bound: Decimal, count: int,
) -> List[Decimal]:
    """Fit an exact number of geometric levels from center to one bound."""
    if not 1 <= count <= MAX_LEVELS or anchor <= 0 or bound <= 0 or anchor == bound:
        raise ValueError("Exact grid geometry is invalid.")
    ratio = (bound / anchor) ** (Decimal(1) / Decimal(count))
    return [anchor * ratio ** index if index < count else bound
            for index in range(1, count + 1)]


def configured_grid_levels(
    anchor: Decimal, config: GridConfig,
) -> Tuple[List[Decimal], List[Decimal]]:
    if config.buy_grid_levels is None and config.sell_grid_levels is None:
        return (
            geometric_levels(anchor, config.lower_price, config.spacing_percent),
            geometric_upper_levels(anchor, config.upper_price, config.spacing_percent),
        )
    if config.buy_grid_levels is None or config.sell_grid_levels is None:
        raise TradingHalt("Saved exact grid level counts are incomplete.")
    return (
        exact_geometric_levels(anchor, config.lower_price, config.buy_grid_levels),
        exact_geometric_levels(anchor, config.upper_price, config.sell_grid_levels),
    )


def _fees_in_asset(order: Dict[str, Any], asset: str) -> Decimal:
    fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
    return sum(
        (_order_decimal(fee.get("cost")) for fee in fees if fee.get("currency") == asset),
        Decimal(0),
    )


class GridBot:
    def __init__(self, config: GridConfig, exchange: Any, database: GridDatabase) -> None:
        self.config = config
        self.exchange = exchange
        self.database = database
        order_prefix = self.database.get_state(ORDER_CLIENT_PREFIX_KEY)
        if order_prefix is None:
            order_prefix = ORDER_CLIENT_PREFIX + uuid.uuid4().hex[:8]
            self.database.set_state(ORDER_CLIENT_PREFIX_KEY, order_prefix)
        if (not order_prefix.startswith(ORDER_CLIENT_PREFIX) or
                len(order_prefix) != len(ORDER_CLIENT_PREFIX) + 8 or
                not order_prefix.isalnum()):
            raise TradingHalt("Saved order namespace is invalid; inspect SQLite state.")
        self.order_client_prefix = order_prefix
        self.exchange_lock = RLock()
        self.stop_controller = StopController(
            exchange, database, config.symbol, self.exchange_lock
        )
        self.market: Dict[str, Any] = {}
        self.anchor: Optional[Decimal] = None
        self.levels: List[Decimal] = []
        self.upper_levels: List[Decimal] = []
        self.baseline_base: Optional[Decimal] = None
        self.high_water_mark: Optional[Decimal] = None
        self.stop_loss_distance: Optional[Decimal] = None
        self._post_only_rejected_in_cycle = False
        safety_mode = self.database.get_state(SAFETY_MODE_KEY)
        if safety_mode not in (None, PAUSED_DOWNSIDE, PAUSED_MANUAL,
                               LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED):
            raise TradingHalt("Unknown saved safety mode; inspect local state.")
        self.is_paused = safety_mode is not None
        self._cycle_lock = RLock()
        saved_width = self.database.get_state(BREAKOUT_WIDTH_KEY)
        self.breakout_width_percent = (
            _decimal(saved_width, BREAKOUT_WIDTH_KEY) if saved_width is not None
            else config.auto_center_percent or
            (config.upper_price - config.lower_price) * 100 /
            (config.upper_price + config.lower_price)
        )
        if self.breakout_width_percent >= 100:
            raise TradingHalt("Saved breakout grid width must be below 100 percent.")
        self._grid_lock = RLock()
        pending_text = self.database.get_state("grid_reset")
        pending = json.loads(pending_text) if pending_text else None
        self.pending_grid_bounds: Optional[Tuple[Decimal, Decimal]] = (
            (Decimal(pending["lower"]), Decimal(pending["upper"])) if pending else None
        )
        self.grid_needs_reset = pending is not None

    def grid_status(self) -> Tuple[Decimal, Decimal, Decimal, int, Decimal]:
        price = self._ticker_price()
        lower, upper, levels, stop_loss = self.grid_configuration()
        return price, lower, upper, levels, stop_loss

    def wallet_balances(self) -> Dict[str, Dict[str, Optional[Decimal]]]:
        """Read free and locked Spot balances for the configured pair."""
        with self._grid_lock:
            base, quote = self.config.symbol.split("/")
        response = self._call(self.exchange.fetch_balance, {"type": "spot"})
        if not isinstance(response, dict):
            raise ValueError("Exchange returned an invalid balance response.")

        balances: Dict[str, Dict[str, Optional[Decimal]]] = {}
        for asset in (quote, base):
            asset_data = response.get(asset)
            balances[asset] = {}
            for field in ("free", "used"):
                raw = asset_data.get(field) if isinstance(asset_data, dict) else None
                if raw is None:
                    by_field = response.get(field)
                    raw = by_field.get(asset) if isinstance(by_field, dict) else None
                if raw is None:
                    balances[asset][field] = None
                    continue
                try:
                    value = Decimal(str(raw))
                except InvalidOperation as error:
                    raise ValueError("Exchange returned an invalid balance value.") from error
                if not value.is_finite() or value < 0:
                    raise ValueError("Exchange returned an invalid balance value.")
                balances[asset][field] = value
        return balances

    def grid_configuration(self) -> Tuple[Decimal, Decimal, int, Decimal]:
        """Read the active settings without making an exchange request."""
        with self._grid_lock:
            return (self.config.lower_price, self.config.upper_price,
                    len(self.levels) + len(self.upper_levels),
                    self.config.stop_loss_price)

    def open_order_summary(
        self, current_price: Optional[Decimal]
    ) -> Tuple[int, int, Optional[Decimal], Optional[Decimal]]:
        """Count live Spot limit orders, including orders outside local SQLite tracking."""
        orders = self._live_open_orders()
        buy_count = sell_count = 0
        buy_prices: List[Decimal] = []
        sell_prices: List[Decimal] = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            info = order.get("info")
            raw_type = order.get("type") or (
                info.get("type") if isinstance(info, dict) else None
            )
            order_type = str(raw_type or "").lower()
            if order_type not in ("limit", "limit_maker"):
                continue
            if str(order.get("status") or "open").lower() != "open":
                continue
            side = str(order.get("side") or "").lower()
            if side == "buy":
                buy_count += 1
                prices = buy_prices
            elif side == "sell":
                sell_count += 1
                prices = sell_prices
            else:
                continue
            try:
                price = Decimal(str(order.get("price")))
            except InvalidOperation:
                continue
            if price.is_finite() and price > 0:
                prices.append(price)
        closest_buy = (
            min(buy_prices, key=lambda price: abs(price - current_price))
            if buy_prices and current_price is not None else None
        )
        closest_sell = (
            min(sell_prices, key=lambda price: abs(price - current_price))
            if sell_prices and current_price is not None else None
        )
        return buy_count, sell_count, closest_buy, closest_sell

    def _live_open_orders(self) -> List[Dict[str, Any]]:
        with self._grid_lock:
            symbol = self.config.symbol
        orders = self._call(self.exchange.fetch_open_orders, symbol)
        if not isinstance(orders, list):
            raise TradingHalt("Exchange returned an invalid open-order list.")
        return orders

    @staticmethod
    def _client_order_id(order: Dict[str, Any]) -> Optional[str]:
        info = order.get("info")
        client_id = order.get("clientOrderId") or (
            info.get("clientOrderId") if isinstance(info, dict) else None
        )
        return str(client_id) if client_id is not None else None

    def _is_bot_order(
        self, order: Any, tracked: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Recognize new tagged orders and exact IDs saved by older bot versions."""
        if not isinstance(order, dict):
            return False
        client_id = self._client_order_id(order)
        if client_id and client_id.startswith(self.order_client_prefix):
            return True
        exchange_id = order.get("id")
        for row in tracked if tracked is not None else self.database.fetch_active_grids():
            if (client_id and client_id == row.get("client_order_id")) or (
                exchange_id is not None and row.get("exchange_order_id") is not None
                and str(exchange_id) == str(row["exchange_order_id"])
            ):
                return True
        return False

    def _cancel_bot_orders(
        self, *, side: Optional[str] = None,
        selected_ids: Optional[set[str]] = None,
        marker_prefix: Optional[str] = None,
        limit_only: bool = False,
    ) -> Tuple[List[Tuple[str, Tuple[str, ...]]], bool]:
        """Cancel only this bot's live orders and verify tracked SQLite rows."""
        with self.stop_controller._lock:
            tracked = self.database.fetch_active_grids()
            selected = [
                row for row in tracked
                if (side is None or row["side"].lower() == side)
                and (not limit_only or row["order_type"] == "LIMIT")
                and (selected_ids is None or row["order_id"] in selected_ids)
            ]

            def matches(order: Any) -> bool:
                if not self._is_bot_order(order, tracked):
                    return False
                if side is not None and str(order.get("side") or "").lower() != side:
                    return False
                if limit_only and str(order.get("type") or "").lower() not in (
                    "limit", "limit_maker"
                ):
                    return False
                if selected_ids is None:
                    return True
                return any(
                    (order.get("id") is not None and row.get("exchange_order_id") is not None
                     and str(order["id"]) == str(row["exchange_order_id"])) or
                    (self._client_order_id(order) is not None and
                     self._client_order_id(order) == row.get("client_order_id"))
                    for row in selected
                )

            live = [order for order in self._live_open_orders() if matches(order)]
            for order in live:
                order_id = order.get("id")
                if order_id is None:
                    raise TradingHalt("Bot order has no exchange ID for targeted cancellation.")
                if marker_prefix:
                    for row in selected:
                        if ((row.get("exchange_order_id") is not None and
                             str(order_id) == str(row["exchange_order_id"])) or
                                (self._client_order_id(order) is not None and
                                 self._client_order_id(order) ==
                                 row.get("client_order_id"))):
                            self.database.set_state(
                                f"{marker_prefix}:{row['order_id']}", "1"
                            )
                try:
                    self._call(self.exchange.cancel_order, str(order_id), self.config.symbol)
                except ccxt.OrderNotFound:
                    # A fill can win the race with cancellation; reconcile below.
                    LOGGER.info("Bot order %s disappeared before cancellation.", order_id)

            remaining = any(matches(order) for order in self._live_open_orders())
            events: List[Tuple[str, Tuple[str, ...]]] = []
            for row in selected:
                order = self._fetch_order(row)
                status = str(order.get("status") or "").lower()
                if status == "closed":
                    changed = self.database.mark_order_filled(row["order_id"])
                    if changed:
                        events.append(("filled", (
                            row["order_id"], row["side"],
                            str(order.get("filled") or row["amount"]),
                            str(order.get("average") or order.get("price") or row["price"]),
                        )))
                elif status in ("canceled", "expired", "rejected"):
                    if marker_prefix and status == "canceled" and (
                        self.database.get_state(f"{marker_prefix}:{row['order_id']}") != "1"
                    ):
                        raise TradingHalt("Tracked order canceled outside the expected pause.")
                    self.database.update_order_status(row["order_id"], status.upper())
                elif status == "open":
                    remaining = True
                else:
                    raise TradingHalt("Bot order cancellation returned an unknown status.")
            return events, not remaining

    def list_open_orders(
        self,
    ) -> Tuple[str, str, List[OpenOrderEntry], List[OpenOrderEntry]]:
        """Return every live order by side, sorted nearest to market by limit price."""
        with self._grid_lock:
            base, quote = self.config.symbol.split("/")
        buys: List[OpenOrderEntry] = []
        sells: List[OpenOrderEntry] = []
        orders = self._live_open_orders()
        for order in orders:
            if not isinstance(order, dict):
                continue
            if str(order.get("status") or "open").lower() != "open":
                continue
            side = str(order.get("side") or "").lower()
            if side not in ("buy", "sell"):
                continue
            raw_amount = order.get("remaining")
            if raw_amount is None:
                raw_amount = order.get("amount")
            try:
                amount = Decimal(str(raw_amount))
                if not amount.is_finite() or amount < 0:
                    amount = None
            except InvalidOperation:
                amount = None
            try:
                price = Decimal(str(order.get("price")))
            except InvalidOperation:
                price = None
            if price is not None and (not price.is_finite() or price <= 0):
                price = None
            (buys if side == "buy" else sells).append((amount, price))
        buys.sort(key=lambda item: (item[1] is None, -(item[1] or Decimal(0))))
        sells.sort(key=lambda item: (item[1] is None, item[1] or Decimal(0)))
        return base, quote, buys, sells

    def set_stop_loss(self, price_text: str) -> Decimal:
        try:
            stop_loss = _decimal(price_text, "stop_loss_price")
        except ValueError as error:
            raise ValueError("Enter a valid positive stop-loss price.") from error
        with self._cycle_lock, self._grid_lock:
            if self.database.get_state(SAFETY_MODE_KEY) in (
                    LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED):
                raise TradingHalt("Hard-stop liquidation has locked grid settings.")
            if stop_loss >= self.config.lower_price:
                raise ValueError(
                    "❌ Rejected: Stop-loss must be lower than the current lower bound."
                )
            if stop_loss < self.config.stop_loss_price:
                raise ValueError("The trailing hard stop cannot be lowered.")
            if self.stop_controller.stop_requested.is_set():
                raise RuntimeError("Bot is stopping; stop-loss was not changed.")
            if self.grid_needs_reset:
                raise RuntimeError("A grid reset is in progress; stop-loss was not changed.")
            saved_text = self.database.get_state("grid_run")
            if saved_text is None:
                raise RuntimeError("No active grid run is available.")
            saved = json.loads(saved_text)
            if saved["fingerprint"] != self.config.fingerprint():
                raise TradingHalt("Saved grid settings do not match the active run.")
            new_config = replace(self.config, stop_loss_price=stop_loss)
            saved["fingerprint"] = new_config.fingerprint()
            active = {
                "lower": str(new_config.lower_price),
                "upper": str(new_config.upper_price),
                "spacing": str(new_config.spacing_percent),
                "investment_quote": str(new_config.investment_quote),
                "stop_loss_price": str(stop_loss),
            }
            if new_config.buy_grid_levels is not None:
                active["buy_levels"] = new_config.buy_grid_levels
                active["sell_levels"] = new_config.sell_grid_levels
            if self.high_water_mark is None:
                raise TradingHalt("Trailing stop was not initialized.")
            distance = self.high_water_mark - stop_loss
            if distance <= 0:
                raise ValueError("Stop-loss must remain below the high water mark.")
            self.database.update_runtime_grid_settings(
                json.dumps(saved, sort_keys=True), json.dumps(active, sort_keys=True),
                trailing_stop=json.dumps({
                    "high_water_mark": str(self.high_water_mark),
                    "stop_loss_distance": str(distance),
                }, sort_keys=True),
            )
            self.config = new_config
            self.stop_loss_distance = distance
        return stop_loss

    def request_grid_reset(self, lower_text: str, upper_text: str) -> None:
        lower = _decimal(lower_text, "lower_price")
        upper = _decimal(upper_text, "upper_price")
        if not self.config.stop_loss_price < lower < upper:
            raise ValueError("Require stop-loss < lower < upper.")
        price = self._ticker_price()
        if not lower < price < upper:
            raise ValueError("Current price must be inside the new bounds.")
        candidate = replace(self.config, lower_price=lower, upper_price=upper,
                            spacing_percent=RESET_SPACING_PERCENT)
        configured_grid_levels(price, candidate)
        with self._grid_lock:
            if self.stop_controller.stop_requested.is_set():
                raise RuntimeError("Bot is stopping; grid was not changed.")
            if self.grid_needs_reset or self.database.get_state("grid_reset"):
                raise RuntimeError("A grid reset is already in progress.")
            request = {"phase": "canceling", "lower": str(lower),
                       "upper": str(upper), "spacing": str(RESET_SPACING_PERCENT)}
            self.database.clear_state(BREAKOUT_TIMER_KEY)
            self.database.set_state("grid_reset", json.dumps(request))
            self.pending_grid_bounds = (lower, upper)
            self.grid_needs_reset = True

    def request_manual_recenter(self, center_text: str, width_text: str,
                                stop_distance_text: Optional[str] = None,
                                allocated_capital_text: Optional[str] = None,
                                grid_levels: Optional[int] = None,
                                ) -> Tuple[Decimal, Decimal]:
        """Queue a persisted reset with explicit bounds and optional pause trigger."""
        center = _decimal(center_text, "center_price")
        width_percent = _decimal(width_text, "width_percentage")
        if width_percent >= 100:
            raise ValueError("Width must be below 100%.")
        width = width_percent / 100
        lower, upper = center * (1 - width), center * (1 + width)
        if (allocated_capital_text is None) != (grid_levels is None):
            raise ValueError("Allocated capital and grid levels must be supplied together.")
        candidate = replace(self.config, lower_price=lower, upper_price=upper)
        if allocated_capital_text is not None:
            capital = _decimal(allocated_capital_text, "allocated_capital")
            if (isinstance(grid_levels, bool) or not isinstance(grid_levels, int) or
                    not 2 <= grid_levels <= 2 * MAX_LEVELS):
                raise ValueError("Grid levels must be a whole number from 2 to 100.")
            if capital / grid_levels < Decimal("7"):
                raise ValueError("Order size must be at least 7 USDT.")
            candidate = replace(
                candidate, investment_quote=capital,
                buy_grid_levels=(grid_levels + 1) // 2,
                sell_grid_levels=grid_levels // 2,
            )
        requested_stop = None
        if stop_distance_text is not None:
            stop_distance = _decimal(stop_distance_text, "stop_loss_percentage")
            if stop_distance >= 100:
                raise ValueError("Stop-loss distance must be below 100%.")
            requested_stop = center * (1 - stop_distance / 100)
            if requested_stop >= lower:
                raise ValueError("Pause trigger must be below the projected lower bound.")
        with self._cycle_lock:
            with self._grid_lock:
                if self.stop_controller.stop_requested.is_set():
                    raise TradingHalt("Bot is stopping; grid was not changed.")
                if self.database.get_state(SAFETY_MODE_KEY) == LIQUIDATED:
                    if requested_stop is None:
                        raise ValueError("A new hard-stop price is required to reset liquidation.")
                    return self._restart_liquidated_run(
                        center, width_percent, lower, upper, requested_stop, candidate
                    )
                if self.is_paused:
                    raise TradingHalt("Release Safety Pause before re-anchoring the grid.")
                if self.grid_needs_reset or self.database.get_state("grid_reset"):
                    raise TradingHalt("A grid reset is already in progress.")
                if self.database.get_state("grid_run") is None:
                    raise TradingHalt("No active grid run is available.")
                if requested_stop is None and self.config.stop_loss_price >= lower:
                    raise ValueError("New lower bound must exceed the pause trigger.")
                candidate_buys, candidate_sells = configured_grid_levels(center, candidate)
                self._validate_level_prices(center, candidate_buys, candidate_sells)
                if allocated_capital_text is not None:
                    self._validate_nominal_grid_sizes(
                        candidate, center, candidate_buys, candidate_sells
                    )
                price = self._ticker_price()
                self.advance_trailing_stop(price)
                if (requested_stop is not None and
                        requested_stop < self.config.stop_loss_price):
                    raise ValueError(RISK_OVERRIDE_DENIED)
                if price <= self.config.stop_loss_price:
                    raise TradingHalt("Market is at the pause trigger; recentering is unavailable.")
                planned_stop = requested_stop or self.config.stop_loss_price
                if not any(self._price(raw) > planned_stop for raw in candidate_buys):
                    raise ValueError("Hard stop leaves no safe BUY grid level.")
                ratio = max(center / candidate_buys[0], candidate_sells[0] / center)
                if not center / ratio < price < center * ratio:
                    raise ValueError("Center must be close to the live market price.")
                request = {
                    "phase": "canceling", "source": "manual_recenter",
                    "center_price": str(center),
                    "width_percent": str(width_percent),
                    "lower": str(lower), "upper": str(upper),
                    "spacing": str(self.config.spacing_percent),
                    "investment_quote": str(candidate.investment_quote),
                }
                if candidate.buy_grid_levels is not None:
                    request["buy_levels"] = candidate.buy_grid_levels
                    request["sell_levels"] = candidate.sell_grid_levels
                if requested_stop is not None:
                    request["stop_loss_price"] = str(requested_stop)
                self.database.set_state("grid_reset", json.dumps(request, sort_keys=True))
                self.database.clear_state(BREAKOUT_TIMER_KEY)
                self.pending_grid_bounds = (lower, upper)
                self.grid_needs_reset = True
                return lower, upper

    def _restart_liquidated_run(self, center: Decimal, width_percent: Decimal,
                                lower: Decimal, upper: Decimal,
                                stop_loss: Decimal, candidate: GridConfig,
                                ) -> Tuple[Decimal, Decimal]:
        """Only an authenticated recenter may release a completed hard stop."""
        state_text = self.database.get_state(LIQUIDATION_KEY)
        if not state_text or json.loads(state_text).get("phase") != "complete":
            raise TradingHalt("Liquidation is unresolved; inspect exchange orders first.")
        if any(self._is_bot_order(order) for order in self._live_open_orders()):
            raise TradingHalt("Bot orders remain; cannot reset a liquidated grid.")
        if self.database.fetch_active_grids():
            raise TradingHalt("Tracked orders remain active; cannot reset liquidation.")
        price = self._ticker_price()
        candidate_buys, candidate_sells = configured_grid_levels(center, candidate)
        if candidate.buy_grid_levels is not None:
            self._validate_level_prices(center, candidate_buys, candidate_sells)
        ratio = max(center / candidate_buys[0], candidate_sells[0] / center)
        if not center / ratio < price < center * ratio:
            raise ValueError("Center must be close to the live market price.")
        if not stop_loss < lower < price < upper:
            raise ValueError("New hard stop and bounds must contain the live price.")
        new_config = replace(candidate, stop_loss_price=stop_loss)
        lowers, uppers = self._validate_reset_grid(new_config, center, Decimal(0))
        baseline = self._free_balance(self.market["base"])
        run = {"anchor": str(center), "baseline_base": str(baseline),
               "fingerprint": new_config.fingerprint()}
        active = {"lower": str(lower), "upper": str(upper),
                  "spacing": str(new_config.spacing_percent),
                  "investment_quote": str(new_config.investment_quote),
                  "stop_loss_price": str(stop_loss)}
        if new_config.buy_grid_levels is not None:
            active["buy_levels"] = new_config.buy_grid_levels
            active["sell_levels"] = new_config.sell_grid_levels
        trailing = {"high_water_mark": str(center),
                    "stop_loss_distance": str(center - stop_loss)}
        request = {"phase": "placing", "source": "manual_liquidation_reset",
                   "lower": str(lower), "upper": str(upper),
                   "spacing": str(new_config.spacing_percent),
                   "investment_quote": str(new_config.investment_quote)}
        if new_config.buy_grid_levels is not None:
            request["buy_levels"] = new_config.buy_grid_levels
            request["sell_levels"] = new_config.sell_grid_levels
        self.database.complete_grid_reset(
            json.dumps(run, sort_keys=True), json.dumps(active, sort_keys=True),
            json.dumps(request, sort_keys=True),
            breakout_width_percent=str(width_percent),
            trailing_stop=json.dumps(trailing, sort_keys=True),
            clear_liquidation_state=True,
        )
        self.database.clear_state(BREAKOUT_TIMER_KEY)
        self.config = new_config
        self.anchor = center
        self.baseline_base = baseline
        self.levels, self.upper_levels = lowers, uppers
        self.breakout_width_percent = width_percent
        self.high_water_mark = center
        self.stop_loss_distance = center - stop_loss
        self.pending_grid_bounds = (lower, upper)
        self.grid_needs_reset = True
        self.is_paused = False
        LOGGER.warning("Admin reset released a completed hard-stop liquidation.")
        return lower, upper

    def _request_breakout_reset(self, price: Decimal) -> None:
        """Persist a reset intent before any order cancellation can begin."""
        width = self.breakout_width_percent / 100
        lower = price * (1 - width)
        upper = price * (1 + width)
        if not lower < price < upper or self.config.stop_loss_price >= price:
            raise TradingHalt("Breakout bounds would violate the hard stop.")
        candidate = replace(self.config, lower_price=lower, upper_price=upper)
        lowers, _ = configured_grid_levels(price, candidate)
        if candidate.buy_grid_levels is not None and any(
                self._price(raw) <= candidate.stop_loss_price for raw in lowers):
            raise TradingHalt(
                "Trailing stop would remove requested BUY levels from the exact grid."
            )
        if not any(self._price(raw) > self.config.stop_loss_price for raw in lowers):
            raise TradingHalt("Trailing stop leaves no safe BUY grid level.")
        with self._grid_lock:
            if self.stop_controller.stop_requested.is_set() or self.grid_needs_reset:
                return
            request = {
                "phase": "canceling", "source": "breakout",
                "previous_upper": str(self.config.upper_price),
                "width_percent": str(self.breakout_width_percent),
                "lower": str(lower), "upper": str(upper),
                "spacing": str(self.config.spacing_percent),
            }
            self.database.set_state("grid_reset", json.dumps(request, sort_keys=True))
            self.database.clear_state(BREAKOUT_TIMER_KEY)
            self.pending_grid_bounds = (lower, upper)
            self.grid_needs_reset = True
            LOGGER.info("Four-hour breakout confirmed at %s; grid reset queued.", price)

    def _observe_upper_breakout(self, price: Decimal, *, now: Optional[float] = None) -> bool:
        """Require four hours of uninterrupted above-bound observations."""
        if self.is_paused or self.grid_needs_reset or price <= self.config.upper_price:
            self.database.clear_state(BREAKOUT_TIMER_KEY)
            return False
        now = time.time() if now is None else now
        if not math.isfinite(now) or now < 0:
            raise TradingHalt("System clock is invalid for breakout tracking.")
        text = self.database.get_state(BREAKOUT_TIMER_KEY)
        prior = None
        if text:
            try:
                prior = json.loads(text)
                started = float(prior["started_at"])
                last = float(prior["last_seen_at"])
                if (not math.isfinite(started) or not math.isfinite(last) or
                        started > last or last > now or
                        prior["grid_fingerprint"] != self.config.breakout_fingerprint() or
                        now - last > max(60, self.config.poll_seconds * 3)):
                    prior = None
            except (TypeError, ValueError, KeyError):
                prior = None
        started = float(prior["started_at"]) if prior else now
        if now - started >= BREAKOUT_COOLDOWN_SECONDS:
            self._request_breakout_reset(price)
            return self.grid_needs_reset
        self.database.set_state(BREAKOUT_TIMER_KEY, json.dumps({
            "started_at": started,
            "last_seen_at": now,
            "grid_fingerprint": self.config.breakout_fingerprint(),
        }, sort_keys=True))
        return False

    def _call(self, method: Any, *args: Any) -> Any:
        with self.exchange_lock:
            return method(*args)

    def _ticker_price(self) -> Decimal:
        ticker = self._call(self.exchange.fetch_ticker, self.config.symbol)
        price = _order_decimal(ticker.get("last"))
        if not price.is_finite() or price <= 0:
            raise TradingHalt("Ticker has no valid last price.")
        return price

    def advance_trailing_stop(self, price: Decimal) -> None:
        """Persist a new high and raised liquidation floor as one grid update."""
        with self._cycle_lock, self._grid_lock:
            if self.high_water_mark is None or self.stop_loss_distance is None:
                raise TradingHalt("Trailing stop was not initialized.")
            if price <= self.high_water_mark:
                return
            if self.database.get_state(SAFETY_MODE_KEY) in (
                    LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED):
                return
            saved_text = self.database.get_state("grid_run")
            if saved_text is None:
                raise TradingHalt("No active grid run is available for trailing.")
            saved = json.loads(saved_text)
            if saved["fingerprint"] != self.config.fingerprint():
                raise TradingHalt("Saved grid settings changed during trailing.")
            proposed_stop = price - self.stop_loss_distance
            new_stop = max(self.config.stop_loss_price, proposed_stop)
            new_config = replace(self.config, stop_loss_price=new_stop)
            saved["fingerprint"] = new_config.fingerprint()
            active = {
                "lower": str(new_config.lower_price),
                "upper": str(new_config.upper_price),
                "spacing": str(new_config.spacing_percent),
                "investment_quote": str(new_config.investment_quote),
                "stop_loss_price": str(new_stop),
            }
            if new_config.buy_grid_levels is not None:
                active["buy_levels"] = new_config.buy_grid_levels
                active["sell_levels"] = new_config.sell_grid_levels
            trailing = {
                "high_water_mark": str(price),
                "stop_loss_distance": str(self.stop_loss_distance),
            }
            self.database.update_runtime_grid_settings(
                json.dumps(saved, sort_keys=True),
                json.dumps(active, sort_keys=True),
                trailing_stop=json.dumps(trailing, sort_keys=True),
                allow_pending_reset=True,
            )
            if new_stop > self.config.stop_loss_price:
                LOGGER.info("Trailing hard stop raised to %s after new high %s.",
                            new_stop, price)
            self.config = new_config
            self.high_water_mark = price

    def prepare(
        self, *, persist: bool
    ) -> Tuple[Decimal, List[Tuple[int, Decimal, Decimal]], List[Tuple[int, Decimal, Decimal]]]:
        self._call(self.exchange.load_markets)
        self.market = self.exchange.market(self.config.symbol)
        if not self.market.get("spot") or self.market.get("active") is False:
            raise TradingHalt("Configured symbol is not an active Spot market.")
        current_price = self._ticker_price()
        if persist:
            self.database.set_state(LAST_MARKET_PRICE_KEY, str(current_price))

        saved = self.database.get_state("grid_run")
        if saved:
            state = json.loads(saved)
            if state["fingerprint"] != self.config.fingerprint():
                raise TradingHalt("Grid settings changed since the saved run; reconcile orders first.")
            self.anchor = Decimal(state["anchor"])
            if "baseline_base" not in state:
                raise TradingHalt("Saved run lacks its starting base balance; reconcile it first.")
            self.baseline_base = Decimal(state["baseline_base"])
        else:
            if self.database.fetch_all_orders():
                raise TradingHalt("Orders exist without a saved anchor; reconcile them first.")
            if not self.config.lower_price < current_price < self.config.upper_price:
                raise ValueError(
                    f"Current price {current_price} must be inside "
                    f"{self.config.lower_price}–{self.config.upper_price}."
                )
            self.anchor = current_price
            self.baseline_base = self._free_balance(self.market["base"])

        if not self.config.lower_price < self.anchor < self.config.upper_price:
            raise TradingHalt("Saved anchor lies outside the configured grid bounds.")
        trailing_text = self.database.get_state(TRAILING_STOP_KEY)
        if trailing_text:
            try:
                trailing = json.loads(trailing_text)
                self.high_water_mark = _decimal(
                    trailing["high_water_mark"], "high_water_mark"
                )
                self.stop_loss_distance = _decimal(
                    trailing["stop_loss_distance"], "stop_loss_distance"
                )
            except (KeyError, TypeError, ValueError) as error:
                raise TradingHalt("Saved trailing stop state is invalid.") from error
            if (self.high_water_mark <= self.config.stop_loss_price or
                    self.high_water_mark - self.stop_loss_distance >
                    self.config.stop_loss_price):
                raise TradingHalt("Saved trailing stop conflicts with the active grid.")
        else:
            self.high_water_mark = self.anchor
            self.stop_loss_distance = self.anchor - self.config.stop_loss_price
            if self.stop_loss_distance <= 0:
                raise TradingHalt("Grid anchor must exceed the hard stop.")
            if persist:
                self.database.set_state(TRAILING_STOP_KEY, json.dumps({
                    "high_water_mark": str(self.high_water_mark),
                    "stop_loss_distance": str(self.stop_loss_distance),
                }, sort_keys=True))
        self.levels, self.upper_levels = configured_grid_levels(self.anchor, self.config)
        if self.config.buy_grid_levels is not None:
            self._validate_level_prices(self.anchor, self.levels, self.upper_levels)
        seed_quote = self._seed_quote()
        quote_per_level = self._lower_quote_per_level()
        seed_amount = self._amount(seed_quote / current_price)
        self._check_order_size(current_price, seed_amount)
        planned: List[Tuple[int, Decimal, Decimal]] = []
        for level, raw_price in enumerate(self.levels, start=1):
            price = self._price(raw_price)
            if price <= self.config.stop_loss_price:
                continue
            amount = self._amount(quote_per_level / price)
            self._check_order_size(price, amount)
            if price * amount > quote_per_level:
                raise TradingHalt("A rounded grid order exceeds its quote allocation.")
            planned.append((level, price, amount))

        upper_amount = self._amount(
            seed_amount * (1 - SELL_AMOUNT_BUFFER) / Decimal(len(self.upper_levels))
        )
        upper_planned: List[Tuple[int, Decimal, Decimal]] = []
        for index, raw_price in enumerate(self.upper_levels, start=1):
            price = self._price(raw_price)
            self._check_order_size(price, upper_amount)
            upper_planned.append((-index, price, upper_amount))

        if persist and not saved:
            state = {
                "anchor": str(self.anchor),
                "baseline_base": str(self.baseline_base),
                "fingerprint": self.config.fingerprint(),
            }
            self.database.set_state("grid_run", json.dumps(state, sort_keys=True))
        if persist and self.database.get_state(BREAKOUT_WIDTH_KEY) is None:
            self.database.set_state(BREAKOUT_WIDTH_KEY, str(self.breakout_width_percent))
        return current_price, planned, upper_planned

    def _seed_quote(self) -> Decimal:
        return self.config.investment_quote * self.config.initial_inventory_percent / 100

    def _lower_quote_per_level(self) -> Decimal:
        lower_budget = self.config.investment_quote - self._seed_quote()
        return lower_budget / Decimal(len(self.levels))

    def _price(self, value: Decimal) -> Decimal:
        return Decimal(self.exchange.price_to_precision(self.config.symbol, str(value)))

    def _amount(self, value: Decimal) -> Decimal:
        return Decimal(self.exchange.amount_to_precision(self.config.symbol, str(value)))

    def _check_order_size(self, price: Decimal, amount: Decimal) -> None:
        if price <= 0 or amount <= 0:
            raise ValueError("Order price or amount rounded to zero.")
        limits = self.market.get("limits") or {}
        amount_min = (limits.get("amount") or {}).get("min")
        cost_min = (limits.get("cost") or {}).get("min")
        if amount_min is not None and amount < _order_decimal(amount_min):
            raise ValueError("Order amount is below the market minimum.")
        if cost_min is not None and price * amount < _order_decimal(cost_min):
            raise ValueError("Order notional is below the market minimum.")

    def _validate_level_prices(
        self, center: Decimal, buys: List[Decimal], sells: List[Decimal],
    ) -> None:
        """Reject exact levels that collapse onto one exchange price tick."""
        buy_prices = [self._price(price) for price in buys]
        sell_prices = [self._price(price) for price in sells]
        if (len(set(buy_prices)) != len(buy_prices) or
                len(set(sell_prices)) != len(sell_prices) or
                any(price >= center for price in buy_prices) or
                any(price <= center for price in sell_prices)):
            raise ValueError("Requested grid levels collapse at exchange price precision.")

    def _validate_nominal_grid_sizes(
        self, config: GridConfig, center: Decimal,
        buys: List[Decimal], sells: List[Decimal],
    ) -> None:
        """Check new allocation against actual per-side exchange minimums before canceling."""
        seed_quote = config.investment_quote * config.initial_inventory_percent / 100
        self._check_order_size(center, self._amount(seed_quote / center))
        lower_quote = (config.investment_quote - seed_quote) / len(buys)
        for raw in buys:
            price = self._price(raw)
            self._check_order_size(price, self._amount(lower_quote / price))
        upper_total = self._amount(seed_quote / center)
        upper_amount = self._amount(upper_total * (1 - SELL_AMOUNT_BUFFER) / len(sells))
        for raw in sells:
            self._check_order_size(self._price(raw), upper_amount)

    def _fetch_order(self, row: Dict[str, Any]) -> Dict[str, Any]:
        carry_text = self.database.get_state("carry_inventory")
        carry = json.loads(carry_text) if carry_text else {}
        if carry.get("order_id") == row["order_id"]:
            return {"id": row["order_id"], "status": "closed",
                    "filled": row["amount"], "amount": row["amount"],
                    "average": row["price"], "price": row["price"],
                    "cost": carry["cost"], "fees": []}
        reference = row.get("exchange_order_id") or row["order_id"]
        if row.get("client_order_id"):
            order = self._call(
                self.exchange.fetch_order, reference, self.config.symbol,
                {"origClientOrderId": row["client_order_id"]},
            )
        else:
            order = self._call(self.exchange.fetch_order, reference, self.config.symbol)
        if not isinstance(order, dict):
            raise TradingHalt("Exchange returned an invalid order response.")
        if order.get("id") and not row.get("exchange_order_id"):
            self.database.set_exchange_order_id(row["order_id"], str(order["id"]))
        self._save_fill_snapshot(row["order_id"], order)
        return order

    def _save_fill_snapshot(self, order_id: str, order: Dict[str, Any]) -> None:
        """Cache exchange-confirmed fills for read-only portfolio calculations."""
        try:
            if order.get("filled") is None:
                return
            filled = Decimal(str(order["filled"]))
            if not filled.is_finite() or filled < 0:
                return
            quote_cost = Decimal(str(order.get("cost") or 0))
            if filled > 0 and quote_cost <= 0:
                execution_price = order.get("average") or order.get("price")
                if execution_price is None:
                    return
                quote_cost = filled * Decimal(str(execution_price))
            snapshot = json.dumps({
                "filled_base": str(filled),
                "filled_quote": str(quote_cost),
                "base_fee": str(_fees_in_asset(order, self.market["base"])),
                "quote_fee": str(_fees_in_asset(order, self.market["quote"])),
            }, sort_keys=True)
            key = FILL_SNAPSHOT_PREFIX + order_id
            if self.database.get_state(key) != snapshot:
                self.database.set_state(key, snapshot)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            LOGGER.warning("Fill snapshot unavailable for order %s.", order_id)

    def _backfill_portfolio_snapshots(self) -> None:
        """Read old completed orders once so existing runs have a cost basis."""
        for row in self.database.fetch_all_orders():
            if (not row.get("client_order_id") or row["status"] == "OPEN" or
                    self.database.get_state(FILL_SNAPSHOT_PREFIX + row["order_id"])):
                continue
            try:
                self._fetch_order(row)
            except Exception as error:
                LOGGER.warning("Could not backfill fill for %s: %s",
                               row["order_id"], type(error).__name__)

    def _reconcile_order(self, row: Dict[str, Any]) -> Optional[Tuple[str, Tuple[str, ...]]]:
        order = self._fetch_order(row)
        status = order.get("status")
        if status == "open":
            if _order_decimal(order.get("filled")) > 0 and row["status"] == "OPEN":
                self.database.update_order_status(row["order_id"], "PARTIALLY_FILLED")
            return None
        if status == "closed":
            changed = self.database.mark_order_filled(row["order_id"])
            if changed:
                amount = str(order.get("filled") or row["amount"])
                price = str(order.get("average") or order.get("price") or row["price"])
                return ("filled", (row["order_id"], row["side"], amount, price))
            return None
        if status in ("canceled", "expired", "rejected"):
            self.database.update_order_status(row["order_id"], status.upper())
            if (status == "canceled" and row["side"] == "BUY" and
                    self.database.get_state(f"safety_pause_buy:{row['order_id']}") == "1"):
                return None
            raise TradingHalt(f"Tracked order {row['order_id']} ended as {status}.")
        raise TradingHalt("Exchange returned an unknown order status.")

    def _submit_order(
        self, level: int, side: str, price: Decimal, amount: Decimal,
        *, parent_order_id: Optional[str] = None, order_type: str = "LIMIT",
        quote_cost: Optional[Decimal] = None,
    ) -> Optional[str]:
        client_id = self.order_client_prefix + uuid.uuid4().hex[:20]
        with self.exchange_lock:
            liquidation = order_type == "MARKET" and side == "SELL" and level == -1
            if self.stop_controller.stop_requested.is_set() and not liquidation:
                raise TradingHalt("Stop was requested before order submission.")
            self.database.insert_order(
                client_id, level, side, price, amount,
                client_order_id=client_id, parent_order_id=parent_order_id,
                order_type=order_type,
            )
            try:
                params = {"newClientOrderId": client_id}
                if order_type == "LIMIT":
                    params["timeInForce"] = "PO"
                if quote_cost is not None:
                    params["quoteOrderQty"] = str(quote_cost)
                response = self.exchange.create_order(
                    self.config.symbol, order_type.lower(), side.lower(), float(amount),
                    float(price) if order_type == "LIMIT" else None,
                    params,
                )
            except ccxt.OrderImmediatelyFillable as error:
                if (order_type != "LIMIT" or
                        "Order would immediately match and take." not in str(error)):
                    raise UncertainOrderError(
                        f"Order {client_id} may have been accepted; inspect it before restarting."
                    ) from error
                self.database.discard_rejected_post_only_order(client_id)
                self._post_only_rejected_in_cycle = True
                LOGGER.warning(
                    "Spot Testnet rejected post-only %s %s at %s (grid level %s); "
                    "skipping this order until the next cycle.",
                    side, amount, price, level,
                )
                return None
            except Exception as error:
                raise UncertainOrderError(
                    f"Order {client_id} may have been accepted; inspect it before restarting."
                ) from error
        if not isinstance(response, dict) or not response.get("id"):
            raise UncertainOrderError(
                f"Order {client_id} returned no exchange ID; inspect it before restarting."
            )
        self.database.set_exchange_order_id(client_id, str(response["id"]))
        LOGGER.info(
            "Spot Testnet submitted %s %s %s %s at %s (grid level %s).",
            order_type, side, amount, self.config.symbol, price, level,
        )
        return client_id

    def _free_balance(self, asset: str) -> Decimal:
        balances = self._call(self.exchange.fetch_balance, {"type": "spot"})
        return _order_decimal((balances.get(asset) or {}).get("free"))

    def _free_bot_base(self) -> Decimal:
        if self.baseline_base is None:
            raise TradingHalt("Starting base balance has not been recorded.")
        return max(
            Decimal(0), self._free_balance(self.market["base"]) - self.baseline_base
        )

    def _place_seed_buy(self, current_price: Decimal) -> None:
        quote_cost = self._seed_quote()
        amount = self._amount(quote_cost / current_price)
        self._check_order_size(current_price, amount)
        if self._free_balance(self.market["quote"]) < quote_cost:
            raise TradingHalt("Insufficient free quote balance for the seed buy.")
        if not self.stop_controller.stop_requested.is_set():
            self._submit_order(
                0, "BUY", current_price, amount,
                order_type="MARKET", quote_cost=quote_cost,
            )

    def _place_seed_upper_sell(self, level: int, seed_row: Dict[str, Any]) -> None:
        seed = self._fetch_order(seed_row)
        filled = _order_decimal(seed.get("filled"))
        if filled <= 0:
            raise TradingHalt("Seed buy has no executed amount.")
        base_fee = _fees_in_asset(seed, self.market["base"])
        allocated_base = min(
            filled - base_fee, filled * (1 - SELL_AMOUNT_BUFFER)
        ) / Decimal(len(self.upper_levels))
        amount = self._amount(allocated_base)
        price = self._price(self.upper_levels[-level - 1])
        self._check_order_size(price, amount)
        if self._free_bot_base() < amount:
            return  # Account balance can lag a newly filled market buy.
        if not self.stop_controller.stop_requested.is_set():
            self._submit_order(
                level, "SELL", price, amount,
                parent_order_id=seed_row["order_id"],
            )

    def _place_buy(self, level: int, parent_order_id: Optional[str]) -> None:
        if level > 0:
            price = self._price(self.levels[level - 1])
            amount = self._amount(self._lower_quote_per_level() / price)
        else:
            sell_row = self.database.get_order(parent_order_id) if parent_order_id else None
            if sell_row is None or sell_row["side"] != "SELL":
                raise TradingHalt("Upper grid buy has no parent sell.")
            sell = self._fetch_order(sell_row)
            amount = self._amount(_order_decimal(sell.get("filled")))
            price = self._price(
                self.anchor if level == -1 else self.upper_levels[-level - 2]
            )
        if price <= self.config.stop_loss_price:
            return  # Never create a BUY at or beneath the liquidation floor.
        self._check_order_size(price, amount)
        if self._free_balance(self.market["quote"]) < price * amount:
            raise TradingHalt("Insufficient free quote balance for the next grid buy.")
        if not self.stop_controller.stop_requested.is_set():
            self._submit_order(level, "BUY", price, amount, parent_order_id=parent_order_id)

    def _place_sell(self, row: Dict[str, Any]) -> None:
        buy = self._fetch_order(row)
        filled = _order_decimal(buy.get("filled"))
        if filled <= 0:
            raise TradingHalt("Filled buy has no executed amount.")
        base_fee = _fees_in_asset(buy, self.market["base"])
        conservative_base = min(filled - base_fee, filled * (1 - SELL_AMOUNT_BUFFER))
        amount = self._amount(conservative_base)
        if row["level"] > 0:
            ratio = Decimal(1) - self.config.spacing_percent / Decimal(100)
            target = self.levels[row["level"] - 1] / ratio
        else:
            target = self.upper_levels[-row["level"] - 1]
        price = self._price(target)
        if price > self.config.upper_price:
            raise TradingHalt("Planned sell is above the upper grid bound.")
        self._check_order_size(price, amount)
        if self._free_bot_base() < amount:
            return  # Recheck after the account balance catches up with the fill.
        if not self.stop_controller.stop_requested.is_set():
            self._submit_order(
                row["level"], "SELL", price, amount,
                parent_order_id=row["order_id"],
            )

    def _record_filled_sell(self, row: Dict[str, Any]) -> None:
        parent_id = row.get("parent_order_id")
        parent = self.database.get_order(parent_id) if parent_id else None
        if parent is None or parent["side"] != "BUY":
            raise TradingHalt("Filled sell has no tracked parent buy.")
        buy = self._fetch_order(parent)
        sell = self._fetch_order(row)
        buy_filled = _order_decimal(buy.get("filled"))
        sell_filled = _order_decimal(sell.get("filled"))
        if buy_filled <= 0 or sell_filled <= 0:
            raise TradingHalt("Filled trade has no executed amount.")
        buy_price = _order_decimal(buy.get("average") or buy.get("price"))
        sell_price = _order_decimal(sell.get("average") or sell.get("price"))
        net_base = buy_filled - _fees_in_asset(buy, self.market["base"])
        if net_base <= 0 or sell_filled > net_base:
            raise TradingHalt("Sell amount exceeds the tracked buy's net base amount.")
        allocation = sell_filled / net_base
        buy_cost = _order_decimal(buy.get("cost"), str(buy_price * buy_filled))
        sell_cost = _order_decimal(sell.get("cost"), str(sell_price * sell_filled))
        profit = (
            sell_cost - _fees_in_asset(sell, self.market["quote"])
            - allocation * (buy_cost + _fees_in_asset(buy, self.market["quote"]))
        )
        self.database.record_trade(
            buy_price, sell_price, profit, sell_order_id=row["order_id"]
        )

    def _bot_base_exposure(self) -> Decimal:
        """Estimate only this bot's acquired base, including partial fills."""
        net_base = Decimal(0)
        carry_text = self.database.get_state("carry_inventory")
        carry = json.loads(carry_text) if carry_text else {}
        if carry.get("order_id"):
            row = self.database.get_order(carry["order_id"])
            if row is not None:
                net_base += _order_decimal(row["amount"])
        for row in self.database.fetch_all_orders():
            if not row.get("client_order_id"):
                continue
            order = self._fetch_order(row)
            filled = _order_decimal(order.get("filled"))
            if row["side"] == "BUY":
                net_base += min(
                    filled - _fees_in_asset(order, self.market["base"]),
                    filled * (1 - SELL_AMOUNT_BUFFER),
                )
            else:
                net_base -= filled
        return max(Decimal(0), net_base)

    def _carry_inventory(self, *, require_available: bool = True) -> Tuple[Decimal, Decimal]:
        """Value remaining BTC by its tracked buy lots after all cancels settle."""
        rows = self.database.fetch_all_orders()
        sold_by_buy: Dict[str, Decimal] = {}
        for row in rows:
            if row["side"] != "SELL" or row["order_type"] != "LIMIT":
                continue
            if not row.get("parent_order_id"):
                raise TradingHalt("An old sell has no cost-basis parent.")
            sell = self._fetch_order(row)
            parent = row["parent_order_id"]
            sold = _order_decimal(sell.get("filled"))
            sold_by_buy[parent] = sold_by_buy.get(parent, Decimal(0)) + sold
            if sold > 0:
                self._record_filled_sell(row)
        amount = cost = Decimal(0)
        prior_carry_text = self.database.get_state("carry_inventory")
        prior_carry = json.loads(prior_carry_text) if prior_carry_text else {}
        for row in rows:
            if row["side"] != "BUY" or not (
                row.get("client_order_id") or row["order_id"] == prior_carry.get("order_id")
            ):
                continue
            buy = self._fetch_order(row)
            filled = _order_decimal(buy.get("filled"))
            if filled <= 0:
                continue
            net = filled - _fees_in_asset(buy, self.market["base"])
            remaining = net - sold_by_buy.get(row["order_id"], Decimal(0))
            if remaining < 0:
                raise TradingHalt("Sold BTC exceeds a tracked buy lot.")
            if net <= 0 or remaining == 0:
                continue
            spent = _order_decimal(buy.get("cost"), str(filled *
                                   _order_decimal(buy.get("average") or buy.get("price"))))
            spent += _fees_in_asset(buy, self.market["quote"])
            amount += remaining
            cost += spent * remaining / net
        if amount > 0 and cost <= 0:
            raise TradingHalt("Carried BTC has no verifiable cost basis.")
        if require_available and amount > self._free_bot_base():
            raise TradingHalt("Tracked BTC is not fully available after cancellation.")
        return amount, cost

    def _validate_reset_grid(self, new_config: GridConfig, price: Decimal,
                             carry_amount: Decimal) -> Tuple[List[Decimal], List[Decimal]]:
        lowers, uppers = configured_grid_levels(price, new_config)
        if new_config.buy_grid_levels is not None:
            self._validate_level_prices(price, lowers, uppers)
            if any(self._price(raw) <= new_config.stop_loss_price for raw in lowers):
                raise TradingHalt(
                    "Trailing stop would remove requested BUY levels from the exact grid."
                )
        if not any(self._price(raw) > new_config.stop_loss_price for raw in lowers):
            raise TradingHalt("Trailing stop leaves no safe BUY grid level.")
        seed_quote = new_config.investment_quote * new_config.initial_inventory_percent / 100
        lower_quote = (new_config.investment_quote - seed_quote) / len(lowers)
        for raw in lowers:
            level_price = self._price(raw)
            if level_price <= new_config.stop_loss_price:
                continue
            self._check_order_size(level_price, self._amount(lower_quote / level_price))
        upper_total = carry_amount if carry_amount > 0 else self._amount(seed_quote / price)
        upper_amount = self._amount(upper_total * (1 - SELL_AMOUNT_BUFFER) / len(uppers))
        for raw in uppers:
            self._check_order_size(self._price(raw), upper_amount)
        needed_quote = new_config.investment_quote if carry_amount == 0 else (
            new_config.investment_quote - seed_quote)
        if self._free_balance(self.market["quote"]) < needed_quote:
            raise TradingHalt("Insufficient free quote for the replacement grid.")
        return lowers, uppers

    def reset_grid(self) -> bool:
        with self._cycle_lock:
            return self._reset_grid_locked()

    def _reset_grid_locked(self) -> bool:
        """Process the persisted reset before an ordinary trading cycle."""
        request_text = self.database.get_state("grid_reset")
        if request_text is None:
            self.pending_grid_bounds = None
            self.grid_needs_reset = False
            return False
        request = json.loads(request_text)
        if self.stop_controller.stop_requested.is_set():
            raise TradingHalt("Stop requested during grid reset.")
        if request["phase"] == "canceling":
            if (request.get("source") == "manual_recenter" and
                    "stop_loss_price" in request and
                    Decimal(request["stop_loss_price"]) < self.config.stop_loss_price):
                self.database.clear_state("grid_reset")
                self.pending_grid_bounds = None
                self.grid_needs_reset = False
                LOGGER.warning("Queued manual recenter withdrawn: trailing floor rose.")
                return False
            if request.get("source") == "breakout":
                if self._ticker_price() <= Decimal(request["previous_upper"]):
                    self.database.clear_state("grid_reset")
                    self.pending_grid_bounds = None
                    self.grid_needs_reset = False
                    LOGGER.info("Breakout faded before cancellation; grid reset withdrawn.")
                    return False
            if request.get("source") == "manual_recenter":
                center = Decimal(request["center_price"])
                candidate = replace(
                    self.config,
                    lower_price=Decimal(request["lower"]),
                    upper_price=Decimal(request["upper"]),
                    buy_grid_levels=request.get("buy_levels", self.config.buy_grid_levels),
                    sell_grid_levels=request.get("sell_levels", self.config.sell_grid_levels),
                )
                buys, sells = configured_grid_levels(center, candidate)
                ratio = max(center / buys[0], sells[0] / center)
                if not center / ratio < self._ticker_price() < center * ratio:
                    self.database.clear_state("grid_reset")
                    self.pending_grid_bounds = None
                    self.grid_needs_reset = False
                    LOGGER.info("Manual recenter withdrawn because market moved away.")
                    return False
            _, complete = self._cancel_bot_orders()
            if not complete or self.database.fetch_active_grids():
                raise TradingHalt("Old grid cancellation could not be verified.")
            carry_amount, carry_cost = self._carry_inventory()
            price = self._ticker_price()
            self.advance_trailing_stop(price)
            if price < self.config.stop_loss_price:
                self._liquidate_locked(price)
                return False
            if request.get("source") == "breakout":
                if price <= Decimal(request["previous_upper"]):
                    if not self.config.lower_price < price < self.config.upper_price:
                        raise TradingHalt(
                            "Breakout faded outside old bounds after cancellation; "
                            "inspect orders before restart."
                        )
                    request["source"] = "breakout_faded"
                    request["lower"] = str(self.config.lower_price)
                    request["upper"] = str(self.config.upper_price)
                    LOGGER.warning("Breakout faded during cancellation; rebuilding old bounds.")
                else:
                    width = Decimal(request["width_percent"]) / 100
                    request["lower"] = str(price * (1 - width))
                    request["upper"] = str(price * (1 + width))
            anchor = price
            if request.get("source") == "manual_recenter":
                center = Decimal(request["center_price"])
                candidate = replace(
                    self.config,
                    lower_price=Decimal(request["lower"]),
                    upper_price=Decimal(request["upper"]),
                    buy_grid_levels=request.get("buy_levels", self.config.buy_grid_levels),
                    sell_grid_levels=request.get("sell_levels", self.config.sell_grid_levels),
                )
                buys, sells = configured_grid_levels(center, candidate)
                ratio = max(center / buys[0], sells[0] / center)
                if center / ratio < price < center * ratio:
                    anchor = center
                elif self.config.lower_price < price < self.config.upper_price:
                    request["source"] = "manual_stale"
                    request["lower"] = str(self.config.lower_price)
                    request["upper"] = str(self.config.upper_price)
                    request["investment_quote"] = str(self.config.investment_quote)
                    if self.config.buy_grid_levels is None:
                        request.pop("buy_levels", None)
                        request.pop("sell_levels", None)
                    else:
                        request["buy_levels"] = self.config.buy_grid_levels
                        request["sell_levels"] = self.config.sell_grid_levels
                    LOGGER.warning("Manual recenter drifted during cancellation; rebuilding old bounds.")
                else:
                    raise TradingHalt(
                        "Market left both grids during cancellation; inspect orders before restart."
                    )
            stop_loss = self.config.stop_loss_price
            if request.get("source") == "manual_recenter" and "stop_loss_price" in request:
                stop_loss = Decimal(request["stop_loss_price"])
                if stop_loss < self.config.stop_loss_price:
                    raise TradingHalt(RISK_OVERRIDE_DENIED)
            new_config = replace(self.config, lower_price=Decimal(request["lower"]),
                                 upper_price=Decimal(request["upper"]),
                                 spacing_percent=Decimal(request["spacing"]),
                                 investment_quote=Decimal(request.get(
                                     "investment_quote", str(self.config.investment_quote))),
                                 buy_grid_levels=request.get(
                                     "buy_levels", self.config.buy_grid_levels),
                                 sell_grid_levels=request.get(
                                     "sell_levels", self.config.sell_grid_levels),
                                 stop_loss_price=stop_loss)
            if new_config.stop_loss_price >= anchor:
                raise TradingHalt("New grid anchor is at or below the hard stop.")
            if not new_config.lower_price < price < new_config.upper_price:
                raise TradingHalt("Price left the requested bounds during reset.")
            lowers, uppers = self._validate_reset_grid(new_config, anchor, carry_amount)
            baseline = self._free_balance(self.market["base"]) - carry_amount
            if baseline < 0:
                raise TradingHalt("Carried BTC exceeds the account balance.")
            state = {"anchor": str(anchor), "baseline_base": str(baseline),
                     "fingerprint": new_config.fingerprint()}
            active = {"lower": request["lower"], "upper": request["upper"],
                      "spacing": request["spacing"],
                      "investment_quote": str(new_config.investment_quote),
                      "stop_loss_price": str(new_config.stop_loss_price)}
            if new_config.buy_grid_levels is not None:
                active["buy_levels"] = new_config.buy_grid_levels
                active["sell_levels"] = new_config.sell_grid_levels
            trailing = None
            if request.get("source") == "manual_recenter":
                trailing = {
                    "high_water_mark": str(anchor),
                    "stop_loss_distance": str(anchor - new_config.stop_loss_price),
                }
            request["phase"] = "placing"
            carry_id = "carry-" + uuid.uuid4().hex if carry_amount > 0 else None
            self.database.complete_grid_reset(
                json.dumps(state, sort_keys=True), json.dumps(active),
                json.dumps(request), carry_order_id=carry_id,
                carry_price=str(carry_cost / carry_amount) if carry_id else None,
                carry_amount=str(carry_amount) if carry_id else None,
                carry_cost=str(carry_cost) if carry_id else None,
                breakout_width_percent=(request["width_percent"]
                                        if request.get("source") == "manual_recenter"
                                        else None),
                trailing_stop=(json.dumps(trailing, sort_keys=True)
                               if trailing is not None else None),
            )
            with self._grid_lock:
                self.config = new_config
                self.anchor = anchor
                self.baseline_base = baseline
                self.levels, self.upper_levels = lowers, uppers
                if request.get("source") == "manual_recenter":
                    self.breakout_width_percent = Decimal(request["width_percent"])
                    self.high_water_mark = anchor
                    self.stop_loss_distance = anchor - new_config.stop_loss_price
        elif request["phase"] != "placing":
            raise TradingHalt("Unknown grid reset phase.")
        for _ in range(3):
            self.run_cycle()
            if self._post_only_rejected_in_cycle:
                return False
            latest = self.database.fetch_latest_orders_by_level()
            safe_buys = {
                level for level, raw in enumerate(self.levels, start=1)
                if self._price(raw) > self.config.stop_loss_price
            }
            expected = safe_buys | set(
                range(-1, -len(self.upper_levels) - 1, -1)) | {0}
            if expected <= latest.keys():
                notification_key = (
                    BREAKOUT_NOTICE_KEY if request.get("source") == "breakout"
                    else "grid_reset_notification_pending"
                )
                self.database.finish_grid_reset(notification_key)
                self.pending_grid_bounds = None
                self.grid_needs_reset = False
                return True
        raise TradingHalt("Replacement grid did not finish placing orders.")

    def _save_liquidation(self, state: Dict[str, Any]) -> None:
        self.database.set_state(LIQUIDATION_KEY, json.dumps(state, sort_keys=True))

    def _liquidation_order(self, client_id: str) -> Optional[Dict[str, Any]]:
        try:
            order = self._call(self.exchange.fetch_order, "", self.config.symbol,
                               {"origClientOrderId": client_id})
        except ccxt.OrderNotFound:
            return None
        if not isinstance(order, dict) or not order.get("id"):
            raise TradingHalt("Hard-stop order lookup returned no exchange order ID.")
        return order

    def _submit_hard_stop_sell(self, state: Dict[str, Any]) -> bool:
        """Resolve the previous client ID and balance before any repeat submission."""
        client_id = state["client_order_id"]
        if state["attempts"]:
            found = self._liquidation_order(client_id)
            if found is not None:
                state["exchange_order_id"] = str(found["id"])
                state["phase"] = "confirming"
                self._save_liquidation(state)
                return False
            if state.get("retry_allowed"):
                time.sleep(0.3)
                found = self._liquidation_order(client_id)
                if found is not None:
                    state["exchange_order_id"] = str(found["id"])
                    state["phase"] = "confirming"
                    self._save_liquidation(state)
                    return False
            if (not state.get("retry_allowed") or
                    self._available_base_for_liquidation() !=
                    Decimal(state["pre_submit_base_free"])):
                raise TradingHalt(
                    "Market-sell outcome is uncertain; inspect the client order ID "
                    "and BTC balance before any further sale."
                )
        remaining = Decimal(state["held_base"]) - Decimal(state.get("sold_base", "0"))
        amount, available = self._liquidation_sell_amount(state, remaining)
        if amount <= 0:
            self._complete_hard_stop(
                state, Decimal(state.get("sold_base", "0")),
                Decimal(state.get("proceeds_quote", "0")),
            )
            return True
        state["sell_amount"] = str(amount)
        state["pre_submit_base_free"] = str(available)
        state["attempts"] += 1
        state["retry_allowed"] = True  # Recover an interrupted request after restart.
        self._save_liquidation(state)  # Durable intent before the network request.
        try:
            response = self._call(
                self.exchange.create_market_sell_order,
                self.config.symbol, float(Decimal(state["sell_amount"])),
                {"newClientOrderId": client_id},
            )
        except (ccxt.NetworkError, ccxt.ExchangeError):
            self._save_liquidation(state)
            raise
        if not isinstance(response, dict) or not response.get("id"):
            raise ccxt.ExchangeNotAvailable(
                "Market SELL returned no exchange ID; verifying its client ID."
            )
        state["exchange_order_id"] = str(response["id"])
        state["phase"] = "confirming"
        self._save_liquidation(state)
        return False

    def _complete_hard_stop(self, state: Dict[str, Any], sold: Decimal,
                            proceeds: Decimal = Decimal(0)) -> None:
        residual = Decimal(state["held_base"]) - sold
        if residual < 0:
            raise TradingHalt("Hard-stop SELL exceeded bot-tracked BTC.")
        if residual > 0 and self._sellable_hard_stop_amount(
                residual, self._ticker_price()) > 0:
            raise TradingHalt("Market SELL left tradable bot BTC; manual review required.")
        # Keep the unsold dust in the audit record while closing active inventory.
        state.update(phase="complete", sold_base=str(sold),
                     residual_base="0", dust_base=str(residual),
                     proceeds_quote=str(proceeds))
        self._save_liquidation(state)
        self.database.set_state(SAFETY_MODE_KEY, LIQUIDATED)
        self.database.set_state("halt_reason", "hard_stop_liquidated")
        self.database.set_state(LIQUIDATION_NOTICE_KEY, "1")
        for key in ("grid_reset", BREAKOUT_TIMER_KEY, SAFETY_PAUSE_NOTICE_KEY,
                    SAFETY_RECOVERY_NOTICE_KEY, SAFETY_RESUME_NOTICE_KEY):
            self.database.clear_state(key)
        self.grid_needs_reset = False
        self.pending_grid_bounds = None
        self.is_paused = True
        LOGGER.critical(
            "Hard stop sold %s %s; unsold dust %s %s is recorded. "
            "Bot is locked until admin reset.",
            sold, self.market["base"], residual, self.market["base"],
        )

    def _sellable_hard_stop_amount(self, held: Decimal, price: Decimal) -> Decimal:
        """Apply market-order lot and notional filters before submitting a SELL."""
        def minimum(value: Any) -> Decimal:
            try:
                parsed = _order_decimal(value)
            except (InvalidOperation, TypeError, ValueError) as error:
                raise TradingHalt("Exchange market minimum is invalid.") from error
            if not parsed.is_finite() or parsed < 0:
                raise TradingHalt("Exchange market minimum is invalid.")
            return parsed

        amount = self._amount(held)
        if amount <= 0:
            return Decimal(0)
        limits = self.market.get("limits") or {}
        amount_min = minimum((limits.get("amount") or {}).get("min"))
        cost_min = max(Decimal("10"), minimum((limits.get("cost") or {}).get("min")))
        info = self.market.get("info") or {}
        filters = (info.get("filters") or []) if isinstance(info, dict) else []
        for rule in filters:
            if not isinstance(rule, dict):
                continue
            kind = rule.get("filterType")
            if kind == "MARKET_LOT_SIZE":
                amount_min = max(amount_min, minimum(rule.get("minQty")))
                step = minimum(rule.get("stepSize"))
                if step > 0:
                    amount = self._amount((amount // step) * step)
            elif kind == "MIN_NOTIONAL" and rule.get("applyToMarket") in (True, "true"):
                cost_min = max(cost_min, minimum(rule.get("minNotional")))
            elif kind == "NOTIONAL" and rule.get("applyMinToMarket") in (True, "true"):
                cost_min = max(cost_min, minimum(rule.get("minNotional")))
        if amount < amount_min or price * amount < cost_min:
            return Decimal(0)
        return amount

    def _available_base_for_liquidation(self) -> Decimal:
        """Read an explicit, fresh Spot free balance; missing data is not zero."""
        balances = self._call(self.exchange.fetch_balance, {"type": "spot"})
        asset = balances.get(self.market["base"]) if isinstance(balances, dict) else None
        free = asset.get("free") if isinstance(asset, dict) else None
        try:
            available = Decimal(str(free))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise TradingHalt("Exchange BTC free balance is unavailable.") from error
        if not available.is_finite() or available < 0:
            raise TradingHalt("Exchange BTC free balance is invalid.")
        return available

    def _liquidation_sell_amount(
        self, state: Dict[str, Any], tracked: Decimal,
    ) -> Tuple[Decimal, Decimal]:
        """Cap each sale at actual free BTC; never hide a material shortfall as dust."""
        if tracked < 0:
            raise TradingHalt("Liquidation ledger has negative remaining BTC.")
        available = self._available_base_for_liquidation()
        price = self._ticker_price()
        safe_amount = min(tracked, available)
        amount = self._sellable_hard_stop_amount(safe_amount, price)
        if amount <= 0 and tracked > 0:
            if self._sellable_hard_stop_amount(tracked, price) > 0:
                state["unavailable_base"] = str(tracked - safe_amount)
                self._save_liquidation(state)
                raise TradingHalt(
                    "Bot inventory exceeds sellable free BTC; liquidation needs review."
                )
            LOGGER.warning(
                "Sellable inventory is below Binance dust/notional limits. "
                "Bypassing execution."
            )
        return amount, available

    def _liquidation_step(self, state: Dict[str, Any]) -> bool:
        """Advance one durable phase; never submit a second sell before reconciliation."""
        if state["phase"] == "canceling":
            _, complete = self._cancel_bot_orders()
            if not complete or self.database.fetch_active_grids():
                return False
            held, cost = self._carry_inventory(require_available=False)
            state.update(held_base=str(held), cost_basis_quote=str(cost),
                         sold_base="0", proceeds_quote="0")
            amount, available = self._liquidation_sell_amount(state, held)
            state["sell_amount"] = str(amount)
            if amount <= 0:
                self._complete_hard_stop(state, Decimal(0))
                return True
            state.update(phase="submitting", client_order_id=self.order_client_prefix + uuid.uuid4().hex[:20],
                         pre_submit_base_free=str(available),
                         attempts=0, retry_allowed=False)
            self._save_liquidation(state)
        if state["phase"] == "submitting":
            if self._submit_hard_stop_sell(state):
                return True
        if state["phase"] == "confirming":
            if state.get("confirmed_order_id") == state["client_order_id"]:
                total_sold = Decimal(state["confirmed_sold_base"])
                total_proceeds = Decimal(state["confirmed_proceeds_quote"])
            else:
                order = self._liquidation_order(state["client_order_id"])
                if order is None or order.get("status") == "open":
                    return False
                if order.get("status") not in ("closed", "canceled", "expired"):
                    raise TradingHalt("Hard-stop SELL has an unknown exchange status.")
                if order.get("filled") is None:
                    raise TradingHalt("Hard-stop SELL has no confirmed filled quantity.")
                sold = _order_decimal(order.get("filled"))
                total_sold = Decimal(state.get("sold_base", "0")) + sold
                if total_sold > Decimal(state["held_base"]):
                    raise TradingHalt("Hard-stop SELL exceeded bot-tracked BTC.")
                proceeds = _order_decimal(order.get("cost"),
                                          str(sold * _order_decimal(order.get("average"))))
                if sold > 0 and proceeds > 0:
                    basis = (Decimal(state["cost_basis_quote"]) * sold /
                             Decimal(state["held_base"]))
                    profit = proceeds - _fees_in_asset(order, self.market["quote"]) - basis
                    self.database.record_trade(
                        basis / sold, proceeds / sold, profit,
                        sell_order_id=state["client_order_id"],
                    )
                total_proceeds = Decimal(state.get("proceeds_quote", "0")) + proceeds
                state.update(confirmed_order_id=state["client_order_id"],
                             confirmed_sold_base=str(total_sold),
                             confirmed_proceeds_quote=str(total_proceeds))
                self._save_liquidation(state)
            residual = Decimal(state["held_base"]) - total_sold
            amount, available = self._liquidation_sell_amount(state, residual)
            if amount <= 0:
                self._complete_hard_stop(state, total_sold, total_proceeds)
                return True
            state.update(phase="submitting", sold_base=str(total_sold),
                         proceeds_quote=str(total_proceeds), sell_amount=str(amount),
                         client_order_id=self.order_client_prefix + uuid.uuid4().hex[:20],
                         pre_submit_base_free=str(available),
                         attempts=0, retry_allowed=False)
            for key in ("confirmed_order_id", "confirmed_sold_base",
                        "confirmed_proceeds_quote"):
                state.pop(key, None)
            self._save_liquidation(state)
            return False
        raise TradingHalt("Unknown hard-stop liquidation phase.")

    def _liquidate_locked(self, price: Decimal) -> None:
        """Cancel, verify, sell only bot inventory, and persist a terminal state."""
        state_text = self.database.get_state(LIQUIDATION_KEY)
        state = json.loads(state_text) if state_text else {
            "phase": "canceling", "trigger_price": str(price),
        }
        if state_text is None:
            self._save_liquidation(state)
            self.database.set_state(SAFETY_MODE_KEY, LIQUIDATING)
            self.is_paused = True
            LOGGER.critical("Hard stop triggered at %s %s; liquidating bot inventory.",
                            price, self.config.symbol)
        if state["phase"] == "complete":
            self._complete_hard_stop(state, Decimal(state["sold_base"]),
                                     Decimal(state.get("proceeds_quote", "0")))
            return
        if state["phase"] == "failed":
            self.database.set_state(SAFETY_MODE_KEY, LIQUIDATION_HALTED)
            self.database.set_state("halt_reason", "hard_stop_failed")
            self.database.set_state(LIQUIDATION_NOTICE_KEY, "1")
            self.is_paused = True
            return
        permanent_errors = (ccxt.InsufficientFunds, ccxt.InvalidOrder,
                            ccxt.AuthenticationError, ccxt.PermissionDenied,
                            ccxt.BadRequest)
        while True:
            retry_delay = 2
            try:
                if self._liquidation_step(state):
                    return
            except permanent_errors as error:
                self._fail_liquidation(state, error)
                return
            except (ccxt.NetworkError, ccxt.ExchangeError) as error:
                if isinstance(error, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
                    retry_delay = 60  # Avoid escalating Binance's rate-limit ban.
                LOGGER.critical("Hard-stop liquidation exchange failure (%s); retrying in %ss.",
                                type(error).__name__, retry_delay)
            except (TradingHalt, ValueError, KeyError, TypeError) as error:
                self._fail_liquidation(state, error)
                return
            time.sleep(retry_delay)

    def _fail_liquidation(self, state: Dict[str, Any], error: Exception) -> None:
        if state.get("confirmed_order_id") == state.get("client_order_id"):
            state["sold_base"] = state["confirmed_sold_base"]
            state["proceeds_quote"] = state["confirmed_proceeds_quote"]
        state["phase"] = "failed"
        self._save_liquidation(state)
        self.database.set_state(SAFETY_MODE_KEY, LIQUIDATION_HALTED)
        self.database.set_state("halt_reason", "hard_stop_failed")
        self.database.set_state(LIQUIDATION_NOTICE_KEY, "1")
        self.is_paused = True
        LOGGER.critical("Hard-stop liquidation halted: %s", type(error).__name__,
                        exc_info=True)

    def liquidate(self, price: Decimal) -> None:
        with self._cycle_lock:
            self._liquidate_locked(price)

    def set_manual_pause(self, active: bool) -> Optional[str]:
        """Persist a manual pause and use the existing targeted BUY cancellation."""
        with self._cycle_lock:
            if self.stop_controller.stop_requested.is_set() or self.grid_needs_reset:
                raise TradingHalt("Bot is stopping or resetting its grid.")
            mode = self.database.get_state(SAFETY_MODE_KEY)
            if mode in (LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED):
                raise TradingHalt("Hard-stop liquidation has locked trading controls.")
            if active:
                if mode != PAUSED_MANUAL:
                    self.database.set_state(SAFETY_MODE_KEY, PAUSED_MANUAL)
                    self.database.clear_state(SAFETY_RECOVERY_NOTICE_KEY)
                    self.database.clear_state(SAFETY_RESUME_NOTICE_KEY)
                    if mode is None:
                        self.database.set_state(SAFETY_PAUSE_NOTICE_KEY, "1")
                self.is_paused = True
                self.database.clear_state(BREAKOUT_TIMER_KEY)
                _, complete = self._cancel_buy_orders_for_pause()
                if not complete:
                    raise TradingHalt("Some BUY orders remain open; cancellation will retry.")
                return PAUSED_MANUAL

            if mode != PAUSED_MANUAL:
                return mode
            price = self._ticker_price()
            if price <= self.config.lower_price:
                # The automatic downside guard still owns the pause.
                self.database.set_state(SAFETY_MODE_KEY, PAUSED_DOWNSIDE)
                self.is_paused = True
                return PAUSED_DOWNSIDE
            self.database.clear_state(SAFETY_MODE_KEY)
            self.database.clear_state(SAFETY_PAUSE_NOTICE_KEY)
            self.is_paused = False
            return None

    def _cancel_buy_orders_for_pause(
        self,
    ) -> Tuple[List[Tuple[str, Tuple[str, ...]]], bool]:
        """Cancel only bot-owned BUY orders while leaving every SELL active."""
        return self._cancel_bot_orders(
            side="buy", marker_prefix="safety_pause_buy", limit_only=True
        )

    def _missing_paused_buys(self) -> bool:
        return any(
            row["side"] == "BUY" and row["status"] == "CANCELED" and
            self.database.get_state(f"safety_pause_buy:{row['order_id']}") == "1"
            for row in self.database.fetch_latest_orders_by_level().values()
        )

    def _cancel_buys_below_stop(self) -> None:
        """Remove tracked BUY limits that a raised hard stop has overtaken."""
        affected = [
            row for row in self.database.fetch_active_grids()
            if row["side"] == "BUY" and row["order_type"] == "LIMIT"
            and Decimal(row["price"]) <= self.config.stop_loss_price
        ]
        if not affected:
            return
        ids = {row["order_id"] for row in affected}
        for row in affected:
            self.database.set_state(f"trailing_stop_buy:{row['order_id']}", "1")
        _, complete = self._cancel_bot_orders(
            side="buy", selected_ids=ids, marker_prefix="trailing_stop_buy"
        )
        if not complete or any(row["order_id"] in ids
                               for row in self.database.fetch_active_grids()):
            raise TradingHalt("BUY orders below the trailing stop need reconciliation.")

    def run_cycle(self) -> List[Tuple[str, Tuple[str, ...]]]:
        with self._cycle_lock:
            return self._run_cycle_locked()

    def _run_cycle_locked(self) -> List[Tuple[str, Tuple[str, ...]]]:
        if not self.levels:
            raise RuntimeError("Call prepare() before run_cycle().")
        self._post_only_rejected_in_cycle = False
        current_price = self._ticker_price()
        self.database.set_state(LAST_MARKET_PRICE_KEY, str(current_price))
        mode = self.database.get_state(SAFETY_MODE_KEY)
        if mode in (LIQUIDATED, LIQUIDATION_HALTED):
            return []
        if mode != LIQUIDATING:
            self.advance_trailing_stop(current_price)
        if mode == LIQUIDATING or current_price < self.config.stop_loss_price:
            self._liquidate_locked(current_price)
            return []
        self._cancel_buys_below_stop()
        if self._observe_upper_breakout(current_price):
            return []
        events: List[Tuple[str, Tuple[str, ...]]] = []
        if self.is_paused:
            pause_events, cancellations_complete = self._cancel_buy_orders_for_pause()
            events.extend(pause_events)
            if (cancellations_complete and current_price > self.config.lower_price
                    and self.database.get_state(SAFETY_MODE_KEY) == PAUSED_DOWNSIDE):
                self.database.clear_state(SAFETY_MODE_KEY)
                self.database.clear_state(SAFETY_PAUSE_NOTICE_KEY)
                self.database.set_state(SAFETY_RECOVERY_NOTICE_KEY, "1")
                self.database.set_state(SAFETY_RESUME_NOTICE_KEY, "1")
                self.is_paused = False
                LOGGER.info("Safety pause lifted after recovery to %s %s.",
                            current_price, self.config.symbol)
        for row in self.database.fetch_active_grids():
            if self.stop_controller.stop_requested.is_set():
                return events
            event = self._reconcile_order(row)
            if event:
                events.append(event)

        latest = self.database.fetch_latest_orders_by_level()
        seed_row = latest.get(0)
        if seed_row is None:
            if not self.is_paused:
                self._place_seed_buy(current_price)
            return events
        if seed_row["status"] not in ("FILLED",):
            seed_row = self.database.get_order(seed_row["order_id"])
        if seed_row["status"] != "FILLED":
            return events

        latest = self.database.fetch_latest_orders_by_level()
        in_bounds = (
            not self.is_paused and
            self.config.lower_price <= current_price <= self.config.upper_price
        )
        lane_levels = (
            list(range(-1, -len(self.upper_levels) - 1, -1))
            + list(range(1, len(self.levels) + 1))
        )
        for level in lane_levels:
            if self.stop_controller.stop_requested.is_set():
                break
            row = latest.get(level)
            if row is None:
                if level < 0:
                    self._place_seed_upper_sell(level, seed_row)
                elif in_bounds:
                    self._place_buy(level, None)
            elif row["status"] in ("OPEN", "PARTIALLY_FILLED"):
                continue
            elif row["status"] == "FILLED" and row["side"] == "BUY":
                self._place_sell(row)
            elif row["status"] == "FILLED" and row["side"] == "SELL":
                self._record_filled_sell(row)
                if in_bounds:
                    self._place_buy(level, row["order_id"])
            elif (row["status"] == "CANCELED" and row["side"] == "BUY" and
                  (self.database.get_state(
                      f"safety_pause_buy:{row['order_id']}") == "1" or
                   self.database.get_state(
                      f"trailing_stop_buy:{row['order_id']}") == "1")):
                canceled_buy = self._fetch_order(row)
                if _order_decimal(canceled_buy.get("filled")) > 0:
                    self._place_sell(row)
                elif in_bounds and Decimal(row["price"]) < current_price:
                    self._place_buy(level, row.get("parent_order_id"))
            else:
                raise TradingHalt("A grid lane ended unexpectedly; inspect local orders.")
        return events

    async def _notify_safety_state(self, notifier: TelegramNotifier) -> None:
        if self.database.get_state(SAFETY_PAUSE_NOTICE_KEY):
            try:
                await notifier.notify_safety_pause()
            except Exception as error:
                LOGGER.warning("Safety pause Telegram update failed: %s",
                               type(error).__name__)
            else:
                self.database.clear_state(SAFETY_PAUSE_NOTICE_KEY)
        missing_buys = self._missing_paused_buys() if not self.is_paused else False
        if (not self.is_paused and missing_buys and
                self.database.get_state(SAFETY_RECOVERY_NOTICE_KEY)):
            try:
                await notifier.notify_safety_recovery()
            except Exception as error:
                LOGGER.warning("Safety recovery Telegram update failed: %s",
                               type(error).__name__)
            else:
                self.database.clear_state(SAFETY_RECOVERY_NOTICE_KEY)
        if (not self.is_paused and
                self.database.get_state(SAFETY_RESUME_NOTICE_KEY) and
                not missing_buys):
            try:
                await notifier.notify_safety_resume()
            except Exception as error:
                LOGGER.warning("Safety resume Telegram update failed: %s",
                               type(error).__name__)
            else:
                self.database.clear_state(SAFETY_RECOVERY_NOTICE_KEY)
                self.database.clear_state(SAFETY_RESUME_NOTICE_KEY)

    async def _notify_liquidation(self, notifier: TelegramNotifier) -> None:
        if not self.database.get_state(LIQUIDATION_NOTICE_KEY):
            return
        mode = self.database.get_state(SAFETY_MODE_KEY)
        try:
            await notifier.notify_hard_stop(mode == LIQUIDATED)
        except Exception as error:
            LOGGER.warning("Hard-stop Telegram alert failed: %s", type(error).__name__)
        else:
            self.database.clear_state(LIQUIDATION_NOTICE_KEY)

    async def run(self, notifier: TelegramNotifier) -> None:
        await asyncio.to_thread(self.prepare, persist=True)
        if (self.database.get_state("halt_reason") and
                self.database.get_state(SAFETY_MODE_KEY) not in
                (LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED)):
            raise TradingHalt("Saved stop state requires manual review before a new run.")
        await asyncio.to_thread(self._backfill_portfolio_snapshots)
        try:
            if self.database.get_state(SAFETY_MODE_KEY) not in (
                    LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED):
                try:
                    await notifier.notify_startup(self.config.symbol)
                except Exception as error:
                    LOGGER.warning("Startup alert failed: %s", type(error).__name__)
            backoff = 1
            while True:
                if self.stop_controller.stop_requested.is_set():
                    break
                try:
                    mode = self.database.get_state(SAFETY_MODE_KEY)
                    if mode in (LIQUIDATED, LIQUIDATION_HALTED):
                        await self._notify_liquidation(notifier)
                        await asyncio.to_thread(
                            self.stop_controller.stop_requested.wait,
                            self.config.poll_seconds,
                        )
                        continue
                    if mode == LIQUIDATING:
                        price = await asyncio.to_thread(self._ticker_price)
                        await asyncio.to_thread(self.liquidate, price)
                        await self._notify_liquidation(notifier)
                        continue
                    if self.grid_needs_reset:
                        price = await asyncio.to_thread(self._ticker_price)
                        await asyncio.to_thread(self.advance_trailing_stop, price)
                        if price < self.config.stop_loss_price:
                            await asyncio.to_thread(self.liquidate, price)
                            await self._notify_liquidation(notifier)
                            continue
                        complete = await asyncio.to_thread(self.reset_grid)
                        await self._notify_safety_state(notifier)
                        backoff = 1
                        if not complete:
                            await asyncio.to_thread(
                                self.stop_controller.stop_requested.wait,
                                self.config.poll_seconds,
                            )
                        continue
                    if self.database.get_state("grid_reset_notification_pending"):
                        try:
                            await notifier.notify_grid_reset()
                        except Exception:
                            LOGGER.warning("Grid reset notification could not be delivered.")
                        else:
                            self.database.clear_state("grid_reset_notification_pending")
                    if self.database.get_state(BREAKOUT_NOTICE_KEY):
                        try:
                            await notifier.notify_breakout_shift()
                        except Exception as error:
                            LOGGER.warning("Breakout Telegram update failed: %s",
                                           type(error).__name__)
                        else:
                            self.database.clear_state(BREAKOUT_NOTICE_KEY)
                    events = await asyncio.to_thread(self.run_cycle)
                    await self._notify_liquidation(notifier)
                    for kind, values in events:
                        if kind == "filled":
                            try:
                                await notifier.notify_order_filled(*values)
                            except Exception as error:
                                LOGGER.warning("Order fill alert failed: %s", type(error).__name__)
                    await self._notify_safety_state(notifier)
                    backoff = 1
                    delay = 0 if self.grid_needs_reset else self.config.poll_seconds
                except (ccxt.RateLimitExceeded, ccxt.DDoSProtection, ccxt.NetworkError):
                    if self.is_paused:
                        await self._notify_safety_state(notifier)
                    delay = min(backoff, 60)
                    backoff = min(backoff * 2, 60)
                except Exception as error:
                    self.stop_controller.stop_requested.set()
                    if self.grid_needs_reset:
                        self.database.set_state("halt_reason", "grid_reset_failed")
                    if self.is_paused:
                        await self._notify_safety_state(notifier)
                    try:
                        await notifier.notify_critical_error(error)
                    except Exception:
                        pass
                    if not self.is_paused:
                        await asyncio.to_thread(self.stop_controller.request_stop)
                    raise
                await asyncio.to_thread(self.stop_controller.stop_requested.wait, delay)
        finally:
            if not self.stop_controller.stop_requested.is_set() and not self.is_paused:
                await asyncio.to_thread(self.stop_controller.request_stop)


def _credentials() -> Tuple[str, str]:
    load_dotenv(dotenv_path=BASE_DIR / ".env")
    key = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
    secret = os.getenv("BINANCE_TESTNET_API_SECRET", "").strip()
    if not key or not secret:
        raise ValueError("Set both Binance Spot Testnet keys in .env.")
    return key, secret


def _center_config_at_current_price(exchange: Any, database: GridDatabase) -> None:
    """Center bounds once per new run; preserve them on restart."""
    if database.get_state("grid_run") is not None:
        return
    path = config_path()
    raw = load_config(path)
    percent_value = raw["grid"].get("auto_center_percent")
    if percent_value is None:
        return
    percent = _decimal(percent_value, "auto_center_percent")
    if percent >= 100:
        raise ValueError("grid.auto_center_percent must be below 100.")
    ticker = exchange.fetch_ticker(raw["grid"]["symbol"])
    price = _order_decimal(ticker.get("last"))
    if not price.is_finite() or price <= 0:
        raise TradingHalt("Cannot center bounds without a valid Testnet price.")
    raw["grid"]["lower_price"] = float(price * (1 - percent / 100))
    raw["grid"]["upper_price"] = float(price * (1 + percent / 100))
    temporary_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(raw, file, indent=2)
            file.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    print(
        f"Centered bounds on Spot Testnet price {price}: "
        f"{raw['grid']['lower_price']} to {raw['grid']['upper_price']}"
    )


def _apply_active_grid_config(config: GridConfig, database: GridDatabase) -> GridConfig:
    active_config_text = database.get_state("active_grid_config")
    if not active_config_text:
        return config
    active = json.loads(active_config_text)
    return replace(
        config,
        lower_price=Decimal(active["lower"]),
        upper_price=Decimal(active["upper"]),
        spacing_percent=Decimal(active["spacing"]),
        investment_quote=Decimal(active.get("investment_quote", str(config.investment_quote))),
        buy_grid_levels=active.get("buy_levels", config.buy_grid_levels),
        sell_grid_levels=active.get("sell_levels", config.sell_grid_levels),
        stop_loss_price=Decimal(
            active.get("stop_loss_price", str(config.stop_loss_price))
        ),
    )


async def _run_services(bot: GridBot, notifier: TelegramNotifier) -> None:
    """Run the local API and trading loop with outbound alerts only."""
    app.state.grid_bot = bot
    app.state.atr_snapshot = None
    app.state.order_book_snapshot = None
    server = uvicorn.Server(uvicorn.Config(
        app, host=os.getenv("BOT_API_HOST", "127.0.0.1"),
        port=8000, log_level="warning",
    ))

    async def serve_api() -> None:
        try:
            await server.serve()
        except SystemExit as error:
            # Uvicorn exits this way when its listening port is unavailable.
            LOGGER.error("Status API could not start (exit %s).", error.code)
        except Exception as error:
            LOGGER.error("Status API stopped: %s", type(error).__name__)
        else:
            if not server.should_exit:
                LOGGER.error("Status API stopped while the trading bot is running.")

    api_task = asyncio.create_task(serve_api(), name="status-api")
    market_data_task = asyncio.create_task(
        _refresh_market_data(bot), name="market-intelligence"
    )
    try:
        await bot.run(notifier)
    finally:
        market_data_task.cancel()
        await asyncio.gather(market_data_task, return_exceptions=True)
        server.should_exit = True
        try:
            await asyncio.wait_for(
                asyncio.gather(api_task, return_exceptions=True), timeout=10
            )
        except asyncio.TimeoutError:
            api_task.cancel()
            await asyncio.gather(api_task, return_exceptions=True)
        finally:
            app.state.grid_bot = None
            app.state.atr_snapshot = None
            app.state.order_book_snapshot = None


def main() -> int:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    parser = argparse.ArgumentParser(description="Binance Spot Testnet geometric grid bot")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Center and preview without orders")
    mode.add_argument("--execute", action="store_true", help="Trade on Spot Testnet")
    arguments = parser.parse_args()
    try:
        key, secret = _credentials()
        exchange = create_exchange(key, secret)
        database = GridDatabase()
        _center_config_at_current_price(exchange, database)
        config = _apply_active_grid_config(GridConfig.load(), database)
        bot = GridBot(config, exchange, database)
        if arguments.check:
            current, planned, upper_planned = bot.prepare(persist=False)
            print(f"Spot Testnet {config.symbol}: current={current}, anchor={bot.anchor}")
            print(f"Seed market buy: {bot._seed_quote()} {bot.market['quote']}")
            print(f"Geometric lower buy levels: {len(planned)}")
            for level, price, amount in planned:
                print(f"  {level}: buy {amount} at {price}")
            print(f"Geometric upper sell levels: {len(upper_planned)}")
            for level, price, amount in upper_planned:
                print(f"  {level}: sell {amount} at {price}")
            print(
                "Total planned quote: "
                f"{bot._seed_quote() + sum((p * a for _, p, a in planned), Decimal(0))}"
            )
            return 0
        token, owner_chat_id = load_telegram_credentials()
        notifier = TelegramNotifier(token, owner_chat_id)
        asyncio.run(_run_services(bot, notifier))
        return 0
    except ccxt.NetworkError as error:
        print(f"Bot stopped: {error}", file=sys.stderr)
        return 1
    except (ValueError, TradingHalt, ccxt.BaseError) as error:
        print(f"Bot stopped: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Check open orders before restarting.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
