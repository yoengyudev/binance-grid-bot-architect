# Iteration 2 production policy

The production strategy is Daily SMA200 deployment gating, at least 0.8% gross
adjacent-cycle grid return, and a fixed catastrophic stop 15% below the lower bound.
This is the selected operational policy, not a claim of demonstrated profitability.

## Deployment and restart rules

- Initial trading, every manual START/resume, and automatic upward breakout
  re-anchors require current execution-market price strictly above Daily SMA200.
  Equality, missing history, gaps, duplicate daily candles and stale history block
  deployment. Signals use the latest 200 consecutive fully closed UTC days.
- Production service entry points inject a separate credential-free Binance
  production Spot OHLCV client. Testnet resets cannot provide the required history;
  Testnet execution remains on Testnet, with the production daily SMA as its macro reference.
  Each daily signal is cached only through the current UTC day. Failed refreshes
  invalidate the signal. Public history has the same bounded HTTP timeout/rate-limit
  circuit protection as execution I/O.
- Saving a draft while ready/IDLE places no orders and need not pass the SMA gate.
  START and reset execution recheck the live gate. A gate failure before re-anchor
  cancellation preserves the working grid. A failure after cancellation retains
  the durable reset intent while hard-stop checks continue.
- The Binance SMA remains an entry/redeployment filter. The runtime BUY kill
  switch additionally uses Yahoo `BTC-USD` daily closes via `yfinance`; see
  [MACRO_TREND.md](MACRO_TREND.md). A downtrend, missing/stale history or Yahoo
  failure cancels bot BUY limits and blocks new BUYs while retaining SELLs and
  the fixed catastrophic stop. This does not adopt the no-stop experiment.
- A completed or failed liquidation remains HALTED. Recovery requires human
  review, resolution of any dust/cleanup alarm, saving/reviewing a new grid, then
  manual START with SMA approval. There is no automatic post-stop restart or timer.

## Spacing and stop

- Backend checks exchange-rounded adjacent BUY/SELL cycles for at least 0.008
  gross return. ATR auto-fill and automatic re-anchor proposals widen narrow
  bounds; manually entered tight grids are rejected. Fees, slippage, carried cost
  and stop losses are separate; 0.8% spacing does not guarantee net profitability.
- Fresh campaign stop = floor_to_tick(lower_bound * 0.85). Config loading, initial
  API setup and liquidation recovery derive this server-side. The dashboard shows
  the calculated stop and does not accept an arbitrary new-campaign floor.
- The historical `advance_trailing_stop` method now updates high-water telemetry
  only. Price highs never move the liquidation floor. The SQLite `trailing_stop`
  key remains for compatibility; its name no longer implies a trailing strategy.
- A successfully installed re-anchor uses max(retained_stop, new_lower * 0.85),
  rounded down by exchange tick. Rejected/deferred plans do not change the floor.
  Explicit authenticated **Raise Hard Stop** remains an operator tightening
  override; it cannot lower the floor. That action sets a new fixed floor and does
  not re-enable price trailing.
- Existing saved grids retain their bounds, positions, cost basis, orders and
  current stop. The release does not silently lower a tighter legacy floor to
  widen its risk range. New campaigns use the 15% rule; existing tighter floors
  remain protected until the operator completes the normal recovery workflow.
- Automatic stops still require positive tracked bot BTC, cancel bot grid orders,
  reconcile cancellations/fills, and market-sell bot inventory through the existing
  liquidation protocol. Reserved wallet BTC remains excluded. Panic liquidation,
  STOP, safety pause and database recovery retain their distinct workflows.

## Validation and deployment

`tests/test_iteration2_policy.py` covers closed-day signal availability, invalid/
stale data, strict gating, caching, public-client isolation, deployment failures
before order writes, active-stop protection during macro outages, fixed-floor
restart persistence, derived initial stops, manual recovery and fee rejection.
Legacy ratchet tests now assert fixed behavior; retained-floor race tests explicitly
restore a legacy saved floor instead of inventing a new trailing move.

The backtester defaults to the same policy with manual recovery. `--stop-mode none`,
`--stop-mode trailing`, different trend parameters and `--restart-after-stop` remain
research comparisons only. A CLI research flag cannot change production settings.

This release is committed for review/deployment. Committing does not connect to
EC2, change exchange orders, start trading, or switch Testnet to Mainnet.
