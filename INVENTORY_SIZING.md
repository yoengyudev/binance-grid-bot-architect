# Experimental inventory-aware BUY sizing (feature #2)

Policy ID `inventory-taper-v1`; separate opt-in from feature #1. Disabled by
default, restricted to Testnet validation, and not deployed to EC2. No real
`.env` is changed. This is not a profitability claim or an optimized strategy.

## Fixed starting policy

Let A be the grid's allocated USDT, H be remaining **bot-owned purchase cost**,
and P be remaining pending BUY limit notionals. Reserved wallet BTC is excluded.
Partial BUY executions contribute their acquired cost to H and only their
unfilled remainder to P. Unsold dust remains part of H.

- At H <= 0.50 A, use the ordinary requested quantity.
- Between 0.50 A and 0.75 A, multiply requested quantity by
  `(0.75 A - H) / (0.25 A)`.
- At H >= 0.75 A, do not place another BUY.
- Every new BUY also fits remaining headroom `max(0, 0.75 A - H - P)`.
- Round down, never increase a requested order, and check both BUY and buffered
  paired SELL minimum quantities/notionals. If either is too small, defer that
  lane without globally pausing SELL execution or using a final-residual latch.

For a nominal 10-USDT BUY on a 100-USDT allocation, held cost of 50, 60, 70 and
75 USDT gives at most 10, 6, 2 and 0 USDT respectively, before pending-commitment
headroom and rounding. The 6- and 2-USDT examples would be deferred with the
bot's current 7-USDT minimum. Small accounts may have fewer active lanes than
the configured count; this policy does not force a minimum-size purchase.

These ratios use **purchase cost**, not live BTC market value or the wallet's
free cash. Falling prices do not create new budget. Rising market value can
exceed the cost-based ceiling; this is not a mark-to-market risk or loss limit.
Future fees and execution slippage are not included in pending notionals.
Actual booked acquisition commissions are included in held cost. Unknown or
invalid exposure fails closed for new BUY sizing.

## Execution and recovery

Set `GRID_INVENTORY_SIZING_ENABLED=true` only in an isolated Testnet validation
environment; restart that validation process to opt in. The flag accepts
true/false or 1/0. Mainnet activation is rejected. The existing configured seed
percentage is retained, not tapered; seeds above 75% are rejected. Every seed
MARKET BUY must provide a quote budget and fit the remaining headroom. A final
submission guard rechecks budget before creating the durable order intent.

Both lower-lane BUYs and upper-lane rebuys are tapered. New sizing never tops
up a sold lot. An already committed BUY is not repeatedly repriced or resized
merely because H changes. Its potential fill was reserved under the ceiling.
If enabling on an older grid, or newly reconciled costs make H+P exceed the
ceiling, cancel and verify bot-owned BUY LIMITs before allowing more BUYs.
Persist cancellation intents so response timeouts and restart recovery cannot
misclassify expected cancellations. Late/partial fills receive existing SELL
protection. Cancellation races, legacy holdings and booked fees can still leave
H above the ceiling; this feature blocks additional exposure and does not force
a sale to conceal that excess.

Deferred lanes are reevaluated during eligible normal cycles. A confirmed SELL
can release purchase cost through existing accounting. This does **not** clear
the existing CAP_EXHAUSTED latch or add feature #5's capital-recycling policy.
Cash preflight clips its nominal funding requirement to inventory headroom;
nominal geometry and exchange-minimum validation still apply.
Macro and regime gates, free-balance checks, the total allocation cap, exchange
minimums, manual pauses and reviewed reset restrictions still apply. Rebuilds
that need BUY permission are deferred without inventory headroom; existing
sell-only recovery can still restore exits for held BTC.

The hard stop, SELL prices, allocation amount, number of configured levels,
automatic re-anchoring and account-loss rules are unchanged. Risk exits are
checked before inventory-budget synchronization. Broader ledger-integrity
failures can still invoke existing engine safety halts.

## Telemetry and checks

`GET /api/bot/status` adds `inventory_sizing`, with the policy ID, enabled and
availability flags, cost basis, ceiling, pending commitment, sizing factor and
headroom. It does not contact Binance. SQLite records deduplicated per-lane
`INVENTORY_BUY_SIZING` decisions and durable `inventory_pause_buy:*` intents.
No additional dashboard panels or Telegram notifications are added.

```powershell
.venv/Scripts/python.exe -m unittest tests.test_inventory_sizing tests.test_regime_execution tests.test_api -q
.venv/Scripts/python.exe -m unittest discover -s tests -q
```

Functional tests cover taper boundaries, rounding and minimums, sequential
reservations, 100-USDT grids, guard bypass attempts, unknown exposure, reserved
BTC exclusion, unchanged baseline behavior, normal SELL replacement, retained
cap latches, cancellation races/timeouts, restarts, and hard-stop priority.
Reviewed grid resets can complete with verified inventory-deferred BUY lanes;
an unknown-exposure deferral never certifies a reset as complete.
Compare this exact policy with the disabled baseline before claiming a trading
benefit. It can reduce both losses and profitable activity; small orders may
wait indefinitely if headroom or exchange minimums remain restrictive.
