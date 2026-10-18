"""Pure residual BUY policy shared by execution and offline research."""
from decimal import Decimal

CAP_EXHAUSTED = 'CAP_EXHAUSTED'
CAP_STATE_KEY = 'allocation_buy_state'
CAP_REASON_KEY = 'allocation_buy_reason'
CAP_ORDER_KEY = 'allocation_final_buy_id'


def residual_quantity(requested, price, remaining, round_amount):
    """Round down within the unchanged cost cap; never spend a rounding remainder."""
    values = (requested, price, remaining)
    if any(not isinstance(v, Decimal) or not v.is_finite() for v in values):
        raise ValueError('Residual BUY inputs must be finite Decimals.')
    if requested <= 0 or price <= 0:
        raise ValueError('Residual BUY price and requested quantity must be positive.')
    if remaining <= 0:
        return Decimal(0)
    amount = round_amount(min(requested, remaining / price))
    if not amount.is_finite() or amount < 0 or amount > requested or amount * price > remaining:
        raise ValueError('Exchange precision exceeded the residual BUY budget.')
    return amount
