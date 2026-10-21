"""Validated runtime settings and shared state keys."""

import os
from decimal import Decimal
from pathlib import Path
from typing import Tuple
from urllib.parse import urlsplit
from dotenv import load_dotenv
from database import resolve_database_path
from trading_environment import get_trading_settings

BASE_DIR = Path(__file__).resolve().parent
MAX_LEVELS = 50
ORDER_CLIENT_PREFIX = "gridbot"
ORDER_CLIENT_PREFIX_KEY = "order_client_prefix"
SELL_AMOUNT_BUFFER = Decimal("0.002")
SPOT_FEE_RATE = Decimal("0.001")  # Standard 0.1% per execution side.
MIN_GRID_ORDER_NOTIONAL = Decimal("7")
RESET_SPACING_PERCENT = Decimal("2.5")
SAFETY_MODE_KEY = "safety_mode"
MACRO_BUY_PAUSE_KEY = "macro_buy_pause"
MACRO_BUY_REASON_KEY = "macro_buy_pause_reason"
MACRO_BUY_NOTICE_KEY = "macro_buy_notification_pending"
ENGINE_STATUS_KEY = "engine_status"
ENGINE_IDLE = "IDLE"
ENGINE_RUNNING = "RUNNING"
ENGINE_HALTED = "HALTED"
ENGINE_FAULT_KEY = "engine_fault"
ENGINE_STOP_CLEANUP_KEY = "engine_stop_cleanup_pending"
ORDER_CLEANUP_ALARM_KEY = "order_cleanup_alarm"
RECONCILE_COVER_KEY = "reconcile_cover_inventory"
MANUAL_ACCOUNT_EXIT_REASON = "manual_full_account_exit"
PAUSED_DOWNSIDE = "PAUSED_DOWNSIDE"
PAUSED_MANUAL = "PAUSED_MANUAL"
PAUSED_RESET = "PAUSED_RESET"
PAUSED_SIZING = "PAUSED_SIZING"
LIQUIDATING = "LIQUIDATING"
LIQUIDATED = "LIQUIDATED"
LIQUIDATION_HALTED = "HALTED"
LIQUIDATION_KEY = "hard_stop_liquidation"
LIQUIDATION_ALARM_KEY = "hard_stop_alarm"
GHOST_ORDER_ALARM_KEY = "ghost_order_alarm"
RUNNER_LEASE_TTL_SECONDS = 30
RUNNER_LEASE_RENEW_SECONDS = 5
FULL_BOOK_AUDIT_SECONDS = 300
LAST_EXCHANGE_AUDIT_TIME_KEY = "last_exchange_audit_time"
RISK_OVERRIDE_DENIED = (
    "Risk Override Denied: The requested hard stop is lower than the active "
    "trailing floor. The stop loss can only move up."
)
LIQUIDATION_NOTICE_KEY = "hard_stop_notification_pending"
SAFETY_PAUSE_NOTICE_KEY = "safety_pause_notice_pending"
SAFETY_RECOVERY_NOTICE_KEY = "safety_recovery_notice_pending"
SAFETY_RESUME_NOTICE_KEY = "safety_resume_notice_pending"
SIZING_PAUSE_REASON_KEY = "sizing_pause_reason"
SIZING_PAUSE_NOTICE_KEY = "sizing_pause_notice_pending"
LAST_MARKET_PRICE_KEY = "last_market_price"
LAST_MARKET_PRICE_TIME_KEY = "last_market_price_at"
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
# Validate before creating the ASGI app, including Uvicorn and monitor-only starts.
TRADING_SETTINGS = get_trading_settings()
resolve_database_path()


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



def _credentials() -> Tuple[str, str]:
    return TRADING_SETTINGS.api_key, TRADING_SETTINGS.api_secret
