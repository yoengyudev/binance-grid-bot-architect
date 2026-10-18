"""Tracked-order cancellation and stop coordination for the trading loop."""

import logging
import traceback
from threading import Event, Lock, RLock
from typing import Any, Callable, Dict, Optional

from database import GridDatabase
from models import StopResult
from portfolio import save_fill_snapshot
from exchange_handler import CircuitOpenError, check_exchange_circuit, exchange_request


LOGGER = logging.getLogger(__name__)


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
        if self.database.in_transaction:
            raise RuntimeError("Exchange I/O must occur outside SQLite transactions.")
        with self.exchange_lock:
            if self.exchange_guard is not None:
                self.exchange_guard()
            check_exchange_circuit(self.exchange)
            return exchange_request(method, *args)

    def _record_error(self, order_id: str, event: str, error: Exception) -> None:
        state = self.database.get_state("engine_status")
        details = f"Order {order_id}:\n" + "".join(traceback.format_exception(error))
        self.database.record_transition(state, state, event, details)

    def request_stop(self) -> StopResult:
        """Signal stop before making exchange calls; retry unresolved rows later."""
        self.stop_requested.set()
        self.database.require_order_cleanup(
            "Stop requested; Binance order cleanup is unverified.", trigger_event="STOP")
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
            orders = [order for order in self.database.fetch_active_grids()
                      if predicate is None or predicate(order)]
            for index, order in enumerate(orders):
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
                        self._record_error(order_id, "STOP_CANCEL_FAILED", error)
                        LOGGER.warning("Cancellation of %s could not be confirmed: %s; fetching status.",
                                       order_id, error, exc_info=True)
                        if isinstance(error, CircuitOpenError):
                            raise
                        response = None

                    status = response.get("status") if isinstance(response, dict) else None
                    final_order = response
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
                        final_order = current

                    if status in ("canceled", "closed"):
                        with self.database.transaction():
                            base, quote = self.symbol.split("/")
                            save_fill_snapshot(self.database, base, quote, order_id, final_order)
                            self.database.update_order_status(
                                order_id, "CANCELED" if status == "canceled" else "FILLED")
                            state = self.database.get_state("engine_status")
                            self.database.record_transition(
                                state, state, "STOP_ORDER_" + status.upper())
                        canceled += status == "canceled"
                        filled += status == "closed"
                    else:
                        state = self.database.get_state("engine_status")
                        self.database.record_transition(
                            state, state, "STOP_ORDER_UNRESOLVED",
                            f"Order {order_id} has unverified exchange status {status!r}.")
                        unresolved += 1
                except Exception as error:
                    # Keep the row active so the next run can reconcile it.
                    self._record_error(order_id, "STOP_RECONCILE_FAILED", error)
                    LOGGER.exception("Stop could not resolve order %s: %s.", order_id, error)
                    if isinstance(error, CircuitOpenError):
                        unresolved += len(orders) - index
                        break  # Keep all remaining intents active for a later retry.
                    unresolved += 1
        return StopResult(canceled, filled, unresolved)
