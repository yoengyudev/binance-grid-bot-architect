"""Pure Decimal fee-defense geometry, shared by execution and research code."""
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

MIN_GRID_RETURN = Decimal('0.008')  # 0.8% gross SELL / BUY return, not net P&L.


def minimum_cycle_return(center, buys, sells):
    if not buys or not sells:
        raise ValueError('Both sides require at least one grid level.')
    returns = []
    for side in (buys, sells):
        previous = center
        for price in side:
            low, high = min(previous, price), max(previous, price)
            if low <= 0:
                raise ValueError('Grid prices must be positive.')
            returns.append(high / low - 1)
            previous = price
    return min(returns)


def validate_grid_returns(center, buys, sells, minimum=MIN_GRID_RETURN):
    actual = minimum_cycle_return(center, buys, sells)
    if actual < minimum:
        raise ValueError(
            f'Fee defense: smallest rounded grid return is {actual * 100:.6f}%; '
            f'at least {minimum * 100:.2f}% is required. Widen the bounds or use fewer levels.'
        )
    return actual


def defended_bounds(center, half_width, buy_count, sell_count, *,
                    minimum=MIN_GRID_RETURN, tick=Decimal('0.01'), round_price=None):
    """Widen symmetric bounds until every rounded adjacent cycle clears the floor.

    Add a small precision margin only when the desired width is insufficient.
    Validate the actual geometric levels; an unrounded ratio is insufficient.
    """
    if not (center > 0 and half_width > 0 and tick > 0 and 1 <= buy_count <= 50
            and 1 <= sell_count <= 50 and 0 <= minimum < 1):
        raise ValueError('Invalid fee-defense geometry.')
    if round_price is None:
        round_price = lambda value: (value / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    required = max(center * (1 - (1 + minimum) ** -buy_count),
                   center * ((1 + minimum) ** sell_count - 1))
    width = max(half_width, required)
    if minimum and width <= required + tick:
        width += 4 * max(buy_count, sell_count) * tick
    width = (width / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    for _ in range(256):
        lower, upper = center - width, center + width
        if lower <= 0:
            raise ValueError('Requested level count/spacing leaves no positive lower bound.')
        sides = []
        for bound, count in ((lower, buy_count), (upper, sell_count)):
            ratio = (bound / center) ** (Decimal(1) / count)
            sides.append([round_price(bound if i == count else center * ratio ** i)
                          for i in range(1, count + 1)])
        if minimum_cycle_return(round_price(center), *sides) >= minimum:
            return lower, upper
        width += tick
    raise ValueError('Could not satisfy fee defense at this exchange precision.')
