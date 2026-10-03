"""Fixed-strategy ATR and dashboard order-book cache refresh."""

import logging
import asyncio
import math
import time
from typing import Any, Dict, List, Optional
import pandas as pd
import pandas_ta
from exchange_handler import CircuitOpenError, check_exchange_circuit
from settings import (
    ATR_REFRESH_SECONDS,
    ORDER_BOOK_REFRESH_SECONDS,
)
from engine import (
    GridBot,
)

LOGGER = logging.getLogger(__name__)


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


async def _refresh_market_data(bot: Any, state: Any) -> None:
    """Cache read-only market data without blocking the API or trading loop."""
    next_atr_refresh = 0.0
    while True:
        try:
            check_exchange_circuit(getattr(bot, "exchange", None))
        except CircuitOpenError as error:
            state.atr_snapshot = None
            state.order_book_snapshot = None
            await asyncio.sleep(max(ORDER_BOOK_REFRESH_SECONDS, error.retry_after))
            continue
        if time.monotonic() >= next_atr_refresh:
            try:
                state.atr_snapshot = await asyncio.to_thread(_fetch_atr_snapshot, bot)
            except Exception as error:
                state.atr_snapshot = None
                LOGGER.warning("Hourly ATR unavailable: %s", type(error).__name__)
            next_atr_refresh = time.monotonic() + ATR_REFRESH_SECONDS
        try:
            state.order_book_snapshot = await asyncio.to_thread(
                _fetch_order_book_snapshot, bot
            )
        except Exception as error:
            state.order_book_snapshot = None
            LOGGER.warning("Order book imbalance unavailable: %s", type(error).__name__)
        await asyncio.sleep(ORDER_BOOK_REFRESH_SECONDS)
