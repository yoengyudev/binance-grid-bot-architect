# October 20 execution safety repair

This repair changes execution protection, commission provenance and frontend
freshness checks. It does not change the deployed strategy's SMA, ATR, grid
spacing, capital allocation, fixed stop floor, or manual post-stop START policy.
Ordinary maker BUY and paired GTC SELL execution remains intentional.

## Between-poll stop crossings

`stop_history.py` follows Binance Spot's public aggregate trade IDs using the
same exchange client/environment, exchange lock, circuit breaker and bounded
read retries as the engine. It catches a sub-floor print even when the next
sampled ticker has already recovered.

- First installation records the current trade ID. It does not replay old
  pre-installation crashes or liquidate a position because of yesterday's wick.
- SQLite retains the cursor across process restarts. Every ID through a fixed
  tail must be accounted for; pagination cannot silently skip to recent trades.
- Four pages of up to 1,000 trades are processed per check. Catch-up retains
  progress and defers ordinary order placement until coverage is verified.
- Gaps, regressing IDs (including exchange resets), and unavailable history
  cause retry/backoff rather than treating missing coverage as safe. The
  diagnostic state key is `stop_trade_history_alarm`; warnings also reach the
  engine logs. Existing resting orders remain on the exchange during retries.
- A witnessed crossing is durably latched before inventory is refreshed again.
  The existing liquidation state machine cancels tracked orders, verifies final
  fills, releases locked bot BTC, and market-sells only the tracked remainder.
  Reserved wallet BTC is excluded. Reconciliation handles partial fills and
  ambiguous market-order responses before any second sale.
- A verified flat position clears the latch and skips new entries for that
  cycle. Later history is checked before a subsequent entry. Human START begins
  a newly reviewed interval; restarting a RUNNING process retains its interval.
- Raising a floor does not apply the higher floor retrospectively to prices
  observed before that change. Trailing remains disabled.

This is **retrospective, bot-managed protection**, not an exchange-hosted stop.
Detection and liquidation still require working exchange APIs and take time.
The execution price can differ from the floor, especially after a gap or rebound.
It does not guarantee a maximum loss or continuous protection during an outage.
An exchange-native OCO design needs separate order-list/reservation accounting;
placing an additional full-position SELL would conflict with locked SELL BTC.

Binance reference: [aggregate trade ID pagination](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints).

## Commission accounting

Verified fill snapshots now preserve the complete commission asset/cost list,
including explicit zero commissions. Realized SELL accounting uses that list
instead of the raw BUY order's often-empty fee fields. Legacy snapshots missing
the list are upgraded through private per-order trade history before accounting.
Fees in a third asset retain the existing estimated-fee classification rather
than being silently treated as free.

The unique SELL-order accounting constraint is retained. This change does not
rewrite already recorded historical realized P&L. Any historic correction needs
an independent, backed-up reconciliation of the old records.

## Liquidation accounting and visibility (1.0.6)

MARKET liquidation confirmation now verifies private per-order executions before
recording proceeds and commission. Explicit zero fees remain zero; incomplete
history retries the confirming phase without submitting another sale. The saved
fill snapshot and realized trade commit atomically. Recent Fills includes these
consolidated exits even though they are not ordinary grid-lane orders, excluding
duplicates already present in current or archived grid orders.

`repair_liquidation_fees.py --order-id CLIENT_ID` previews a historical correction;
`--apply` requires an attended, completed HALTED Testnet liquidation and ownership
of the runner lease. Back up the ledger first, stop the service, and wait for its
lease to expire. The tool allows only Testnet GETs, verifies matching MARKET SELL
quantity/proceeds and explicit pair-asset fees, and excludes ordinary grid trades.
It preserves purchase basis, execution prices and original timestamps. Each fee
correction and snapshot has an atomic audit record; repeat application is a no-op.
It does not change the actual exchange loss or execute any order.

Runner lease contention/loss exits with temporary-failure status 75. The systemd
unit's existing Restart=on-failure and 60-second RestartSec can retry after an
expired lease, while configuration/safety-review errors remain status 2 and do
not loop. A competing process cannot clear, steal or bypass a still-valid lease.
The canonical --ready service still requires manual post-stop grid review/START.

## Frontend freshness

Ticker and kline callbacks reject old/missing exchange event timestamps and
excessively future-dated events. Klines must also belong to the current interval
(with a short boundary grace period). Ignored older candles cannot refresh the
LIVE watchdog. Fresh hourly candle updates remain valid throughout the hour;
the event itself must be recent. Capturing a grid center checks event age again
at click time, so recent message receipt cannot extend an old quote's lifetime.

## Deployment status

Release identity and per-deployment verification belong in the attended Testnet
evidence directory. Validate startup, synthetic crossing/rebound and recovery
before considering Mainnet. Begin a new observation baseline after protection
changes; earlier Testnet results relied on different stop-detection behavior.
The 1.0.6 maintenance deployment preserves the completed HALTED campaign and
changes only explicitly audited historical liquidation fee estimates.
