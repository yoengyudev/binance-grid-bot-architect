"""Iteration 2 constants and closed daily signal validation (no network I/O)."""
from decimal import Decimal, ROUND_DOWN

DAILY_MS = 86_400_000
MACRO_SMA_PERIOD = 200
CATASTROPHIC_STOP_GAP = Decimal('0.15')
STRATEGY_VERSION = 'iteration2'


def catastrophic_stop(lower, tick=Decimal('0.01')):
    """Round down by exchange tick so the 15% buffer is never shortened."""
    lower, tick = Decimal(str(lower)), Decimal(str(tick))
    if not lower.is_finite() or not tick.is_finite() or lower <= 0 or tick <= 0:
        raise ValueError('Invalid lower bound or price tick for catastrophic stop.')
    stop = ((lower * (1 - CATASTROPHIC_STOP_GAP) / tick)
            .to_integral_value(rounding=ROUND_DOWN) * tick)
    if stop <= 0:
        raise ValueError('Catastrophic stop rounds below the minimum price tick.')
    return stop


def closed_daily_sma(candles, now_ms):
    """Require the latest 200 consecutive fully closed UTC daily candles."""
    day = int(now_ms) // DAILY_MS * DAILY_MS
    closes = {}
    for candle in candles:
        if not isinstance(candle, (tuple, list)) or len(candle) < 6:
            raise ValueError('Malformed daily candle.')
        stamp = int(candle[0])
        if stamp != candle[0] or stamp % DAILY_MS or stamp in closes:
            raise ValueError('Daily candles are duplicated or not UTC aligned.')
        close = Decimal(str(candle[4]))
        if not close.is_finite() or close <= 0:
            raise ValueError('Daily candle close must be positive and finite.')
        closes[stamp] = close
    required = [day - i * DAILY_MS for i in range(MACRO_SMA_PERIOD, 0, -1)]
    if any(stamp not in closes for stamp in required):
        raise ValueError('Daily SMA200 needs 200 consecutive closed days through yesterday.')
    return sum((closes[stamp] for stamp in required), Decimal(0)) / MACRO_SMA_PERIOD, day
