"""Durable Binance aggregate-trade catch-up for crossings between ticker polls.

This is retrospective protection, not an exchange-hosted stop or a fill-price
guarantee. It deliberately does not replay pre-installation market history.
"""
import json
from decimal import Decimal, InvalidOperation

import ccxt

CURSOR_KEY = 'stop_trade_cursor'
BREACH_KEY = 'stop_trade_breach'
ALARM_KEY = 'stop_trade_history_alarm'


class StopHistoryPending(ccxt.NetworkError):
    """Trade coverage is not verified; ordinary order placement must wait."""


class StopTradeHistory:
    def __init__(self, database, fetch, symbol):
        self.database, self.fetch, self.symbol = database, fetch, symbol

    @staticmethod
    def _trade(raw):
        try:
            identity, timestamp = int(raw['a']), int(raw['T'])
            price = Decimal(str(raw['p']))
            if identity < 0 or timestamp < 0 or not price.is_finite() or price <= 0:
                raise ValueError()
            return identity, timestamp, price
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise StopHistoryPending('Invalid aggregate trade; stop coverage unverified.') from error

    def _latest(self):
        rows = self.fetch({'symbol': self.symbol, 'limit': 1})
        if not isinstance(rows, list) or len(rows) != 1:
            raise StopHistoryPending('No aggregate trade available for stop coverage.')
        return self._trade(rows[0])

    def initialize(self, *, reset=False, floor=None):
        if reset or self.database.get_state(CURSOR_KEY) is None:
            identity, timestamp, price = self._latest()
            with self.database.transaction():
                self.database.set_state(CURSOR_KEY, json.dumps({
                    'id': identity, 'time': timestamp, 'floor': str(floor) if floor is not None else None}))
                if reset:
                    self.database.clear_state(BREACH_KEY)
                if floor is not None and price < floor:
                    self.database.set_state(BREACH_KEY, json.dumps({
                        'id': identity, 'time': timestamp, 'price': str(price), 'floor': str(floor)}))
                state = self.database.get_state('engine_status')
                self.database.record_transition(state, state, 'STOP_TRADE_COVERAGE_STARTED',
                                                f'Aggregate trade {identity}; earlier history is not replayed.')

    def scan(self, floor, *, max_pages=4):
        """Read every ID through a fixed tail; never skip a truncated page/gap.

        Progress survives network failure/restart. A breach is committed before
        returning, so a failed inventory read cannot erase it after a rebound.
        """
        try:
            breach = self.database.get_state(BREACH_KEY)
            if breach:
                return Decimal(json.loads(breach)['price'])
            self.initialize(floor=floor)
            breach = self.database.get_state(BREACH_KEY)
            if breach:
                return Decimal(json.loads(breach)['price'])
            cursor = json.loads(self.database.get_state(CURSOR_KEY))
            identity, timestamp = int(cursor['id']), int(cursor['time'])
            # A newly raised stop must not apply retrospectively to old prices.
            history_floor = min(floor, Decimal(cursor.get('floor') or str(floor)))
            target, _, _ = self._latest()
            if target < identity:
                raise StopHistoryPending('Aggregate IDs regressed; review exchange reset before START.')
            for _ in range(max_pages):
                if identity >= target:
                    self.database.set_state(CURSOR_KEY, json.dumps({
                        'id': identity, 'time': timestamp, 'floor': str(floor)}))
                    self.database.clear_state(ALARM_KEY)
                    return None
                rows = self.fetch({'symbol': self.symbol, 'fromId': identity + 1, 'limit': 1000})
                if not isinstance(rows, list) or not rows:
                    raise StopHistoryPending('Aggregate trade history is incomplete.')
                progressed = False
                for raw in rows:
                    next_id, next_time, price = self._trade(raw)
                    if next_id > target:
                        break
                    if next_id != identity + 1 or next_time < timestamp:
                        raise StopHistoryPending('Aggregate trade history has a gap or regressed.')
                    identity, timestamp, progressed = next_id, next_time, True
                    if price < history_floor:
                        with self.database.transaction():
                            self.database.set_state(BREACH_KEY, json.dumps({
                                'id': identity, 'time': timestamp, 'price': str(price), 'floor': str(history_floor)}))
                            self.database.set_state(CURSOR_KEY, json.dumps({
                                'id': identity, 'time': timestamp, 'floor': str(history_floor)}))
                        self.database.clear_state(ALARM_KEY)
                        return price
                if not progressed:
                    raise StopHistoryPending('Aggregate trade cursor did not advance.')
                self.database.set_state(CURSOR_KEY, json.dumps({
                    'id': identity, 'time': timestamp, 'floor': str(history_floor)}))
            if identity >= target:
                self.database.set_state(CURSOR_KEY, json.dumps({
                    'id': identity, 'time': timestamp, 'floor': str(floor)}))
                self.database.clear_state(ALARM_KEY)
                return None
            raise StopHistoryPending('Catching up stop history; new order placement deferred.')
        except (ccxt.NetworkError, ccxt.ExchangeError) as error:
            self.database.set_state(ALARM_KEY, str(error))
            raise

    def acknowledge_flat(self):
        # Only the caller's reconciled, zero bot inventory can dismiss a breach.
        self.database.clear_state(BREACH_KEY)
