# Experimental campaign dollar risk budget (feature #3)

Policy `campaign-dollar-risk-v1` is disabled by default and restricted to
isolated Testnet validation. No runtime `.env` or EC2 configuration is changed
by this implementation. It is a loss-control experiment, not evidence of an
improved trading return or Mainnet readiness.

## Configuration and campaign boundary

`GRID_RISK_BUDGET_ENABLED=true` requires an explicit positive finite
`GRID_RISK_BUDGET_USDT`, strictly smaller than allocated capital. The flag
accepts true/false or 1/0. Mainnet activation is rejected. The example amount
of 2 USDT is illustrative, not a recommendation or an optimized parameter.

Activation requires a ledger without existing orders, realized trades or dust
reserves. Use a separate state directory with `grid_testnet.sqlite`; never
delete or reuse the running bot's database. The policy ID, symbol, dollar
budget and exit latch are persisted. Restarting, changing allocation, saving
another grid or re-anchoring does not restart the loss allowance. Changing or
disabling the budget in the same ledger is rejected. Ordinary factory reset
is blocked so it cannot erase consumed risk or unlock a completed risk exit.
Starting a new campaign requires a reviewed, separate ledger after accounting
for existing orders and inventory; this feature does not automate that handoff.

## Entry sizing

Let B be the dollar budget, S the current hard-stop price, H remaining bot
purchase cost including known acquisition fees, Q remaining bot BTC, and R
booked campaign realized P&L plus the conservative reserve for unbooked SELL
losses. Reserved manual wallet BTC is excluded. The fixed planning assumptions
are f = 0.001 commission per side and s = 0.0025 market-exit slippage.

```
exit_value(price) = price * (1 - s) * (1 - f)
held_risk = max(0, H * (1 + f) - Q * exit_value(S))
pending_unit_risk(price) = max(0, price * (1 + f)^2 - exit_value(S))
pending_risk = sum(remaining BUY quantity * pending_unit_risk(limit price))
modeled_risk = max(0, -R) + held_risk + pending_risk
headroom = max(0, B - modeled_risk)
```

Profits do not increase the entry budget above B. The extra fee allowance is
conservative when acquisition fees are already confirmed. A partial BUY moves
executed quantity into held cost and retains its unfilled commitment. Updating
fee or execution information can change the estimate. Partial or late SELLs
reserve negative P&L until ordinary trade accounting catches up; positive
unbooked SELL proceeds do not grant more buying permission.

New LIMIT BUYs are clipped to `headroom / pending_unit_risk`, never increased,
then rounded down and checked against BUY and paired SELL exchange minimums.
The initial seed uses at most its configured inventory fraction of available
risk headroom; nominal lower-lane sizing does not increase to absorb the
reduced seed. The final order-submission guard checks risk again, including
explicit MARKET quote budgets. Existing macro, regime, inventory, total-cost,
free-cash and cap-exhaustion rules still apply.

An unfundable seed or split initial SELL lane fails preparation before buying.
A later BUY lane below minimum is deferred without forcing an undersized
order or globally pausing SELL execution. A 100-USDT five-level grid with a
2-USDT risk budget and a distant stop may therefore be rejected or have fewer
funded BUY lanes. This feature does not promise to activate every level.

## Earlier campaign exit and recovery

At every normal trading cycle, after refreshing tracked fills:

```
estimated_exit_pnl = R + Q * exit_value(current engine price) - H * (1 + f)
exit when estimated_exit_pnl <= -0.80 * B
```

For B = 2 USDT the exit begins at an estimated loss of 1.60 USDT, leaving a
0.40-USDT cushion. This is an estimate, not an actual booked loss. Realized
profits can offset current floating losses in this campaign-net exit test;
this is not a trailing drawdown limit measured from peak equity.

The original price hard stop remains the first check and works independently
of the dollar projection. A dollar exit persists its latch before network
actions, cancels bot orders, sells only verified bot-owned inventory using the
existing liquidation state machine, and halts. Exact order identity, partial
execution, retries and dust handling use the existing recovery machinery.
Restarting after the latch was committed finishes that exit even if price has
recovered. Ordinary START and re-anchor cannot clear the latch. Telegram uses
the distinct `CAMPAIGN DOLLAR RISK LIMIT` label; price-stop alerts retain their
existing labels. Notifications remain best effort.

If exposure cannot be verified, new BUYs are blocked and bot BUY LIMITs are
canceled with durable cancellation markers. Existing SELL targets are kept.
Previously committed BUYs can still fill during cancellation. They remain
reserved until confirmed canceled or executed; over-budget reservations are
canceled and verified before more exposure is allowed. An unavailable dollar
projection cannot safely certify an early exit; the existing price stop and
engine safety checks remain in effect.

**The budget is not a guaranteed maximum loss.** Price gaps, fees above the
assumptions, slippage, cancellation races, exchange downtime, unknown inventory,
minimum-order dust and process or network outages can exceed it. This is a
software-controlled exit, not a standing exchange stop order.

## Telemetry and local verification

`GET /api/bot/status` adds `campaign_risk`, containing availability, policy ID,
budget, 80% trigger, modeled risk, remaining headroom, estimated exit P&L,
booked realized P&L, unbooked loss reserve and persisted latch. Policy decimals
are serialized as strings; `realized_pnl` inside this policy is the conservative
R above. The dashboard's normal realized-profit summary still comes from
committed trade history; neither this projection nor its reserves alter it.
Status does not fetch market data or submit orders for this projection.
No additional dashboard panel is introduced.

```
python -m unittest tests.test_campaign_risk tests.test_inventory_sizing tests.test_regime_execution tests.test_api -q
python -m unittest discover -s tests -q
```

Tests exercise actual engine paths with real temporary SQLite databases and a
deterministic offline exchange. Coverage includes sizing, minimums, partial
fills, missing data, budget persistence, all three features together, distinct
alerts, earlier exits, price-stop priority, reserved BTC exclusion, and recovery
after a crash or lost sale response. These are functional checks. A separate
comparison with the disabled baseline is still required to establish trading
performance. Automatic ATR re-anchoring and new capital recycling are not
part of feature #3.
