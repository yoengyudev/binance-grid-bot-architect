"""FastAPI dashboard routes, authentication, and status projections."""

import logging
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any, Dict, Optional, Tuple
import ccxt
import jwt
from fastapi import FastAPI, Depends, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from database import GridDatabase
from exchange_handler import check_exchange_circuit, create_exchange
from notifier import markdown_text, schedule_telegram_alert
from settings import (
    AUTH_COOKIE_NAME,
    ENGINE_FAULT_KEY,
    ENGINE_IDLE,
    ENGINE_RUNNING,
    ENGINE_STATUS_KEY,
    ENGINE_STOP_CLEANUP_KEY,
    FRONTEND_ORIGIN,
    GHOST_ORDER_ALARM_KEY,
    JWT_ALGORITHM,
    JWT_AUDIENCE,
    JWT_ISSUER,
    JWT_LIFETIME_SECONDS,
    JWT_REFRESH_GRACE_SECONDS,
    LAST_EXCHANGE_AUDIT_TIME_KEY,
    LIQUIDATED,
    LIQUIDATING,
    LIQUIDATION_ALARM_KEY,
    LIQUIDATION_HALTED,
    MANUAL_ACCOUNT_EXIT_REASON,
    MOCK_ADMIN_PASSWORD,
    ORDER_CLEANUP_ALARM_KEY,
    PAUSED_RESET,
    PAUSED_SIZING,
    RISK_OVERRIDE_DENIED,
    SAFETY_MODE_KEY,
    SIZING_PAUSE_REASON_KEY,
    TRADING_SETTINGS,
    TRAILING_STOP_KEY,
    _credentials,
)
from models import (
    EngineStartRequest,
    GridStopConflict,
    InsufficientGridCapital,
    LoginRequest,
    PauseRequest,
    RecenterRequest,
    StopLossRequest,
    TradingHalt,
    _order_decimal,
)
from portfolio import (
    _portfolio_wallet,
    _realized_pnl_payload,
    _unavailable_wallet,
)
from engine import (
    GridBot,
)

LOGGER = logging.getLogger(__name__)


app = FastAPI(title="Grid Bot Status API")


app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.state.grid_bot = None

app.state.monitor_only = False

app.state.standby = False

app.state.ready = False

app.state.atr_snapshot = None

app.state.order_book_snapshot = None

app.state.preview_jwt_secret = secrets.token_urlsafe(48)


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
    if (os.getenv("BOT_AUTH_MOCK_ENABLED") == "1" and
            app.state.grid_bot is None and not app.state.monitor_only):
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


def _exchange_audit_telemetry(bot: Optional["GridBot"],
                              database: GridDatabase) -> Dict[str, Any]:
    """Report saved exchange verification and configured, not open-order, bounds."""
    config = bot.config if bot is not None else None
    lower = getattr(config, "lower_price", None)
    upper = getattr(config, "upper_price", None)
    if lower is None or upper is None:
        try:
            saved = json.loads(database.get_state("active_grid_config") or "{}")
            lower = lower if lower is not None else saved.get("lower")
            upper = upper if upper is not None else saved.get("upper")
        except (ValueError, TypeError, AttributeError):
            pass

    def bound(value: Any) -> Optional[float]:
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else None
        except (ValueError, TypeError, OverflowError):
            return None

    return {
        "last_exchange_audit_time": database.get_state(LAST_EXCHANGE_AUDIT_TIME_KEY),
        "configured_lower_bound": bound(lower),
        "configured_upper_bound": bound(upper),
    }


def _unprotected_exposure_payload(bot: Optional["GridBot"],
                                  database: Optional[GridDatabase],
                                  engine_status: str) -> Dict[str, Any]:
    """Read account BTC exposure, including manual holdings, without placing orders."""
    unavailable = {"unprotected_btc_exposure": None,
                   "has_unprotected_exposure": False}
    if bot is None and not app.state.monitor_only:
        return unavailable

    def quantity(value: Any) -> Decimal:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError("Invalid exchange exposure quantity.")
        return result

    try:
        if bot is None:
            _credentials()
            exchange = create_exchange()
            lock = RLock()
            symbol = "BTC/USDT"
        else:
            exchange = getattr(bot, "exchange", None)
            if exchange is None:
                return unavailable
            lock = bot.exchange_lock
            symbol = bot.config.symbol
        # One read-only snapshot under the exchange lock, separate from ledger P&L.
        with lock:
            check_exchange_circuit(exchange)
            exchange.load_markets()
            market = exchange.market(symbol)
            if (not isinstance(market, dict) or market.get("base") != "BTC"
                    or not market.get("spot") or market.get("active") is False):
                raise ValueError("BTC Spot market metadata is unavailable.")
            balances = exchange.fetch_balance({"type": "spot"})
            if not isinstance(balances, dict):
                raise ValueError("Spot balance is unavailable.")
            btc = balances.get("BTC") or {}
            free = btc.get("free", (balances.get("free") or {}).get("BTC"))
            used = btc.get("used", (balances.get("used") or {}).get("BTC"))
            total = quantity(free) + quantity(used)
            live_orders = exchange.fetch_open_orders()
            if not isinstance(live_orders, list):
                raise ValueError("Spot open orders are unavailable.")
            locked = bot_locked = Decimal(0)
            tracked = database.fetch_active_grids() if database is not None else []
            for order in live_orders:
                if not isinstance(order, dict):
                    raise ValueError("Invalid Spot open order.")
                # BTC can be locked in SELL limits on other quote pairs as well.
                if (not str(order.get("symbol", "")).startswith("BTC/") or
                        str(order.get("side", "")).upper() != "SELL" or
                        str(order.get("type", "")).upper() not in ("LIMIT", "LIMIT_MAKER") or
                        str(order.get("status", "open")).lower() != "open"):
                    continue
                remaining = order.get("remaining")
                if remaining is None:
                    remaining = quantity(order.get("amount")) - quantity(order.get("filled"))
                remaining = quantity(remaining)
                locked += remaining
                if bot is not None and bot._is_bot_order(order, tracked):
                    bot_locked += remaining
            uncovered = max(total - locked, Decimal(0))
            exposure = Decimal(0)
            if uncovered > 0:
                price = quantity(exchange.fetch_ticker(symbol).get("last"))
                if price <= 0:
                    raise ValueError("BTC price is unavailable.")
                limits = market.get("limits") or {}
                minimum_amount = quantity((limits.get("amount") or {}).get("min") or 0)
                minimum_cost = quantity((limits.get("cost") or {}).get("min") or 0)
                filters = (market.get("info") or {}).get("filters") or []
                if any(rule.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL") for rule in filters):
                    minimum_cost = Decimal(0)  # Raw flags determine MARKET minimums.
                steps = []
                for rule in filters:
                    kind = rule.get("filterType")
                    if kind in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                        minimum_amount = max(minimum_amount, quantity(rule.get("minQty") or 0))
                        step = quantity(rule.get("stepSize") or 0)
                        if step > 0:
                            steps.append(step)
                    elif (kind == "MIN_NOTIONAL" and rule.get("applyToMarket", True) or
                          kind == "NOTIONAL" and rule.get("applyMinToMarket", True)):
                        minimum_cost = max(minimum_cost, quantity(rule.get("minNotional") or 0))
                # Apply lot precision and dust minimums, without capping the
                # account's exposure to the maximum size of a single SELL.
                if steps:
                    scale = Decimal(10) ** max(-step.as_tuple().exponent for step in steps)
                    step = Decimal(math.lcm(*(int(step * scale) for step in steps))) / scale
                    uncovered = (uncovered // step) * step
                try:
                    tradable = quantity(exchange.amount_to_precision(symbol, str(uncovered)))
                except ccxt.InvalidOrder:
                    if uncovered >= minimum_amount and uncovered * price >= minimum_cost:
                        raise
                    tradable = Decimal(0)
                if tradable >= minimum_amount and tradable * price >= minimum_cost:
                    exposure = tradable
    except (ccxt.BaseError, TradingHalt, OSError, ValueError, TypeError,
            AttributeError, InvalidOperation) as error:
        LOGGER.warning("Spot BTC exposure unavailable: %s", type(error).__name__)
        return unavailable

    protected = Decimal(0)
    if (bot is not None and database is not None and engine_status == ENGINE_RUNNING
            and not app.state.standby and database.get_state("grid_run") is not None
            and not bot.stop_controller.stop_requested.is_set()
            and not database.get_state(ENGINE_FAULT_KEY)
            and database.get_state(ENGINE_STOP_CLEANUP_KEY) != "1"
            and not database.get_state("halt_reason")
            and database.get_state(SAFETY_MODE_KEY) not in (LIQUIDATED, LIQUIDATION_HALTED)
            and bot.config.stop_loss_price > 0):
        # Active hard/trailing stops cover bot-owned lots, not manual inventory.
        held = _portfolio_wallet(database)["btc_held"]
        if held is not None:
            protected = max(Decimal(str(held)) - bot_locked, Decimal(0))
    return {"unprotected_btc_exposure": float(exposure),
            "has_unprotected_exposure": exposure > protected}


@app.get("/api/bot/status")
def bot_status(response: Response,
               _: str = Depends(get_current_user)) -> Dict[str, Any]:
    """Summarize the bot or report the read-only monitor's stopped state."""
    response.headers["Cache-Control"] = "no-store"
    bot = app.state.grid_bot
    if app.state.standby and bot is not None:
        atr = app.state.atr_snapshot
        order_book = app.state.order_book_snapshot
        return {
            "trading_environment": TRADING_SETTINGS.environment.value,
            "status": "Online", "pair": bot.config.symbol,
            "safety_pause": "Idle", "pause_mode": None,
            "trading_state": "IDLE", "engine_status": ENGINE_IDLE,
            "engine_fault": (bot.database.get_state(ENGINE_FAULT_KEY) or
                             bot.database.get_state(ORDER_CLEANUP_ALARM_KEY)),
            "order_cleanup_required": bot.database.get_state(ENGINE_STOP_CLEANUP_KEY) == "1",
            "unresolved_order_alarm": bot.database.get_state(ORDER_CLEANUP_ALARM_KEY),
            "ghost_order_alarm": bot.database.get_state(GHOST_ORDER_ALARM_KEY),
            "has_grid_run": False, "grid_levels": 0,
            "exact_grid_recenter_supported": True,
            "lower_bound": None, "upper_bound": None,
            **_exchange_audit_telemetry(bot, bot.database),
            **_unprotected_exposure_payload(bot, bot.database, ENGINE_IDLE),
            "wallet": {"btc_held": 0.0, "average_cost": None,
                       "unrealized_pnl": 0.0},
            "realized_pnl": _realized_pnl_payload(bot.database),
            "atr_value": atr["atr_value"] if atr is not None else None,
            "atr_percentage": atr["atr_percentage"] if atr is not None else None,
            "bid_volume": order_book["bid_volume"] if order_book is not None else None,
            "ask_volume": order_book["ask_volume"] if order_book is not None else None,
            "imbalance_ratio": order_book["imbalance_ratio"] if order_book is not None else None,
            "current_hard_stop_loss": None, "high_water_mark": None,
        }
    if app.state.monitor_only and bot is None:
        return {
            "trading_environment": TRADING_SETTINGS.environment.value,
            "status": "Online", "pair": "BTC/USDT",
            "safety_pause": "Stopped", "pause_mode": None,
            "trading_state": "STOPPED", "engine_status": ENGINE_IDLE,
            "order_cleanup_required": False, "unresolved_order_alarm": None,
            "ghost_order_alarm": None,
            "has_grid_run": False, "grid_levels": 0,
            "exact_grid_recenter_supported": False,
            "lower_bound": None, "upper_bound": None,
            "last_exchange_audit_time": None,
            "configured_lower_bound": None, "configured_upper_bound": None,
            **_unprotected_exposure_payload(None, None, ENGINE_IDLE),
            "wallet": {"btc_held": 0.0, "average_cost": None,
                       "unrealized_pnl": 0.0},
            "realized_pnl": _realized_pnl_payload(None),
            "atr_value": None, "atr_percentage": None,
            "bid_volume": None, "ask_volume": None,
            "imbalance_ratio": None,
            "current_hard_stop_loss": None, "high_water_mark": None,
        }
    database = bot.database if bot is not None else GridDatabase()
    orders = [
        order for order in database.fetch_active_grids()
        if order["order_type"] == "LIMIT"
    ]
    prices = [Decimal(order["price"]) for order in orders]
    safety_mode = database.get_state(SAFETY_MODE_KEY)
    manual_account_exit = (database.get_state("halt_reason") ==
                           MANUAL_ACCOUNT_EXIT_REASON)
    engine_status = (database.get_state(ENGINE_STATUS_KEY) or
                     (ENGINE_RUNNING if bot is not None else ENGINE_IDLE))
    pending_text = database.get_state("grid_reset")
    pending_grid = None
    if pending_text:
        try:
            pending = json.loads(pending_text)
            pending_grid = {
                "token": hashlib.sha256(pending_text.encode("utf-8")).hexdigest(),
                "source": pending.get("source", "saved_grid"),
                "phase": pending.get("phase"),
                "center": pending.get("center_price"),
                "lower": pending.get("lower"),
                "upper": pending.get("upper"),
                "stop_loss": pending.get(
                    "stop_loss_price", str(bot.config.stop_loss_price) if bot else None
                ),
                "investment_quote": pending.get(
                    "investment_quote", str(bot.config.investment_quote) if bot else None
                ),
                "buy_levels": pending.get(
                    "buy_levels", bot.config.buy_grid_levels if bot else None
                ),
                "sell_levels": pending.get(
                    "sell_levels", bot.config.sell_grid_levels if bot else None
                ),
            }
        except (TypeError, ValueError, AttributeError):
            pending_grid = {"token": None, "source": "invalid"}
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
        "trading_environment": TRADING_SETTINGS.environment.value,
        "status": "Online" if bot is not None else "Offline",
        "pair": bot.config.symbol if bot is not None else "BTC/USDT",
        "safety_pause": (
            "Active" if safety_mode is not None
            else "Normal"
        ),
        "pause_mode": safety_mode,
        "engine_status": engine_status,
        "engine_fault": (database.get_state(ENGINE_FAULT_KEY) or
                         database.get_state(ORDER_CLEANUP_ALARM_KEY) or
                         database.get_state(LIQUIDATION_ALARM_KEY)),
        "order_cleanup_required": database.get_state(ENGINE_STOP_CLEANUP_KEY) == "1",
        "unresolved_order_alarm": database.get_state(ORDER_CLEANUP_ALARM_KEY),
        "ghost_order_alarm": database.get_state(GHOST_ORDER_ALARM_KEY),
        "pending_grid": pending_grid,
        "has_grid_run": database.get_state("grid_run") is not None,
        "trading_state": "ACCOUNT_CLEARED" if manual_account_exit else safety_mode if safety_mode in (
            LIQUIDATING, LIQUIDATED, LIQUIDATION_HALTED, PAUSED_SIZING,
            PAUSED_RESET) else (ENGINE_IDLE if engine_status == ENGINE_IDLE else "ACTIVE"),
        **({"pause_reason": database.get_state(SIZING_PAUSE_REASON_KEY)}
           if safety_mode == PAUSED_SIZING else {}),
        "grid_levels": len(orders),
        "exact_grid_recenter_supported": True,
        "lower_bound": float(min(prices)) if prices else None,
        "upper_bound": float(max(prices)) if prices else None,
        **_exchange_audit_telemetry(bot, database),
        **_unprotected_exposure_payload(bot, database, engine_status),
        "wallet": _portfolio_wallet(database) if bot is not None
                  else _unavailable_wallet(),
        "realized_pnl": _realized_pnl_payload(database),
        "atr_value": atr["atr_value"] if atr is not None else None,
        "atr_percentage": atr["atr_percentage"] if atr is not None else None,
        "bid_volume": order_book["bid_volume"] if order_book is not None else None,
        "ask_volume": order_book["ask_volume"] if order_book is not None else None,
        "imbalance_ratio": order_book["imbalance_ratio"] if order_book is not None else None,
        "current_hard_stop_loss": float(stop_value) if stop_value is not None else None,
        "high_water_mark": (float(high_water_mark)
                            if high_water_mark is not None else None),
    }


def _ledger_order(row: Dict[str, Any], *, filled: bool = False) -> Dict[str, str]:
    """Use saved execution data for fills and identify legacy price estimates."""
    price = Decimal(row["price"])
    amount = Decimal(row["amount"])
    price_source = "order"
    if filled and row.get("fill_snapshot"):
        try:
            snapshot = json.loads(row["fill_snapshot"])
            executed = Decimal(snapshot["filled_base"])
            quote = Decimal(snapshot["filled_quote"])
            if executed.is_finite() and quote.is_finite() and executed > 0 and quote > 0:
                amount = executed
                price = quote / executed
                price_source = "execution"
        except (KeyError, TypeError, ValueError, InvalidOperation, ZeroDivisionError):
            LOGGER.warning("Invalid saved fill snapshot for order ledger.")
    return {
        "id": str(row["order_id"]),
        "timestamp": row["updated_at"] if filled else row["created_at"],
        "side": row["side"],
        "status": ("PENDING_CONFIRMATION" if not filled and
                   row.get("exchange_order_id") is None else row["status"]),
        "order_type": row["order_type"],
        "price": format(price, "f"), "amount": format(amount, "f"),
        "price_source": price_source,
    }


@app.get("/api/orders/live")
def live_orders(response: Response,
                _: str = Depends(get_current_user)) -> Dict[str, Any]:
    """Show bot-tracked limits and recent fills without exchange API calls."""
    response.headers["Cache-Control"] = "no-store"
    bot = app.state.grid_bot
    if bot is None:
        return {"open_limits": [], "filled_trades": []}
    database = bot.database
    active = [
        _ledger_order(row) for row in database.fetch_active_grids()
        if row["order_type"] == "LIMIT"
    ]
    recent = [
        _ledger_order(row, filled=True)
        for row in database.fetch_recent_filled_orders(20)
    ]
    active.sort(key=lambda row: (row["timestamp"], row["id"]), reverse=True)
    return {"open_limits": active, "filled_trades": recent}


@app.get("/api/wallet-balance")
def wallet_balance(response: Response,
                   _: str = Depends(get_current_user)) -> Dict[str, str]:
    """Return only the admin's currently free Spot USDT balance."""
    response.headers["Cache-Control"] = "no-store"
    bot = app.state.grid_bot
    if bot is None and not app.state.monitor_only:
        raise HTTPException(status_code=503, detail="The trading bot is offline.")
    try:
        if bot is None:
            _credentials()
            exchange = create_exchange()
            balances = exchange.fetch_balance({"type": "spot"})
            asset = balances.get("USDT") if isinstance(balances, dict) else None
            free = asset.get("free") if isinstance(asset, dict) else None
            if free is None:
                raise TradingHalt("Exchange did not return a free USDT balance.")
            free_usdt = _order_decimal(free)
            if not free_usdt.is_finite() or free_usdt < 0:
                raise TradingHalt("Exchange returned an invalid free USDT balance.")
        else:
            free_usdt = bot._free_balance("USDT")
    except (ccxt.BaseError, TradingHalt, OSError, ValueError, TypeError,
            InvalidOperation) as error:
        LOGGER.warning("Spot USDT balance fetch failed: %s", type(error).__name__)
        raise HTTPException(
            status_code=502, detail="Could not fetch the Spot USDT balance."
        ) from error
    return {"available_usdt": str(free_usdt)}


@app.post("/api/bot/pause")
def set_bot_pause(payload: PauseRequest, _: None = Depends(_require_dashboard_origin),
                  __: str = Depends(get_current_user)) -> Dict[str, Any]:
    if app.state.standby:
        raise HTTPException(status_code=409, detail="The bot is idle; no grid is active.")
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


@app.post("/api/engine/start")
def start_engine(response: Response,
                 payload: Optional[EngineStartRequest] = None,
                 _: None = Depends(_require_dashboard_origin),
                 __: str = Depends(get_current_user)) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    bot = app.state.grid_bot
    if not app.state.ready or bot is None:
        raise HTTPException(status_code=503, detail="The ready-mode engine is offline.")
    try:
        result = bot.start_engine(
            auto_cover_inventory=(payload.auto_cover_inventory if payload else False),
            pending_grid_token=(payload.pending_grid_token if payload else None),
        )
    except InsufficientGridCapital as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except GridStopConflict as error:
        raise HTTPException(status_code=409, detail={
            "code": "GRID_STOP_CONFLICT", "message": str(error),
            "lowest_buy_price": str(error.lowest_buy_price),
            "hard_stop_price": str(error.hard_stop_price),
            "action": "REANCHOR_GRID",
        }) from error
    except (TradingHalt, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (ccxt.BaseError, OSError, RuntimeError, sqlite3.Error) as error:
        LOGGER.exception("Engine start validation failed.")
        raise HTTPException(status_code=502, detail="Could not verify the grid before starting.") from error
    app.state.standby = False
    schedule_telegram_alert(f"*Admin START*\nPair: {markdown_text(bot.config.symbol)}")
    return result


@app.post("/api/engine/stop")
def stop_engine(response: Response,
                _: None = Depends(_require_dashboard_origin),
                __: str = Depends(get_current_user)) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    bot = app.state.grid_bot
    if not app.state.ready or bot is None:
        raise HTTPException(status_code=503, detail="The ready-mode engine is offline.")
    try:
        result = bot.stop_engine()
        schedule_telegram_alert("*Admin STOP*\nBot orders cleared; engine stopped")
        return result
    except TradingHalt as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (ccxt.BaseError, OSError, RuntimeError, sqlite3.Error) as error:
        LOGGER.exception("Engine stop could not verify bot order cancellation.")
        raise HTTPException(status_code=502, detail="Engine is idle, but order cleanup needs verification.") from error


@app.post("/api/bot/prepare-reset")
def prepare_bot_reset(response: Response,
                      _: None = Depends(_require_dashboard_origin),
                      __: str = Depends(get_current_user)) -> Dict[str, Any]:
    """Pause placement and cancel/reconcile this bot's BUY and SELL orders."""
    response.headers["Cache-Control"] = "no-store"
    if app.state.standby:
        raise HTTPException(status_code=409, detail="The bot is idle; no grid is active.")
    bot = app.state.grid_bot
    if bot is None:
        raise HTTPException(status_code=503, detail="The trading bot is offline.")
    try:
        result = bot.prepare_factory_reset()
    except TradingHalt as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (ccxt.BaseError, OSError, RuntimeError, sqlite3.Error) as error:
        LOGGER.exception("Bot order cleanup for factory reset could not finish.")
        raise HTTPException(
            status_code=502,
            detail="Could not verify order cleanup; the bot remains paused for reset.",
        ) from error
    return result


@app.post("/api/bot/stop-loss")
def update_hard_stop(payload: StopLossRequest, response: Response,
                     _: None = Depends(_require_dashboard_origin),
                     __: str = Depends(get_current_user)) -> Dict[str, Any]:
    """Raise the active trailing floor without rebuilding or canceling the grid."""
    response.headers["Cache-Control"] = "no-store"
    if app.state.standby:
        raise HTTPException(status_code=409, detail="The bot is idle; no grid is active.")
    bot = app.state.grid_bot
    if bot is None:
        raise HTTPException(status_code=503, detail="The trading bot is offline.")
    try:
        stop = bot.set_stop_loss(str(payload.price))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except TradingHalt as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (OSError, RuntimeError, sqlite3.Error) as error:
        LOGGER.exception("Hard stop update failed.")
        raise HTTPException(status_code=503, detail="Could not update the hard stop.") from error
    LOGGER.warning("Admin raised the active hard stop to %s without a grid reset.", stop)
    return {"current_hard_stop_loss": float(stop), "grid_reset": False}


@app.post("/api/admin/factory-reset")
def factory_reset(_: None = Depends(_require_dashboard_origin),
                  __: str = Depends(get_current_user)) -> Dict[str, Any]:
    """Erase bot accounting only after exchange and local exposure are clear."""
    if app.state.standby:
        raise HTTPException(status_code=409, detail="The bot is idle; no grid is active.")
    bot = app.state.grid_bot
    if bot is None:
        raise HTTPException(status_code=503, detail="The trading bot is offline.")
    try:
        bot.factory_reset()
    except TradingHalt as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except (ccxt.BaseError, OSError, RuntimeError, sqlite3.Error) as error:
        LOGGER.exception("Factory reset could not verify the exchange or database.")
        raise HTTPException(
            status_code=502, detail="Factory reset could not verify account safety. No data was deleted."
        ) from error
    LOGGER.warning("Admin factory reset erased bot order and trade history.")
    return {"status": "reset", "trading_state": LIQUIDATED}


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
    if app.state.standby:
        if stop_distance is None or payload.allocated_capital is None:
            raise HTTPException(
                status_code=422,
                detail="Starting a grid requires stop loss, allocated capital, and exact grid levels.",
            )
        try:
            lower, upper = bot.request_initial_grid(
                str(payload.center_price), str(width), str(stop_distance),
                str(payload.allocated_capital), payload.grid_levels,
            )
        except InsufficientGridCapital as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except TradingHalt as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ccxt.BaseError, OSError, RuntimeError, sqlite3.Error) as error:
            LOGGER.exception("Initial grid request failed.")
            raise HTTPException(status_code=502, detail="Could not start the grid.") from error
        if app.state.ready:
            app.state.standby = False
        return {"status": "queued", "lower_bound": float(lower),
                "upper_bound": float(upper)}
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
    except InsufficientGridCapital as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
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
