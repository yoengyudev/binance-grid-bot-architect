# Experimental fee-aware net-edge validation (feature #6)

Policy `net-edge-validation-v1` is default-disabled and Testnet-only. This local
implementation does not change EC2, a real `.env`, production data or live
orders. It adds execution checks, not a profitability claim. It does not fetch
a live account fee schedule, optimize strategy settings or alter historical
backtest results.

## Explicit assumptions

`GRID_FEE_VALIDATION_ENABLED=true` requires all five percentage inputs:

| Environment variable | Example percentage | Meaning |
| --- | ---: | --- |
| `GRID_BUY_FEE_PERCENT` | 0.10 | Entry commission ceiling |
| `GRID_SELL_FEE_PERCENT` | 0.10 | Exit commission ceiling |
| `GRID_ENTRY_SLIPPAGE_PERCENT` | 0.05 | Adverse entry-price planning buffer |
| `GRID_EXIT_SLIPPAGE_PERCENT` | 0.05 | Adverse exit-price planning buffer |
| `GRID_MIN_NET_RETURN_PERCENT` | 0.05 | Minimum modeled net return on allocated lot cost |

These are illustrative assumptions, **not verified Binance account rates** or
recommendations. Fee tier, discounts, promotions and commission assets must be
reviewed separately before choosing real inputs. The flag accepts true/false
or 1/0. Inputs must be finite, nonnegative and below 100%; the minimum net return
must be strictly positive. Missing inputs fail instead of silently assuming zero.
Mainnet activation is rejected.

The policy ID, symbol and normalized inputs persist in SQLite. Changing or
disabling them in the same ledger is rejected. Use a reviewed flat reset/new
isolated validation ledger for a new cost experiment; feature #3's stronger
history-preservation rules remain in force. No existing commission or realized
profit record is rewritten by a policy change.

## New-entry arithmetic

Let b and s be entry and exit commission fractions, e and x the adverse price
buffers, P the proposed BUY limit/reference price, T its promised SELL target,
and m the required net return. Environment percentages are divided by 100.

```
modeled net return = T * (1-x) * (1-s) * (1-b) / (P * (1+e)) - 1
required gross return = (1+m) * (1+e) / ((1-b)*(1-s)*(1-x)) - 1
```

Entry commission is modeled as loss of usable base quantity, conservatively
covering an equivalent quote commission. Exit commission reduces quote
proceeds. Buffers are stress assumptions; the actual orders keep their original
limit prices. Resting limits are not submitted at worse prices merely because
the model reserves a buffer.

A new BUY must meet this net floor **and** the existing 0.4% gross floor after
exchange price precision. Future SELL quantity is rounded down from usable
base, retaining the larger of the modeled entry commission allowance and the
existing base reserve. BUY and paired SELL quantities/notionals must both satisfy
exchange minimums. Orders are never increased to manufacture a valid projection.

For gross quantity Q, modeled usable quantity is `Q*(1-b)`. If precision leaves
some of that quantity unsold, its proportional purchase cost stays with the
retained dust. It is neither a realized loss nor released capital. Projected
net profit concerns the sold portion only. All computations use Decimal.

Fresh grid preparation and reviewed geometry changes reject spacing that cannot
meet the configured net floor. The final submission guard checks actual rounded
order sizes before a durable BUY intent is created. A MARKET seed must have an
explicit quote budget and valid modeled exits on **all** initial SELL lanes.
The weakest seed exit is used in its successful decision record.

With feature #4, a proposed ATR BUY net that fails these checks is deferred,
keeping the previous plan and existing orders. The policy does not silently
widen geometry or move held-lot targets to rescue projected profit. With feature
#5, a fee-rejected replacement cannot release the cap or reserve an order.
All other sizing, cash, inventory, cap and dollar-risk constraints still apply.

## Observed commissions and existing positions

Each cycle audits persisted execution snapshots without extra network I/O.
Base/quote commission totals must be valid and internally consistent. Their
normalized effective rate must not exceed the configured side's allowance.
Mixed commission assets/price averaging can make this a conservative effective
rate check; the message does not establish that an exchange overcharged.

If the exchange exposes per-order trade details, executed quantities require
trade-verified commissions. Existing execution reconciliation supplies those
details; this feature does not invent missing fees or start a new authenticated
fee worker. Unverified partial SELLs can therefore pause new entries until
normal final-fill verification or reviewed recovery completes. Older/offline
fixtures without the trade API retain explicit configured estimates and are
identified as estimated, rather than claimed verified.

A positive commission in an unvalued third asset such as BNB blocks new BUYs;
the policy does not assume that its quote cost is zero or invent a historical
conversion rate. An explicit zero commission is valid. Unknown/malformed cost
data or rates above the model also block entries. When the model is exceeded,
review the assumptions and reconcile costs before beginning a new validation
ledger; old evidence is not automatically erased after a sale.

On a blocked audit, cancel only bot BUY LIMITs using durable `fee_pause_buy:*`
markers. Retain exact fill identities through timeouts/restarts. A BUY that
fills during cancellation gets its original frozen SELL exit. Canceling a
small partial can leave a below-minimum fragment; existing sizing/dust recovery
rules apply, and its cost remains held. No automatic top-up is introduced.
An individual unprofitable pending BUY can also be canceled without disabling
otherwise eligible lanes.

New BUY intents save their validated SELL target before HTTP. Seeds save their
initial SELL targets. When ATR freezing is also active, both records must agree.
**Existing exits are never vetoed because their net projection is poor.**
Existing SELL prices are preserved, and actual acquisition cost includes known
entry fees once. It does not receive another modeled entry fee or entry-price
buffer. Missing unverified acquisition costs use the more conservative of
known cost and the configured estimate, rather than adding both together.
Read-only warnings identify held exits below the modeled edge; they do not
force a sale or move its price. Price and dollar emergency exits run before
fee auditing and remain independent of the profit gate.

## Accounting, telemetry and limitations

`GET /api/bot/status` adds `fee_validation`: the explicit policy, fractional
rate units, required gross return, commission-source audit, BUY permission,
held-exit warning counts and the latest deduplicated decision. A decision
includes reference BUY price, target, quantity, modeled sell size, retained
base and projected net return/profit. Status does not fetch fees, place orders
or book projected profit. No dashboard panel or Telegram message is added.

Actual realized P&L remains entirely in existing confirmed trade accounting.
Known base fees affect acquired quantity, quote fees affect cost/proceeds, and
the existing fee-basis classification remains intact. Third-asset valuation
limitations in historical accounting are not silently repaired or presented as
exact by this feature. Forecasts, commission warnings and actual booked P&L are
separate values. SQLite journals deduplicated `FEE_VALIDATION` decisions and
cancellation retries, not artificial trades or profits.

Passing the model does not guarantee a net gain. Assumptions can be wrong;
market seeds can exceed buffers, stops can close at losses, fee schedules can
change, and small orders can be constrained by precision/minimums. Queue and
fill probability, hosting costs, taxes, depegs and all other risks are outside
this projection. It can reduce activity, and unverified fees can require review.

```
python -m unittest tests.test_fee_validation tests.test_capital_recycling tests.test_residual_budget tests.test_auto_atr tests.test_campaign_risk tests.test_inventory_sizing tests.test_regime_execution tests.test_api -q
python -m unittest discover -s tests -q
```

Offline engine/SQLite tests cover both-side fees, execution buffers, precision,
dust cost allocation, no double charging, unsupported/missing commissions,
frozen exits, cancel/fill races, profile persistence, emergency exits, unchanged
realized accounting, read-only API behavior and all six features together.
Separate prospective performance validation is still required before deployment.
