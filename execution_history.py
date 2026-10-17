"""Rebuild cumulative order accounting from Binance's private trade history."""

from decimal import Decimal
from typing import Any, Callable, Dict

import ccxt


class ExecutionHistoryPending(ccxt.NetworkError):
    """Order/trade views disagree temporarily; no replacement may be submitted."""


def verified_execution_order(call: Callable[..., Any], exchange: Any, symbol: str,
                             order: Dict[str, Any]) -> Dict[str, Any]:
    """Read all executions for one known order, never adopt unrelated wallet trades.

    Binance's fetch_order_trades uses /myTrades with orderId. Start at fromId=0
    rather than relying on an exchange default that can return only recent fills.
    Incomplete history must not turn a cumulative fill into an incorrect balance.
    """
    filled = Decimal(str(order.get('filled') or 0))
    if filled <= 0 or not getattr(exchange, 'has', {}).get('fetchOrderTrades'):
        return order
    order_id = str(order['id'])
    trades = call(exchange.fetch_order_trades, order_id, symbol, None, 500, {'fromId': 0})
    seen, fees = set(), {}
    quantity = cost = Decimal(0)
    cursor = -1
    for _ in range(100):  # Bound pathological pagination; fail closed below.
        if not isinstance(trades, list):
            raise ExecutionHistoryPending('Invalid execution-history response.')
        page_ids = []
        for trade in trades:
            if not isinstance(trade, dict) or str(trade.get('order')) != order_id:
                raise ExecutionHistoryPending('Execution belongs to another order.')
            if trade.get('symbol') not in (None, symbol):
                raise ExecutionHistoryPending('Execution belongs to another symbol.')
            info = trade.get('info') or {}
            trade_id = trade.get('id', info.get('id'))
            if trade_id is not None:
                trade_id = int(trade_id)
                page_ids.append(trade_id)
                if trade_id in seen:
                    continue
                seen.add(trade_id)
            qty = Decimal(str(info.get('qty', trade.get('amount'))))
            quote = info.get('quoteQty', trade.get('cost'))
            if quote is None:
                quote = qty * Decimal(str(trade.get('price') or order.get('average') or order.get('price')))
            quote = Decimal(str(quote))
            if not qty.is_finite() or not quote.is_finite() or qty <= 0 or quote <= 0:
                raise ExecutionHistoryPending('Invalid execution amount or cost.')
            if 'commission' in info and info.get('commissionAsset'):
                commissions = [{'currency': info['commissionAsset'], 'cost': info['commission']}]
            else:
                commissions = trade.get('fees') or ([trade['fee']] if trade.get('fee') else [])
            if not commissions:
                raise ExecutionHistoryPending('Execution commissions are not available yet.')
            for fee in commissions:
                amount = Decimal(str(fee['cost']))
                if not fee.get('currency') or not amount.is_finite() or amount < 0:
                    raise ExecutionHistoryPending('Invalid execution commission.')
                fees[fee['currency']] = fees.get(fee['currency'], Decimal(0)) + amount
            quantity += qty
            cost += quote
        if len(trades) < 500:
            break
        if len(page_ids) != len(trades) or max(page_ids) <= cursor:
            raise ExecutionHistoryPending('Execution-history cursor did not advance.')
        cursor = max(page_ids)
        trades = call(exchange.fetch_order_trades, order_id, symbol, None, 500,
                      {'fromId': cursor + 1})
    else:
        raise ExecutionHistoryPending('Execution-history page limit exceeded.')
    if quantity <= 0 or abs(quantity - filled) > Decimal('0.000000000001'):
        raise ExecutionHistoryPending('Order and execution quantities differ; retry reconciliation.')
    return dict(order, cost=str(cost), average=str(cost / quantity), fee=None,
                fees=[{'currency': asset, 'cost': str(amount)} for asset, amount in fees.items()])
