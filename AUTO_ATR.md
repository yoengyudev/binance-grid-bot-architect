# Experimental automatic ATR BUY-net adaptation (feature #4)

Policy `hourly-atr-buy-net-v1`. Default-disabled, Testnet-only, local
implementation. No EC2 process, runtime environment, live order, database or
dashboard layout was changed during implementation. This feature has not been
shown profitable and is not a Mainnet release.

## What moves

Automatic adaptation moves **future BUY entries**. It does not reprice existing
SELLs or the promised exit of an acquired lot. Each new BUY stores its exact
paired SELL price atomically with the durable order intent, before HTTP. Before
adopting automatic geometry on an existing grid, the engine also freezes the
original exits of its tracked BUY lots. A fill racing cancellation therefore
keeps its original exit, including after a restart. Partial fills use the existing
fee reserve, precision and SELL coverage rules. A missing required frozen exit
fails closed; the engine cannot infer it from a newer plan.

The original grid's configuration, seed anchor and initial SELL targets remain
intact. Its displayed configured lower/upper bounds remain the original manual
bounds. Actual adaptive BUY prices are visible in the order ledger; their
current entry band is also reported in `auto_atr.active.lower/upper` telemetry.
The adaptive band describes future entries, not the full range of carried SELLs.
The configured number of lanes and capital are unchanged. Upper-side lanes join
the adaptive BUY net only after their existing SELL has completed. A lane with
an unsold lot keeps its SELL; no second BUY is added on that lane.

## Fixed starting policy

Set `GRID_AUTO_ATR_ENABLED=true` only in isolated Testnet validation. It accepts
true/false or 1/0 and requires feature #1's range-market gate to be enabled
(`GRID_REGIME_FILTER_ENABLED=true`). Features #2 and #3 remain separately opt-in
and continue to restrict every order when enabled. The real `.env` is not edited
by these code changes. A ledger with a saved automatic plan cannot silently
disable adaptation in place; retire it through a reviewed flat reset or a new
isolated validation ledger.

- Fetch public Binance Mainnet Spot hourly candles using a separate,
  credential-free client. The daemon does not take the trading exchange lock.
- Use the production baseline's `closed_hourly_atr`: the latest 15 consecutive
  completed UTC hours and its fresh-window ATR calculation. Forming/future
  prices do not enter the ATR. This is separate from feature #1's longer-history
  Wilder regime indicators and the dashboard's visualization timeframe.
- Refresh in the background every 60 seconds or at the hour boundary. A failed
  fetch invalidates the cached signal, with retry backoff up to 300 seconds.
  Snapshots expire at 180 seconds or immediately at an hour rollover.
- Review once per newly completed hour. The first eligible adaptation can be
  queued after the seed fills. Subsequent applied plans require at least four
  completed hours since the previous plan **and** either a center drift of one
  previous spacing or an ATR change of at least 25%.
- Center the new BUY net on the current execution-market ticker. Spacing is
  `max(0.5 * closed_hourly_ATR / center, 0.004)`. Positive lane IDs are followed
  by upper-side rebuy lane IDs. Lane i buys at `center / (1 + spacing)^i`, rounded
  down. Its future SELL is at least `BUY * (1 + spacing)`, rounded up to a valid
  exchange price. A minimum 0.4% gross price return is required after rounding.

These are explicit experimental parameters, not optimized recommendations.
All adaptive BUY prices must fit above the existing hard stop. Nominal lower
BUY and paired SELL sizes must meet exchange minimums before replacing the old
net. If the proposal cannot fit, keep the old plan and orders. Actual placement
still passes current cash, inventory, dollar-risk, total-allocation, precision
and paired-SELL minimum checks; some proposed lanes can remain unfunded.

The complete plan contains potential prices for every lane, but a price in the
plan is not permission to buy. Manual pauses, unavailable/blocked SMA200 data,
non-RANGE regimes, stop controls, pending manual resets, and CAP_EXHAUSTED retain
priority. The automatic policy never releases a cap or risk latch, changes the
hard stop, clears realized losses, increases allocation, buys an extra seed,
sells held BTC to make room, or starts an idle/halted bot.

## Cancellation, recovery and scope

Persist the candidate and original per-lot exits first. On the following cycle,
cancel **only bot BUY LIMITs**, using durable `auto_atr_cancel:*` markers. Confirm
the cancellations and any racing fills before activating replacement prices.
An uncertain response or crash leaves a recoverable pending candidate; there
is no assumption that budget was released. New BUY submissions remain blocked
while a candidate is pending. Existing SELL execution continues normally.

Before activation, data must still represent the same completed hour, the
market must remain within half a spacing of the proposed center, and the net
must remain above the retained floor. Otherwise withdraw the candidate and
retain the previous active plan. Evaluate again at the next completed hour.
Successful activation does not place a MARKET order or change realized P&L.
It only allows normal guarded lane placement to use the new BUY prices.

Unavailable ATR blocks new BUYs and cancels existing BUY LIMITs while preserving
their frozen exits and current SELLs. Price hard stops and the campaign-dollar
exit run before the ATR worker or adaptation logic. Persistent plans and cooldowns
survive process restarts. Plans bind to the grid geometry and seed identity; an
explicit manual grid reset has priority and starts a new plan for that new grid.
Feature #3's campaign dollar budget and loss history remain independently intact.

While feature #4 is enabled, the older four-hour **full-grid** upper-breakout
reset is suppressed: it could cancel/rebuild carried SELLs. Manual recentering
and existing safety recovery still require their usual review. Automatic
adaptation does not override a downside safety pause or remove the manual START
and reset checks. Existing positions can therefore still wait for their saved
targets. There is no forced daily trade or guaranteed holding period.

This implementation adds no capital-recycling feature (#5) and does not clear
CAP_EXHAUSTED. It uses the existing minimum-return guard, not a new fee model
(#6). A 0.4% gross target is not a guaranteed net profit: actual commissions,
slippage, gaps and stop exits can exceed the margin. Testnet execution anomalies
can still trigger stops or produce unrealistic fills even though ATR and regime
inputs come from Mainnet.

## Read-only status and validation

`GET /api/bot/status` adds `auto_atr`: policy ID, data source, signal availability,
closed-hour ATR, active/pending plan, last evaluation hour and deduplicated
decision. Status never starts an ATR refresh or changes orders. No new dashboard
panel or Telegram message is added. SQLite journals `AUTO_ATR_DECISION`,
`AUTO_ATR_REANCHORED`, `AUTO_ATR_CANDIDATE_WITHDRAWN`, and cancellation retries.

```
python -m unittest tests.test_auto_atr tests.test_campaign_risk tests.test_inventory_sizing tests.test_regime_execution tests.test_api -q
python -m unittest discover -s tests -q
```

Tests use the actual engine, temporary SQLite ledgers, a deterministic exchange,
and injected public-data workers. They cover closed candles, missing data,
cooldowns, fee-safe rounding, preserved exits, partial/late fills, cancellation
timeouts, restarts, frozen metadata, stop priority, all four features together,
and read-only API projections. They establish functional behavior. A separate
prospective comparison against the unchanged baseline is needed to assess
profitability, trading frequency and the cost of cancellations or deferred lanes.
