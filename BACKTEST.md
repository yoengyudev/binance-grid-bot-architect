# Grid strategy backtest

## Accepted residual BUY rule

The live engine now allows one final residual BUY when a requested BUY LIMIT
cannot fit the remaining total allocation. Use `--replacement-buy-policy residual`
to model it. `fixed` remains the default historical comparison; it globally
pauses and cancels BUYs on cap rejection. The earlier `budget_aware` experiment
is **rejected for Live/Mainnet** and retained solely to reproduce its research.
The live engine has no switch or configuration path for that rejected policy.

The residual policy rounds the final base quantity down within remaining
cost-cap room and validates both the BUY and conservative reverse SELL against
exchange/bot minimums. It cannot spend an arbitrary final fraction smaller than
a quantity increment. In the reproduced example, 82.8181102459 USDT of room at
41,284.67 buys 0.00200 BTC (82.56934 USDT), rather than rounding up past the cap.

After that final intent, enter persistent `CAP_EXHAUSTED`. If no valid residual
can fit, enter it without placing an order. Keep previously accepted orders,
including the final BUY, eligible to fill. Reconcile partial fills, place SELL
protection and evaluate the stop while preventing all new BUYs and automatic
re-anchors. SELL proceeds, macro recovery, process restart and STOP/START cannot
release the latch. Only a successfully validated manual replacement/new campaign
can release it. The initial reservation of a normally funded ladder does not
itself trigger the latch; the trigger is the first underfunded new BUY request.

The live latch commits atomically with its final order intent before HTTP. An
uncertain response retains the original client ID and latch. Even a definitive
Post-Only/balance rejection of that final attempt remains fail-closed pending
manual review; it cannot open an alternate BUY or silently retry a larger order.
Macro/stop/manual controls can still cancel accepted orders for their own reasons.
The underlying allocation validator, cap tolerance, fee exclusions and legacy
campaign cap flag are unchanged. `CAP_EXHAUSTED` is a separate durable allocation
state, so liquidation and manual safety modes retain their normal priority.

`walk_forward.py` and [WALK_FORWARD.md](WALK_FORWARD.md) provide the protected
rolling validation pipeline. `atr_multiple`/`--atr-multiple` affects initial ATR
proposals in research only; live ATR proposal settings remain unchanged.

## Rejected budget-aware BUY sizing experiment

`--replacement-buy-policy budget_aware` requires
`--allocation-policy total_cost_cap` and is an archived research comparison.
It must not be used for live execution or walk-forward parameter selection.

During grid maintenance, the variant never increases a requested BUY quantity.
It floors the quantity to the lesser of the requested amount, remaining cost-cap
room, and free quote cash. Quote-denominated BUY fees are conservatively reserved
for existing resting BUYs and the proposed BUY during sizing; the underlying
cost-cap definition still excludes future fees. Both the reduced BUY and its
post-commission, rounded reverse SELL must satisfy order minimums. If they cannot,
only that lane waits for a later eligible maintenance cycle; peer orders are not
canceled and the global sizing pause is not entered for this budget shortage.
The existing macro, bounds, Post-Only and hard-stop rules still apply. Unknown or
invalid sizing still fails through the existing validation paths.

Seed budgets and initial/re-anchor planning keep their existing fixed allocation
rules; this experiment does not dynamically shrink re-anchor plans, recycle
retained dust, raise the budget, sell inventory to fit, or restart after a stop.
Lane priority stays unchanged: upper-lane rebuys precede lower BUY lanes, so a
small remaining budget can leave later lanes unfunded. A RUNNING state can still
include macro BUY pauses and waiting lanes; it is not proof of continuous trading.

Results record `replacement_buy_policy`, `budget_buy_resizes`,
`budget_buy_deferrals` (distinct waiting episodes), and
`budget_deferred_lanes_at_end`. Events identify resized, deferred and resumed
lanes. Macro pauses, grid installations and terminal stops clear the set of
currently waiting lanes. Add the research flag to a documented offline command,
and choose a fresh `--output` directory to preserve the baseline.

The paired historical experiment and independent accounting audit are preserved
in `../artifacts/budget-aware-sizing-20261019/`. It uses previously inspected data
and is not blind out-of-sample validation, WFO, or a production release.

## Total allocation policy

New runs default to `--allocation-policy total_cost_cap`, matching the live
engine's policy for new campaigns. Before planning a campaign or re-anchor,
and before placing each new BUY, tracked BTC purchase cost plus outstanding
BUY notionals must fit within the campaign allocation (absolute tolerance
0.000001 USDT). An initial proposal also reserves its seed quote budget.
The cap excludes future fees, market-price appreciation and separately recorded
dust/outside-wallet reserves. It is not a portfolio drawdown or market-value cap.
Existing accepted orders still fill and are accounted; incurred quote fees can
push cost above the cap and block subsequent BUYs.

An over-cap re-anchor is deferred under the simulator's atomic replacement
assumption, preserving the old grid and stop. An over-cap new BUY causes
`SIZING_PAUSED`, cancelling BUYs while retaining existing SELL orders and
hard-stop monitoring. No emergency sale or reduced order is invented to fit.
Results report the allocation policy, cap-related pauses/deferrals, and ending
position cost and BUY commitments. Partial-fill/cancel latency is not modeled.

Use `--allocation-policy legacy_cash` to reproduce earlier cash-only sizing;
this switch is independent of `--buy-trend-policy legacy`. Old studies may need
both switches. The currently grandfathered Testnet campaign is not automatically
converted by this research change. Offline simulations still begin with fresh
cash; they are not replays of its carried inventory.

`backtest_grid.py` is standalone research code using `pandas`, `ccxt`, and
`yfinance` for public Yahoo downloads, plus the adjacent pure-math
`grid_geometry.py` module shared with the engine. Offline replay does not import yfinance.
It never imports the application, loads `.env`, accesses the live
ledger, uses API credentials, or submits/cancels exchange orders.

```powershell
python -m pip install "pandas>=2.2,<4" "ccxt>=4.5,<5" "yfinance>=0.2.54,<2"
python backtest_grid.py --months 12 --capital 500 --levels 8
python backtest_grid.py --months 12 --capital 500 --levels 8 --stop-mode fixed --stop-gap-pct 15 --restart-after-stop --cooldown-hours 8
```

The default end is the most recent fully closed execution-candle boundary in UTC. Data is public
Binance **production Spot** history, not periodically reset Testnet history.
Fills use **5-minute candles by default** (`--execution-timeframe 1m` is supported).
ATR uses closed **1-hour candles** (`--indicator-timeframe 4h` is supported).
The deployment trend filter is a separate Binance **daily SMA200**. Select a daily SMA50
explicitly with `--trend-period 50`. ATR OHLCV is aggregated from execution data;
daily trend OHLCV is fetched separately, including 200 preceding complete days.
The default `--buy-trend-policy production` additionally reads **Yahoo BTC-USD**
daily history. BUYs require its last completed daily close to be strictly above
its own SMA200. This ongoing gate applies in all stop modes. A negative signal
cancels BUYs and blocks replacements while preserving SELLs, inventory, and
any enabled price stop. The signals have independent UTC availability timestamps and are
forward-filled to execution opens; a forming candle supplies neither signal.
At intrabar events, the most recent execution-open signal is retained until the
next execution open. Partial macro buckets do not supply signals.

Execution pagination retains 50 ATR-timeframe warm-up bars (50 hours for 1h ATR,
200 hours for 4h) so existing fine-data snapshots remain reproducible. The daily
download separately requires 200 fully closed warm-up days. Missing
execution bars and malformed candles cause an error instead of being invented or skipped.

For reproducibility, specify `--start 2025-10-16T00:00:00Z --end 2026-10-16T00:00:00Z`.
`--end` is exclusive. To replay the saved dataset without network calls:

```powershell
python backtest_grid.py --csv backtest_results/btc_usdt_5m.csv --trend-csv backtest_results/btc_usdt_1d_trend.csv --buy-signal-csv backtest_results/btc_usd_1d_buy_signal.csv --start 2025-10-16T00:00:00Z --end 2026-10-16T00:00:00Z --output replay_results
```

Use the actual start/end in `summary.json` for a downloaded default window.
CSV columns are `timestamp,open,high,low,close,volume`, with ISO UTC timestamps
or Unix milliseconds. Each input must include its full warm-up period above.
Independent daily mode with `--csv` requires `--trend-csv` and never silently
contacts Binance. Missing daily warm-up or a stale daily tail causes an error;
the simulator never forwards an old daily SMA indefinitely through missing data.
Start/end must align to the execution timeframe; `--end` cannot include unfinished
or future execution candles. `--execution-timeframe 1h` exists only for explicit
legacy comparisons, not the default fine-resolution simulation.

Production-policy offline replay also requires `--buy-signal-csv` with 200
preceding Yahoo daily closes and coverage through yesterday for every test day.
It accepts normalized OHLCV or older Yahoo `Open,High,Low,Close` CSVs with a
`timestamp` column; missing volume defaults to zero because SMA uses only closes.
Missing input never causes a silent Yahoo network call. `summary.json` records
the selected policy and all three snapshot hashes. Input snapshots are preserved.

Use **`--buy-trend-policy legacy`** explicitly to reproduce earlier research:
fixed/trailing modes use entry-only Binance gating; no-stop mode cancels BUYs
on intrabar modeled-price crossings of the selected Binance SMA. Legacy mode
does not use Yahoo and needs no BUY-signal CSV. Library `Config` also defaults
to production; `GridBacktest.run()` receives Yahoo candles as `buy_signal_frame`.

In production policy, a macro-off interval invalidates the four-hour breakout
timer. Halted campaigns do not count as actively BUY-paused or accumulate
blocked-entry bars unless research restarts are enabled. `--no-trend-filter`
explicitly disables both gates for an ablation.

## Iteration 2 defaults and research comparisons

The default is **Daily SMA200 + at least 0.8% gross grid spacing + fixed 15%
catastrophic stop**, with no automatic restart after liquidation. These defaults
match the production policy described in [ITERATION2.md](ITERATION2.md).
The script itself never changes the live engine or deploys software.

- **Explicit no-stop research comparison:** `--stop-mode none` disables all price-stop
  liquidation and stop ratcheting. The stop is `None`, including on re-anchor.
  The selected ongoing BUY policy still applies. Retain BTC quantity/cost and
  existing SELL limits; SELL fills continue. Resume BUY lanes only when that
  policy approves, respecting bounds, funding and Post-Only constraints.
  A new daily signal can pause BUYs even without a price change. Threshold crossings
  are processed inside each OHLC path in legacy mode; production policy uses
  completed-day Yahoo signals.
  Opening gaps can fill previously resting BUYs before cancellation takes effect.
  Pausing does not move the grid or guarantee that SELL prices will be reached.
- **Daily SMA200 gate:** initial activation, automatic restart and breakout
  re-anchor require current modeled price **strictly greater than** the mean of
  the last 200 fully closed daily closes. Equality and missing values block
  deployment. The current day first affects the next UTC day's signals.
- **Cooldown in explicit fixed/trailing comparison modes:** every hard stop records its actual modeled crossing time. A restart
  must wait **at least 8 elapsed hours by default AND pass the SMA gate**. Merely
  crossing above the SMA early cannot bypass the cooldown. If the cooldown expires
  below the SMA, the simulator waits in cash/dust until a later eligible execution open.
  Set `--cooldown-hours 4` or `12` to study alternative predeclared policies.
- In fixed/trailing modes, a blocked breakout re-anchor preserves existing orders and risk monitoring;
  only the explicit legacy trailing mode ratchets a floor with price.
  The production BUY gate can cancel BUYs while retaining SELLs and risk
  monitoring. Only explicit legacy fixed/trailing modes retain the earlier
  entry-only SMA policy for reproducibility.
- Restarts still require `--restart-after-stop`. Without it a hard stop remains
  halted, matching the production manual-recovery workflow. Automatic restart
  and its cooldown are research-only; a crash requires human review in production.
- `--no-trend-filter --cooldown-hours 0` is an explicit **ablation** for isolating
  the impact of finer candles from the new entry policy. It is not the default.
  Fee/slippage/capital/filter settings should be identical in paired comparisons.

The Iteration 2 default is **`--stop-mode fixed`**, a
**fixed catastrophic stop 15% below the grid lower bound**:
`floor_to_tick(lower * 0.85)`. It does not ratchet upward with price/high water.
Only a successfully installed automatic re-anchor may update that grid's floor
to `max(previous_stop, floor_to_tick(new_lower * 0.85))`; it never lowers the
existing protection. A rejected proposal cannot alter the current stop. A new
post-liquidation campaign uses its newly calculated lower bound and stop.
The wider stop allows larger individual losses; fewer stop events is not itself
evidence of higher expected return. A negative Yahoo daily signal suspends BUYs;
inventory risk remains until the catastrophic stop or normal grid sales.

To reproduce the former research policy explicitly:

```powershell
python backtest_grid.py --buy-trend-policy legacy --months 12 --restart-after-stop --trend-timeframe 1h --trend-period 50 --stop-mode trailing --stop-gap-pct 2
```

`--stop-mode trailing` retains the previous absolute-distance ratchet and
`max(ATR, lower * stop_gap)` initial buffer. `--stop-gap-pct` defaults to 15;
the fixed mode uses only the selected percentage, not that legacy ATR maximum.
The percentage and cooldown have no effect in `none` mode. No-stop mode retains
open losses in marked equity; removing forced sales does not remove downside
exposure, and a recovery is not guaranteed within or after the test period.

## Minimum gross grid return and safe re-anchoring

The default minimum planned SELL/BUY return per adjacent grid cycle is **0.8%**.
`--min-grid-spacing-pct 1.0` selects 1%; the CLI rejects values below 0.8%.
This applies to exchange-rounded prices, including each side's center-adjacent
cycle. ATR proposes a width; insufficient widths are widened, not accepted.
For center C, B lower levels, S upper levels and minimum fraction r:

```
half_width >= max(2 * ATR, C * (1 - (1 + r)**(-B)), C * ((1 + r)**S - 1))
```

Tick rounding adds a small margin where necessary. The resulting actual levels
are validated again. Eight levels means four on each side and requires roughly
3.24% symmetric half-width at the 0.8% floor. Research no-stop mode has no floor;
explicit fixed mode uses `lower * (1 - stop_gap)`, and trailing mode starts at
`lower - max(ATR, lower * stop_gap)`.

The production engine, frontend and simulator reject new manually specified
grids below the floor. The frontend ATR auto-fill and automatic breakout plans
widen narrow proposals. Existing active orders are not retroactively moved simply
by importing new code; reviewed new grids, resets and START enforce the new policy.
Production hard stops still require explicit manual recovery; automatic post-stop
research campaigns require the backtest flag described above.

When a stop is enabled, automatic re-anchors never lower the protective hard-stop floor. Legacy trailing
mode preserves its distance; fixed mode may raise its floor only on installation.
If a full replacement cannot fit above that floor at the required spacing, the
proposal is **deferred**, retaining existing orders and risk monitoring. There is
no artificial fatal halt and no silent lowering/removal of protection on carried BTC.
The live engine checks again before cancellation and after confirmed cancellation.
If a race makes the plan infeasible after cancellation, the durable reset intent
waits while subsequent polls still evaluate liquidation. It survives a process
restart; it never declares a replacement installed before settings/accounting commit.
Simulator sizing or capital shortages also defer a proposed automatic rebuild
without discarding the current working grid.

The minimum is **gross geometric spacing**, not guaranteed realized net profit.
Seed MARKET slippage, carried inventory's original cost, fees, and hard-stop exits
can still cause losses. More spacing cannot establish an edge by itself.

## Rules mirrored from the application

- Exact total levels: odd totals give the extra lane to BUYs. Eight means four
  lower BUY lanes and four upper SELL lanes, not eight on each side.
- At activation, anchor to the current execution-candle open using ATR available before
  that open. ATR mirrors the application's fresh 15-candle, 14-period
  `pandas_ta` RMA/SMA-seed calculation without TA-Lib. It is **not** an expanding
  or full-history ATR. The live dashboard can use an incomplete candle; this
  simulator deliberately uses only closed macro candles to prevent look-ahead.
  Choosing 4h indicators is a deliberate strategy variant of the live 1h rule.
- Bounds: start with center +/- 2 ATR, then widen for minimum gross grid return.
  Research default hard stop uses 0.85 * lower,
  rounded down to price precision.
  ATR bounds are generated at activation, not recalculated every hour.
- Seed 50% into BTC with a MARKET BUY; reserve the other 50% across the lower
  BUYs. Upper SELLs distribute all tradable seed increments, including the final
  rounding remainder, using the deployed sequential allocation algorithm.
- BUY/SELL prices are geometric. A lower BUY sells one level higher; an upper
  SELL rebuys one level lower. Lower BUYs use fixed quote budgets; upper rebuys
  use the quantity sold. New orders respect the bounds, stop and Post-Only side.
- Default fees are 0.1% per fill on both LIMIT and MARKET orders. By default,
  BUY commissions are deducted in BTC; SELL commissions in USDT. Use
  `--buy-fee-asset quote` for the alternative. In that mode order budgets include
  the additional quote fee to prevent simulated borrowing.
- In explicit stop modes, checks run only with positive tracked BTC; exactly flat runs keep BUYs.
  The optional fixed stop does not trail. Explicit legacy trailing mode uses the
  run's fixed absolute distance and a monotonic floor while inventory is positive.
  BUYs at/below a raised stop are canceled in either mode.
- In explicit stop modes, strict comparison: price **below** the stop triggers. All grid orders
  are canceled; tradable bot BTC is market-sold. Remaining dust and its cost are
  retained, not written off as zero. MARKET fees/slippage are included in P&L.
- Four hours continuously above the ceiling while BTC remains can re-anchor
  the grid using its saved percentage width, carried inventory/cost, and retained
  stop. Insufficient carry size defers the automatic replacement while preserving
  the working grid and risk monitoring. `--breakout-hours 0` disables this model.
- When stops are enabled, a hard stop stays halted by default, matching the real manual START workflow.
  `--restart-after-stop` is a **different operator scenario**: at the first eligible
  execution open after cooldown AND trend approval, acknowledge still-unsellable
  dust, keep its quantity/cost separately, and start a new ATR grid using remaining
  quote. It compounds losses/profits,
  makes no deposits, and does not claim the production bot automatically restarts.

## Execution assumptions and limits

The live engine now uses post-only BUY entries and GTC limit SELL exits. A newly
submitted live SELL can execute at its target or better when the current bid
already crosses that target. The historical simulator below retains its earlier
post-only-style SELL-placement restriction for reproducibility. Its existing
metrics therefore do not validate this revised exit execution policy; update
the replay model and rerun it before making new profitability claims.

Both predetermined routes are run independently by default: open-high-low-close
(`ohlc`) and open-low-high-close (`olhc`). No favorable route is selected after
seeing a candle. Each monotonic segment processes prices in crossing order;
replacement orders cannot fill at extrema that occurred before their placement.
These are sensitivity scenarios, **not guaranteed upper/lower bounds** on results.

Even 1m/5m OHLCV cannot reconstruct ticker polling, exact extrema order, spread,
depth, partial fills, maker queue position, cancellation races or actual manual
interventions. Orders are assumed fully filled on a touch, with instantaneous
replacement when Post-Only permits. That can overstate fills. The four-hour
breakout timer uses interpolated path continuity, which cannot be established
from OHLC candles alone. Each candle is split into three equal-time monotonic
segments (100 seconds for 5m, 20 seconds for 1m); event times are interpolated,
not observed trades. A true production replay requires trades/ticks
and the recorded configuration/decision timeline.

Yahoo daily signals are assumed available at UTC midnight after candle close.
Publication lag, stale caches and provider outages are not reconstructed from
historical OHLC. Live START can initialize and wait for a permitted seed; the
research model waits for both gates before its initial seed deployment.

Opening gaps fill already marketable resting limits first at their limit prices,
then apply the stop at the available open, not the old trigger price. This
allows adverse BUY fills before cancellation rather than magically avoiding them.

`--slippage-bps` defaults to **10 basis points (0.1%)** adverse movement for
MARKET seed/stop fills only. This is a declared assumption, not a measured
slippage estimate. Repeat with 0, 25 and 100 bps. A historical order book is
needed to model crash liquidity. Constant default filters are price tick 0.01,
quantity step 0.00001 and MARKET minimum 5 USDT; grid orders also have the bot's
7 USDT floor. Override the CLI values when appropriate. Historical filter
changes, BNB discounts, commission promotions and fees on fees are not modeled.

All calculations exclude pre-existing wallet BTC, including the separate 1 BTC
Testnet allowance. Small reserved BTC is still marked in total equity even
though subsequent grid stops do not monitor it.

## Output

- `summary.json`: configuration, execution/trend/code/geometry hashes, package versions, assumptions
  and both paths' metrics.
- `trades_<path>.csv`: every simulated execution, fee quote value and realized P&L.
- `equity_<path>.csv`: mark-to-market cash, BTC, realized/unrealized P&L and state
  at modeled intrabar events and extrema.
- `events_<path>.csv`: activation, SMA BUY pause/resume, hard-stop, dust acknowledgment, sizing/rebuild events.
- `btc_usdt_<execution-timeframe>.csv`: exact dataset including warm-up, reusable offline.
  If that file is also the input, the snapshot is written as `data_snapshot.csv`
  so the source data is never overwritten.
- `btc_usdt_<trend-timeframe>_trend.csv`: independent trend dataset including its
  complete SMA warm-up. A conflicting source path uses `trend_snapshot.csv` instead.

**Total Realized P&L** includes parent BUY cost/commission and net SELL proceeds,
including hard-stop losses. **Total Trades** counts all executions (seed BUYs,
LIMIT fills and MARKET stop SELLs), not round trips. **Max Drawdown** measures
total marked equity against its running peak, including open positions and dust,
at modeled intrabar marks. Equity and realized P&L are deliberately reported
separately. No fictitious final market sale is added at the test end.

Reports include pause/resume episodes, canceled BUY count, end-of-bar BUY-pause
counts and the final pause flag. `RUNNING` still means SELLs and polling remain
active during an SMA BUY pause. This is distinct from engine halting.

Reports also include the raw BTC price return over the same dataset (before
fees/slippage), cooldown/trend blocked entry bars and blocked re-anchor episodes.
Installed grids' minimum actual return and execution-bar state counts show whether
the price floor was met and whether a permanent fault ended trading early.
These make market regime and lack of trading visible. A lower drawdown from
staying idle or an early configuration fault is not evidence of improved edge.

Compare both paths, restart policies, fee currencies, slippage and out-of-sample
periods before inferring an edge. Earlier no-stop/SMA-pause results remain available
as explicit comparisons. Revisiting the same year after prior results
still makes this research in-sample. A profitable run alone is not statistical proof.
