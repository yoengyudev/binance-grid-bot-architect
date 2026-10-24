# Experimental hourly market-regime BUY gate

This implements feature #1 only. It is disabled by default and restricted to
Testnet validation. It is not an AI predictor or evidence of better returns.
The running EC2 release, environment, SQLite ledger and orders are untouched.

## Fixed v1 classification

Policy ID: `hourly-regime-v1`. Source: credential-free Binance **Mainnet Spot**
hourly candles for the configured symbol, even when executing on Testnet.
Use exactly the latest 240 consecutive completed UTC hourly candles. Require
fresh coverage through the last closed hour; missing, duplicate, misaligned or
invalid OHLCV fails closed. Ignore all forming/future-bar prices.

- Wilder ADX14, +DI14, -DI14 and ATR14 use the first 14 actual transitions as
  their smoothed-sum seeds. ADX seeds from the first 14 valid DX values.
- EMA20 seeds from the first 20 closes. Normalized slope is
  `(EMA20[t] - EMA20[t-3]) / (3 * ATR14[t])`.
- `RANGE`: ADX <20 and absolute normalized slope <=0.1. BUYs may proceed only
  if every existing macro, capital, sizing and safety check also permits them.
- `DOWNTREND`: ADX >=25, -DI > +DI, and normalized slope <-0.1.
- `UPTREND`: ADX >=25, +DI > -DI, and normalized slope >0.1.
- `TRANSITION`: all other valid combinations. All three non-range states block
  new BUYs. Invalid/unavailable/stale data also blocks BUYs.

These thresholds are explicit starting hypotheses, not optimized parameters.
There is no directional prediction, statistical confidence claim or promise
that the gate will anticipate a crash. It can miss trends, react late and
reduce profitable activity. No profitable backtest for this policy is claimed.

## Runtime behavior

Set `GRID_REGIME_FILTER_ENABLED=true` in an **isolated Testnet validation**
environment and restart that validation process to opt in. Keep it `false` for
the existing baseline; no current `.env` was changed. Invalid flag values fail
configuration validation, and enabling this experimental filter on Mainnet is
rejected. Direct constructor injection of a monitor is supported for tests.

A dedicated public client fetches at most once per 60 seconds on success,
also refreshing at the hour boundary. A single background daemon performs
network I/O without acquiring the trading exchange lock. Failures invalidate
positive cached permission and retry after 30, 60, 120, 240, then 300 seconds.
Signals expire after 180 seconds or immediately when their closed-hour boundary
no longer matches. Calls reading API telemetry never start a fetch.

When blocked, persist cancellation intents and cancel only bot-owned BUY
LIMITs. Cancel failures retry; fills winning a cancellation race are reconciled
and receive the existing paired SELL. A below-minimum partial fill can still
invoke the existing sizing pause and require review. Existing SELLs remain
working. No SELL target, grid boundary, allocation or hard-stop rule is changed.
The ordinary cycle evaluates the hard stop before synchronizing this gate.

The final BUY submission guard covers seed MARKET buys, ordinary LIMITs,
replacement buys, reset placement and final-residual buys. Whole-grid resets
are deferred while blocked, preserving existing protective SELLs; the existing
sell-only resume exception can restore exits for owned BTC. When RANGE returns,
eligible canceled BUY lanes can resume under all existing constraints. The
capital-exhaustion latch remains unchanged; this feature does not recycle funds.

This filter does not automatically liquidate on a DOWNTREND classification and
does not suppress an executable Testnet or Mainnet hard-stop price breach.
Mainnet-derived indicators cannot prevent a Testnet-only flash crash.

## Observability and validation

`GET /api/bot/status` includes `regime_filter`: enabled flag, policy ID, public
source, closed-hour snapshot, ADX/DMI/ATR/slope, availability, reason and BUY
permission. Reasons also appear in logs and SQLite transitions under
`REGIME_BUY_GATE_CHANGED`; durable cancellation markers survive restarts.
No extra dashboard panels or Telegram messages are added by this feature.

Run offline checks from this repository:

```powershell
.venv/Scripts/python.exe -m unittest tests.test_market_regime tests.test_regime_execution tests.test_api -q
.venv/Scripts/python.exe -m unittest discover -s tests -q
```

Functional tests establish rule execution and safety behavior. Compare this
exact policy with the disabled baseline using the actual engine and realistic
costs before claiming a benefit or authorizing a deployment.
