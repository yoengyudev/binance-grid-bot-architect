# Rolling validation with a protected allocation cap

`walk_forward.py` is standalone offline research. It imports no application,
environment settings, credentials or trading database and never creates an
exchange client. It accepts only explicit CSV inputs and writes new artifacts.
It cannot deploy, alter the live configuration or enable the rejected
`budget_aware` policy.

## Protocol

Default: train on six trailing calendar months, select parameters, test the
next three months, then advance three months. Training and testing do not
overlap within a fold. Consecutive test windows are adjacent. A later training
window may use earlier test history after that history has become past data.
An incomplete final test window is excluded and reported rather than shifted.

The default parameter grid has six combinations: ATR half-width multipliers
1.5, 2 and 3; minimum gross grid returns 0.8% and 1.2%. The fixed reference is
2 ATR and 0.8%. Rounding/fee defense can make different ATR inputs produce the
same actual bounds; six settings are not six independent strategies.

Each training candidate runs independent fresh 500-USDT campaigns on both
declared OHLC paths, with 0.1% fees and 10 bps adverse MARKET slippage.
Selection maximizes the smaller of `(net return percent - maximum drawdown
percent)` across the two paths. Both paths must have at least three SELL
executions. Ties prefer fewer CAP_EXHAUSTED bars, then the baseline, then a
stable numeric ordering. If no candidate has enough activity, retain the
baseline and explicitly report the low-evidence fallback. Report every
candidate, including no-entry and configuration outcomes; unexpected data or
simulation errors block the run rather than silently removing a candidate.

Execution data is sliced strictly before the training boundary before it reaches
any candidate. Daily inputs also exclude unavailable daily closes. Every window
requires complete 50-hour execution/ATR warm-up and 200 preceding closed daily
observations for both macro gates. Missing, duplicated or malformed candles are
rejected. No gap filling, date substitution or automatic network fallback occurs.

The selection is saved before reading its simulated test performance. The same
selection is evaluated in OOS shadow accounts using 25 bps MARKET slippage;
stress results never enter training selection. These paths and cost assumptions
are sensitivity cases, not profit bounds or independent market samples.

## Test-account continuity

Selected and fixed-reference test accounts each start once, at the first test
window. Preserve cash, BTC, purchase cost, pending orders, stop floor, realized
P&L and cap/halt state across all later windows. Drawdown uses the continuous
marked equity, including inventory/dust and each modeled intrabar mark.

Selecting parameters does not force a re-anchor, sell inventory, reset a budget,
acknowledge dust or release a stop/cap latch. New parameters await the next
normally permitted grid installation. Existing orders retain their installed
geometry; the installation timeline records what was actually applied.
CAP_EXHAUSTED and post-stop HALT remain latched at a window boundary. No
automatic recovery is invented to make each quarter trade.

Training campaigns deliberately start fresh to compare candidate deployment
settings. They are not stitched into the test account. Only the consecutive
test windows form the continuous OOS performance record.

## Run

From the backend directory, supply complete execution, Binance daily and Yahoo
daily snapshots. Always choose a fresh output directory:

```powershell
.venv/Scripts/python.exe walk_forward.py --csv ../artifacts/independent-validation-20261018/data/2024/execution.csv --trend-csv ../artifacts/independent-validation-20261018/data/2024/binance_daily.csv --buy-signal-csv ../artifacts/independent-validation-20261018/data/2024/yahoo_daily.csv --start 2024-01-01T00:00:00Z --end 2025-01-01T00:00:00Z --workers 3 --output ../artifacts/my-fresh-wfo-run
```

The default manifest fixes candidate grid, objective, folds, source/input hashes,
capital and risk rules before training. Output includes all training metrics,
fills and events; per-fold selection records; cumulative OOS metrics; continuous
OOS fills/events/daily equity; and parameter installation timelines. Every
modeled mark checks cap and available-cash invariants. Daily curve compression
affects storage only; accounting and drawdown still evaluate every mark.

`--atr-multiples`, `--spacing-percent`, `--train-months`, `--test-months`,
`--min-training-sells`, `--slippage-bps` and `--stress-slippage-bps` define a
new experiment. Do not change them after inspecting test returns and still call
the resulting selection unseen. Unexpected errors leave partial evidence intact;
use a new registered output location for a revised experiment.

## Evidence limits

The initial 2024 and October 2025–October 2026 datasets have already been
inspected. Their rolling windows validate pipeline operation and retrospective
selection behavior; they are not a new blind holdout. The missing-data years
2019, 2020, 2021 and 2023 remain excluded for their existing coverage failures.
No aggregate return across separate historical accounts should be presented as
one tradable equity curve.

Full fills on touch, instant replacements, fixed filters and assumed intrabar
routes do not reproduce queues, partial fills, polling/cancel latency, provider
outages or crash liquidity. Walk-forward validation is not proof of edge or
Mainnet readiness. Keep policy changes and live deployment as separate decisions.

References: [Binance Spot filter definitions](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md)
describe notional and quantity increments; [QuantConnect walk-forward documentation](https://www.quantconnect.com/docs/v2/writing-algorithms/optimization/walk-forward-optimization)
describes rolling parameter optimization and its overfitting tradeoffs.
