# Execution fault-tolerance audit

Scope: startup/START, CCXT order reads and writes, SQLite fill accounting, and
partial grid executions. Changes are local; no live service or orders were touched.

## Findings and refactors

| Finding | Existing behavior | Refactor |
| --- | --- | --- |
| Crash-window fills | Startup already queried account open orders and fetched missing tracked orders by saved client/exchange ID. A fill could be marked FILLED without recovering commissions or realized SELL accounting before START. | `GridBot._recover_execution_ledger()` runs inside startup/START reconciliation. It refreshes active orders and incomplete terminal snapshots, verifies private executions, and recovers missing realized SELL records. |
| Incomplete trade history | BUY commission lookup requested one default page. An order with many executions could wait forever for its complete quantity. | `execution_history.verified_execution_order()` explicitly reads `/myTrades` through CCXT's `fetch_order_trades`, scoped to symbol/order ID, from trade ID zero, with cursor pagination and trade-ID deduplication. Cumulative quantity must match before replacement orders can use its accounting. |
| Transient reads | Active-loop failures had outer exponential retry, but individual reads failed immediately. Background idle cleanup used a fixed polling delay. | `_call()` retries read methods up to three attempts with exponential jitter, releasing the exchange lock between attempts. Active-loop and idle-cleanup retries use capped exponential jitter. Existing shared circuit penalties and Retry-After remain minimum delays. |
| Lost create response | The durable intent prevented blind replay, but even an exchange-accepted timeout immediately became an uncertain-order fault. | `_submit_order()` queries the SAME saved client ID after a network/rate-limit exception. A confirmed matching acceptance is attached to the original intent without another POST. Failed recovery retains the intent and fails closed. CCXT automatic request replay is explicitly disabled. |
| Partial BUY lane | Cumulative partial fills were recorded, but the lane waited for the whole order indefinitely. | Sellable LIMIT BUY partials now cancel their remainder with a durable `partial_fill_buy` marker. The engine verifies the final executed quantity before placing one matching SELL. A fragment below exchange/bot minimums remains working; other lanes continue. |
| Stale cumulative fills | A stale exchange response could overwrite a newer cumulative fill snapshot. | `save_fill_snapshot()` rejects a decreasing executed quantity. Engine snapshot updates use SQLite transactions, including commission-only refreshes. |

## Startup execution path

1. The process acquires its runner lease before startup reconciliation. START also
   holds the engine cycle lock and checks the existing cleanup interlock.
2. `_reconcile_state()` loads market rules, queries the account open book, checks
   bot ownership/identity/amount/price, and queries tracked orders missing from it.
3. `_recover_execution_ledger()` reads private execution history for tracked
   orders requiring recovery. Synthetic carry lots and unrelated wallet trades
   are not adopted. Complete terminal snapshots are cached to limit repeat reads.
4. Verified execution quantity, weighted execution cost, commissions, exchange ID,
   and order status are saved together. Missing completed SELL accounting is
   replayed through `_record_filled_sell()`; the existing unique sell-order key
   prevents duplicate realized trades after a crash or repeated reconciliation.
5. Only successful reconciliation advances the audit timestamp and permits the
   existing START guards to enable execution. Ready-mode process restarts still
   clean up orders and remain IDLE pending manual START.

The initial status-recovery transaction and later realized-SELL transaction may
be interrupted separately. Recovery deliberately scans for FILLED SELL rows with
no realized record, so replay repairs that crash window without placing orders.

## Retry and partial-fill guarantees

- Read retry is bounded. After exhaustion, the running loop retries with a
  longer capped delay. Authentication, invalid orders, accounting violations,
  and conflicting identity are not blindly retried as successful trades.
- Rate limits/DDoS errors leave the immediate read loop and use the shared
  circuit breaker. A known cooldown cannot be bypassed by order recovery.
- A timed-out create/cancel is never blindly replayed. Unconfirmed submission
  recovery still requires review; assuming rejection would risk duplicate BTC.
- A partial BUY cancellation timeout leaves its intent and marker durable. A
  later cycle/restart resolves it by exact ID. A fill winning the cancel race is
  processed using its final quantity, not the earlier partial quantity.
- A partially filled SELL stays open, protecting the unsold remainder. Its
  cumulative disposal reduces inventory immediately; realized cycle P&L and a
  replacement BUY follow confirmed completion. Other lanes continue meanwhile.
- Unexpected external cancellations still halt for review. This patch does not
  silently reinterpret arbitrary cancellations as planned grid actions.

## Validation and limits

`tests/test_execution_recovery.py` adds deterministic tests for ghost BUY/SELL
fills, deduplicated realized accounting, missing history, pagination, bounded and
interruptible retries, circuit cooldowns, ambiguous create acceptance/rejection,
30% BUY and SELL fills, below-minimum fragments, cancellation races, restart
recovery, and monotonic snapshots. Existing fault, stop, lease, API, macro-filter,
and liquidation regressions are retained. The final full local unittest suite
passed all **456 tests**, including **18 new recovery tests**. Core-file syntax
compilation and `git diff --check` also passed.

These tests use fake exchanges and temporary SQLite files. They do not certify
Mainnet behavior or simulate real Binance latency. Incomplete history during
startup keeps START locked with a recorded reconciliation fault. A completely
lost database cannot safely reconstruct bot ownership or parent-lot ancestry
from wallet trades alone. Fees in a third asset such as BNB retain the existing
estimated quote-currency valuation. During exchange outages the existing
client-side hard stop cannot guarantee immediate execution.

Reference: [Binance Spot account trade list](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints#account-trade-list-user_data)
documents the 500-trade default and supported `symbol + orderId + fromId`
pagination. [CCXT Manual](https://docs.ccxt.com/docs/manual) documents unified
order lookup and exchange-specific trade pagination.
