# Grid strategy backtest

`backtest_grid.py` is standalone research code requiring only `pandas` and
`ccxt` and the adjacent pure-math `grid_geometry.py` module shared with the engine.
It never imports the application, loads `.env`, accesses the live
ledger, uses API credentials, or submits/cancels exchange orders.

```powershell
python -m pip install "pandas>=2.2,<4" "ccxt>=4.5,<5"
python backtest_grid.py --months 12 --capital 500 --levels 8
python backtest_grid.py --months 12 --capital 500 --levels 8 --stop-mode fixed --stop-gap-pct 15 --restart-after-stop --cooldown-hours 8
```

The default end is the most recent fully closed execution-candle boundary in UTC. Data is public
Binance **production Spot** history, not periodically reset Testnet history.
Fills use **5-minute candles by default** (`--execution-timeframe 1m` is supported).
ATR uses closed **1-hour candles** (`--indicator-timeframe 4h` is supported).
The default trend filter is a separate **daily SMA200**. Select a daily SMA50
explicitly with `--trend-period 50`. ATR OHLCV is aggregated from execution data;
daily trend OHLCV is fetched separately, including 200 preceding complete days.
The two signals have independent UTC close/availability timestamps and are
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
python backtest_grid.py --csv backtest_results/btc_usdt_5m.csv --trend-csv backtest_results/btc_usdt_1d_trend.csv --start 2025-10-16T00:00:00Z --end 2026-10-16T00:00:00Z --stop-mode none --output replay_results
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

## Iteration 2 defaults and research comparisons

The default is **Daily SMA200 + at least 0.8% gross grid spacing + fixed 15%
catastrophic stop**, with no automatic restart after liquidation. These defaults
match the production policy described in [ITERATION2.md](ITERATION2.md).
The script itself never changes the live engine or deploys software.

- **Explicit no-stop research comparison:** `--stop-mode none` disables all price-stop
  liquidation and stop ratcheting. The stop is `None`, including on re-anchor.
  At or below the last closed Daily SMA200, cancel resting BUY limits and block
  new BUY placements. Retain BTC quantity/cost and existing SELL limits; SELL
  fills continue. Resume eligible BUY lanes only when modeled price is strictly
  above the SMA, still respecting grid bounds, funding and Post-Only constraints.
  A new daily signal can pause BUYs even without a price change. Threshold crossings
  are processed inside each declared OHLC path, not just at execution-bar closes.
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
  The trend filter does not liquidate an existing run just because price dips
  below the SMA, nor does it gate each normal replacement fill. These comparison
  modes retain the earlier entry-only SMA policy for reproducibility.
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
evidence of higher expected return. Entry filtering does not liquidate existing
positions or suppress their normal replacements when price later crosses below
the SMA. Risk remains until the catastrophic stop or normal grid sales.

To reproduce the former research policy explicitly:

```powershell
python backtest_grid.py --months 12 --restart-after-stop --trend-timeframe 1h --trend-period 50 --stop-mode trailing --stop-gap-pct 2
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
