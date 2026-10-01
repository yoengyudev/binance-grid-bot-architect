"""Tracked-order cancellation and stop coordination for the trading loop."""

import logging
from dataclasses import dataclass
from threading import Event, Lock, RLock
from typing import Any, Callable, Dict, Optional

from database import GridDatabase


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class StopResult:
    canceled: int = 0
    filled: int = 0
    unresolved: int = 0


class StopController:
    """Pause the trading loop and cancel only orders tracked by this bot."""

    def __init__(
        self,
        exchange: Any,
        database: GridDatabase,
        symbol: str,
        exchange_lock: Optional[Any] = None,
        exchange_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        self.exchange = exchange
        self.database = database
        self.symbol = symbol
        self.stop_requested = Event()
        self._lock = Lock()
        self.exchange_lock = exchange_lock or RLock()
        self.exchange_guard = exchange_guard

    def _exchange_call(self, method: Any, *args: Any) -> Any:
        with self.exchange_lock:
            if self.exchange_guard is not None:
                self.exchange_guard()
            return method(*args)

    def request_stop(self) -> StopResult:
        """Signal stop before making exchange calls; retry unresolved rows later."""
        self.stop_requested.set()
        self.database.require_order_cleanup("Stop requested; Binance order cleanup is unverified.")
        result = self.cancel_tracked_orders()
        if result.unresolved:
            LOGGER.error("Stop left %s tracked orders unresolved; cleanup remains required.",
                         result.unresolved)
        return result

    def cancel_tracked_orders(
        self, predicate: Optional[Callable[[Dict[str, Any]], bool]] = None
    ) -> StopResult:
        """Cancel tracked orders without pausing the trading loop."""
        canceled = filled = unresolved = 0
        with self._lock:
            for order in self.database.fetch_active_grids():
                if predicate is not None and not predicate(order):
                    continue
                order_id = order["order_id"]
                reference = order.get("exchange_order_id") or order_id
                params = (
                    {"origClientOrderId": order["client_order_id"]}
                    if order.get("client_order_id") else None
                )
                try:
                    try:
                        if params is None:
                            response = self._exchange_call(
                                self.exchange.cancel_order, reference, self.symbol
                            )
                        else:
                            response = self._exchange_call(
                                self.exchange.cancel_order, reference, self.symbol, params
                            )
                    except Exception as error:
                        # The order may have filled just before cancellation.
                        LOGGER.warning("Cancellation of %s could not be confirmed (%s); fetching status.",
                                       order_id, type(error).__name__)
                        response = None

                    status = response.get("status") if isinstance(response, dict) else None
                    if status not in ("canceled", "closed"):
                        if params is None:
                            current = self._exchange_call(
                                self.exchange.fetch_order, reference, self.symbol
                            )
                        else:
                            current = self._exchange_call(
                                self.exchange.fetch_order, reference, self.symbol, params
                            )
                        status = current.get("status") if isinstance(current, dict) else None

                    if status == "canceled":
                        self.database.update_order_status(order_id, "CANCELED")
                        canceled += 1
                    elif status == "closed":
                        self.database.mark_order_filled(order_id)
                        filled += 1
                    else:
                        unresolved += 1
                except Exception as error:
                    # Keep the row active so the next run can reconcile it.
                    LOGGER.error("Stop could not resolve order %s (%s).",
                                 order_id, type(error).__name__)
                    unresolved += 1
        return StopResult(canceled, filled, unresolved)
