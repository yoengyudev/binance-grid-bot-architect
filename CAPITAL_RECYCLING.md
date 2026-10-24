# Experimental confirmed-SELL capital recycling (feature #5)

Policy `confirmed-sell-recycling-v1`. Default-disabled, Testnet-only, and not
deployed. Enable with `GRID_CAPITAL_RECYCLING_ENABLED=true` in an isolated
validation environment. The flag accepts true/false or 1/0. This implementation
does not edit a real `.env`, contact EC2, or prove the policy profitable.

## Purpose and scope

The existing final-residual policy latches CAP_EXHAUSTED when a full BUY cannot
fit. Its disabled baseline still requires manual review to release that latch.
Feature #5 adds a narrowly controlled automatic release: a newly confirmed,
profitable ordinary SELL can make room for a full replacement BUY. This is
reuse of sale-freed capital **inside the configured allocation**, not automatic
profit compounding. A 100-USDT allocation stays 100 USDT after profitable sales.
External wallet deposits, price appreciation, elapsed time and ATR changes do
not increase it or authorize a release.

Activation requires either an existing strict total-allocation cap or a fresh
isolated ledger. On a fresh ledger the feature enables that cap before orders
can be submitted. Existing uncapped trading history is rejected; there is no
silent conversion of an exposed legacy run. Opting into an already capped
CAP_EXHAUSTED ledger starts its confirmation fence at activation, excluding
already booked sales. A saved recycling ledger cannot silently disable the
feature in place. A reviewed flat reset/new validation ledger is required to
retire it; feature #3's stricter history-preservation rules still apply.

## Durable cap episodes and qualifying sales

At the first exhaustion in each episode, persist a unique episode ID, the
highest booked trade ID, already observed unbooked/partial/terminal SELL IDs,
and any accepted final-residual BUY ID. Repeated cycles do not move this fence.
Manual reviewed cap resets clear the active episode, while historical trade
records and journal entries remain subject to the existing reset rules.

A qualifying SELL must:

1. Be a current, fully filled bot-owned **LIMIT SELL**, with exchange identity,
   a tracked BUY parent and matching committed trade accounting.
2. Be booked after the episode's trade-ID fence and not be one of its already
   observed partial/terminal sales. A previously booked profit cannot be reused
   simply because a later cap episode started.
3. Have positive booked P&L after the ordinary accounting fee model, positive
   sold quantity, and a matching complete execution snapshot. When the exchange
   supports per-trade fee lookup, the SELL snapshot must contain verified trade
   fees. Offline fixtures without that API retain the existing estimated-fee
   accounting; estimated fees are not a guarantee about actual future costs.

Partial SELLs, missing/unknown executions, unrelated wallet transactions,
archived historical sales, losing sales and MARKET liquidation/panic sales do
not qualify. The fence measures locally observed confirmation, not an exact
exchange execution timestamp: a fill racing episode creation can qualify once
it is newly verified. Budget and cash checks still apply to that confirmation.

An accepted final-residual BUY must first be exchange-confirmed FILLED or
CANCELED, with a valid cumulative fill snapshot. Remaining partial BUY quantity
and held cost remain reserved. A missing intent, absent exchange identity or
uncertain cancellation is not interpreted as released room. An earlier final
maker rejection whose intent was discarded remains a manual-review case;
feature #5 does not guess that it is safe to reopen.

## Full replacement and unchanged protections

Choose the first eligible waiting BUY in the existing lane priority: upper-side
rebuys first, then lower lanes. Existing open orders and lots awaiting SELL
protection are not replaced. Entries at/beneath the hard stop or at/above the
current market are ineligible. Feature #4's active BUY prices and frozen SELL
targets are used when present; otherwise use the original geometry.

Require the full normal quantity, rounded down. A size reduced by inventory or
dollar-risk controls cannot unlock the cap. Both the BUY and its conservatively
buffered paired SELL must satisfy minimum quantity/notional rules. A too-large
first eligible replacement does not authorize a smaller residual or a jump to
a later, smaller lane. Existing cash and total-cost constraints also apply:

```
H = exact Decimal purchase cost of remaining bot BTC, including known fees
P = outstanding unfilled BUY commitment
N = proposed full BUY notional
H + P + N * 1.001 <= configured allocation
free quote >= N * 1.001
```

The 0.1% allowance is an entry-fee planning reserve for this release. It does
not change other policies' fee models. Unknown inventory, costs or commitments
block release. Reserved manual wallet BTC is excluded; tracked unsold dust
continues to consume position cost under normal ledger accounting.

SMA200, the range gate, inventory taper/ceiling, campaign dollar risk, fresh ATR
permissions, maker-only entries, safety modes, pending manual reset/coverage,
STOP, cleanup/fault alarms and engine IDLE/HALTED states retain priority.
Recycling never lowers a stop, clears a campaign loss, releases a risk latch,
changes held-lot SELL targets, increases capital, buys an extra seed or starts a
halted engine. Both price and dollar exits run before recycling in a cycle.
Enabling feature #5 does not automatically enable features #1–#4.

## Atomic reservation and failures

After checking current wallet cash, repeat ledger eligibility inside the same
SQLite transaction that creates the BUY intent. Clearing the cap, consuming
the episode's sale evidence, recording the replacement's exact client ID and
saving feature #4's frozen exit commit together **before HTTP**. A failure in
that transaction rolls back all of them. Exchange/notification I/O never runs
inside it. Existing runner fencing and final submission guards remain active.

A lost/uncertain submission retains the specific intent and its allocation
reservation. Existing exact-ID reconciliation must resolve it; the policy does
not POST a second purchase or reuse the consumed SELL to unlock a new episode.
A definitive maker/balance rejection or local circuit rejection discards the
unsubmitted intent and starts a fresh locked episode atomically. It requires
another newly confirmed profitable SELL rather than retrying the same credit.

An accepted release allows ordinary guarded lane maintenance to continue. The
bot can promptly enter CAP_EXHAUSTED again if another full BUY cannot fit;
that is a new episode, and the newly reserved order remains working. There is
no promise of continuous orders, a daily trade count, or a guaranteed profit.
More turnover can increase fees, inventory losses and stop exposure. This is
an experimental execution policy, not evidence that trading risk is reduced.

## Telemetry and local verification

`GET /api/bot/status` adds `capital_recycling`: availability, fixed configured
allocation, `profit_compounding=false`, current episode, last release/reserved
BUY identity, held/pending costs and deduplicated decision. It does not submit
orders, refresh market data, release a cap, or alter realized P&L. No dashboard
panel or Telegram alert is added. SQLite journals `RECYCLING_DECISION` and
`RECYCLING_RELEASE_RESERVED`; the latter records reservation, not a realized
profit or proof of exchange execution. Ordinary trade history still alone
determines the dashboard's realized-profit totals.

```
python -m unittest tests.test_capital_recycling tests.test_residual_budget tests.test_auto_atr tests.test_campaign_risk tests.test_inventory_sizing tests.test_regime_execution tests.test_api -q
python -m unittest discover -s tests -q
```

Offline tests use the actual engine and temporary SQLite ledgers. They cover
real cap exhaustion, episode fences, partial/old/losing sales, pending residuals,
fee data, full-order headroom, rollback, crash windows, lost responses, definite
rejections, restarts, stops, read-only status and all five features together.
The historical research under `research/CAPITAL_RECYCLING.md` remains unchanged;
its simulator is not used as a substitute for these execution checks. A
separate prospective performance comparison is still required.
