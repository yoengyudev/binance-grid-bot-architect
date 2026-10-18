"""Versioned production policy. Changes require a new reviewed code release.

Historical simulator defaults deliberately remain separate for reproducibility.
No RSI or allocation-cap bypass is part of this policy.
"""
from decimal import Decimal as D
from grid_geometry import defended_bounds

BASELINE_ID = 'sma200-compressed-atr-v1'
ATR_SPACING_FRACTION = D('.5')
MIN_GRID_RETURN = D('.004')
GRID_LEVELS = 5
HOUR_MS = 3_600_000


def validate_profile(raw, *, required=False):
    marker = raw.get('strategy_baseline')
    if marker is None and not required:
        return False  # Preserve existing Testnet ledgers/configuration.
    if marker != BASELINE_ID:
        raise ValueError(f'Mainnet/locked configuration requires strategy_baseline={BASELINE_ID}.')
    if set(raw)-{'exchange','grid','strategy_baseline'}:
        raise ValueError('Locked baseline forbids unreviewed top-level configuration fields.')
    grid = raw.get('grid',{})
    allowed = {'symbol','investment_quote','lower_price','upper_price','spacing_percent',
               'initial_inventory_percent','stop_loss_price','poll_seconds',
               'auto_center_percent','grid_levels'}
    if set(grid)-allowed:
        raise ValueError('Locked baseline forbids unreviewed strategy/grid parameters.')
    if (grid.get('symbol')!='BTC/USDT' or grid.get('grid_levels')!=GRID_LEVELS
            or isinstance(grid.get('grid_levels'),bool)
            or D(str(grid.get('initial_inventory_percent',0)))!=D(50)
            or grid.get('auto_center_percent') is not None):
        raise ValueError('Locked baseline requires BTC/USDT, 5 levels, 50% seed and no auto-centering.')
    return True


def compressed_bounds(center,atr,*,tick=D('.01'),round_price=None):
    center,atr = D(str(center)),D(str(atr))
    if not center.is_finite() or not atr.is_finite() or min(center,atr)<=0:
        raise ValueError('Closed 1h ATR and center must be positive and finite.')
    target = max(ATR_SPACING_FRACTION*atr/center,MIN_GRID_RETURN)
    return defended_bounds(center,tick,3,2,minimum=target,tick=tick,round_price=round_price)


def closed_hourly_atr(candles,now_ms):
    """Same fresh-15-candle ATR/RMA definition as the saved research simulator.

    Only the latest 15 consecutive closed UTC hours are eligible. A forming
    candle, future bars, gaps, stale history and malformed OHLC fail closed.
    """
    end = int(now_ms)//HOUR_MS*HOUR_MS
    by_time = {}
    for row in candles:
        if not isinstance(row,(list,tuple)) or len(row)<6:
            raise ValueError('Malformed hourly candle.')
        stamp = int(row[0])
        if stamp!=row[0] or stamp%HOUR_MS or stamp in by_time:
            raise ValueError('Duplicate/misaligned hourly candle.')
        by_time[stamp] = row
    true_ranges = []
    previous = None
    for stamp in range(end-15*HOUR_MS,end,HOUR_MS):
        if stamp not in by_time:
            raise ValueError('Need 15 consecutive closed 1h candles through the last closed hour.')
        o,h,l,c = (D(str(v)) for v in by_time[stamp][1:5])
        if (not all(x.is_finite() and x>0 for x in (o,h,l,c))
                or h<max(o,l,c) or l>min(o,h,c)):
            raise ValueError('Invalid closed hourly OHLC.')
        true_ranges.append(h-l if previous is None else max(h-l,abs(h-previous),abs(l-previous)))
        previous = c
    atr = (sum(true_ranges[:14],D(0))/14*13+true_ranges[14])/14
    if atr<=0:
        raise ValueError('Closed hourly ATR must be positive.')
    return atr,previous,end
