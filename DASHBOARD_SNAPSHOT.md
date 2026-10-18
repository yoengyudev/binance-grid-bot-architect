# Dashboard snapshot and total allocation policy

## Exhausted allocation

`allocation_state=CAP_EXHAUSTED` exposes the residual BUY latch, with
`allocation_pause_reason` and `final_residual_buy_id`. `pause_mode` displays it
when no other safety mode takes priority; liquidation/cleanup alarms retain
priority. A busy snapshot includes fresh allocation state in `control_state`,
so cached financial data cannot hide a newly latched BUY block.

The last accepted residual BUY can remain open. With the engine running, SELL
protection, partial-fill reconciliation and the hard stop continue. No new BUYs
or automatic re-anchors are permitted. This differs from manual STOP, which
cancels both order sides and suspends ordinary engine risk monitoring. Neither
STOP/START, manual-pause release nor later SELL proceeds clears the allocation
latch. A validated manual grid installation does, in the same commit as the
new campaign. See [BACKTEST.md](BACKTEST.md) for rounding and failure handling.

The dashboard polls `/api/dashboard/snapshot` for portfolio, P&L, mark price,
orders and exposure together. A non-blocking engine-cycle guard plus one
read-only SQLite transaction prevents per-order accounting updates from being
presented as one completed view. No exchange request runs under that guard.
The old status/order routes remain available for compatibility and control
preflights.

During a cycle, the last complete cached snapshot retains its original ID and
time and is explicitly marked reconciling. Operational halt/liquidation status
is read separately so a fresh alarm is not hidden by cached financial data.
Without an initial cache, a 409 ENGINE_RECONCILING response requests a retry.
The UI keeps cached data during soft refreshes and retries reconciliation.

Ledger percentages use the engine mark and its sampling timestamp, not an
independently streaming quote. The live ticker/chart remain live and separate.
A live price crossing a resting row prompts faster local snapshot polling and
an Awaiting verification label. A price crossing alone never declares a fill
or removes an order; only reconciled exchange status does that.

## Total cost cap for new campaigns

New initial and post-liquidation grids enable `total_allocation_cap=1`.
Manual re-anchor proposals carry this policy into their replacement campaign.
The engine checks carried bot BTC cost plus the planned BUY-limit notionals
before accepting a proposal, before START, after cancellation reconciliation,
and before each BUY intent. Partial BUY quantities reduce outstanding commitment.
Unknown cost basis fails closed. Market BUYs require an explicit quote budget.
Existing reserved/unmonitored wallet BTC is not adopted or included.

A fill winning a cancellation race can increase carried cost. The post-cancel
check then blocks replacement orders and uses the existing sizing pause while
retaining client-side hard-stop monitoring. An active campaign is never sold
just to meet the allocation number.

The cap is defined on recorded cost basis plus BUY notionals, excluding future
fees and price appreciation. It is not a cap on mark-to-market value or a
maximum-loss guarantee. Exposure and form labels state this definition.

The already running pre-policy campaign is grandfathered during maintenance
resume. Its sizing is not retroactively changed; the UI labels legacy sizing
and shows current carried cost, pending BUY commitment and combined potential
cost. Saving a new/replacement grid opts into enforcement. Cap state is persisted
atomically with grid installation and survives restart.

The prior engine guard, exchange retry and daily macro policies are unchanged.
