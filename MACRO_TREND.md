# Runtime SMA200 BUY kill switch

Install the declared dependencies with `python -m pip install -r requirements.txt`.
`macro_trend.py` provides the requested `get_macro_trend()` function. It is a
non-blocking accessor: the first call starts one background download and returns
False until usable data is available. Calls thereafter use the cached result.
`fetch_macro_trend()` performs the synchronous Yahoo request inside that worker.

## Data and timing

- Source: `yfinance.download('BTC-USD', interval='1d')`, explicitly requesting
  the last 250 completed UTC days with start/end dates, a 10-second response
  timeout, no mass-download threads, and unadjusted closes.
- Compare the latest **completed daily close** to the mean of the latest 200
  completed daily closes. Return True only for `close > sma200`; equality is False.
  A forming daily candle is excluded. This is a daily regime filter, not a
  comparison of the live Binance ticker with yesterday's SMA.
- Require 200 consecutive valid days through yesterday. Empty data, missing
  unrecoverable days, duplicates, nonfinite/nonpositive closes and stale daily
  tails cannot produce an uptrend. Single-ticker MultiIndex output is supported.
- If a daily close is absent or blank within the last seven completed UTC days,
  make one additional bounded Yahoo `BTC-USD` hourly request in the background.
  Recover that day's close only when all 24 distinct UTC hourly bars are present
  and have finite, positive closes. Use the 23:00 bar's completed close, never
  the hourly mean, today's forming candle, interpolation, or a previous close.
  If the hourly response fails or a missing day is incomplete, try one bounded
  same-feed 15-minute request. Require all 96 distinct UTC quarter-hour bars
  with positive, finite closes, then use the completed 23:45 close. Never mix
  partial resolutions to manufacture a complete day. Preserve days already
  verified from hourly bars, and publish no repairs unless every missing day
  is verified. Older gaps or days incomplete in both resolutions keep BUYs
  paused. No Binance prices are substituted into the Yahoo series.
  A complete daily response makes no hourly request and behaves as before.
- Status includes `reconstructed_days` and a `recovery_intervals` mapping so operators can identify recovered
  candles. A successful recovery still requires `close > sma200`; it does not
  force the regime filter to approve BUYs. No new dependency is needed.
- Refresh lazily every five minutes while the engine calls the accessor. Keep
  an existing valid result during a background refresh for at most 15 minutes.
  A refresh failure invalidates it immediately and retries after 30 seconds.
  UTC day rollover invalidates the previous day's signal immediately.
  A worker completing after midnight must match the new required signal day;
  stale/future/nonfinite snapshots are rejected before publication. Failure to
  launch a worker also observes the 30-second retry interval.
- At most one download runs at a time. A slow response cannot accumulate jobs
  or hold the cycle lock, exchange lock, API event loop, or SQLite transaction.
  The accessor never waits for HTTP; it only takes a short snapshot lock and
  starts a daemon worker when due. Yahoo requests are independent of Binance I/O.

## Execution behavior

The engine reconciles fills and evaluates its hard stop first. It then checks
the cached Yahoo trend before normal grid maintenance. When False:

1. Persist `macro_buy_pause=1` and a transition-journal event, independently of
   manual/reset/sizing safety states.
2. Mark and cancel **bot-owned BUY LIMITs only**, reconciling fills/cancellation.
   Manual wallet orders are left alone. Cancellation timeouts are journaled and
   retried on later polls; they do not authorize a new BUY.
3. Leave existing SELL LIMITs active. Filled/partially filled BUYs can still
   receive protective SELL replacements, and SELL fills still reach accounting.
4. Block lower-grid BUYs, upper SELL-to-BUY replacements, and the seed MARKET
   BUY. A second guard immediately before order-intent creation catches a trend
   change between cycle planning and submission.
5. Defer automatic/manual grid rebuild execution without canceling SELLs for
   that rebuild. Retain the durable pending reset and continue normal risk checks.
   The reviewed same-grid `engine_resume` recovery may restore SELLs for already
   owned bot inventory while BUYs remain blocked.

When True again, eligible BUY lanes can resume subject to bounds, balance,
BUY post-only constraints, exchange precision and the retained fixed floor. SELL exits use GTC limits and can execute immediately even while macro BUYs are paused. Manual pauses and reset locks
remain independent; macro recovery cannot release them. A process restart starts
with an unknown/False cache, while saved cancellation markers prevent expected
canceled BUYs from being mistaken for unexpected order failures.

The console warns on entry into the pause:

```text
Price below/equal SMA 200 or macro data unavailable. Buy orders suspended to protect capital; SELLs and hard stop remain active.
```

Macro pause does not liquidate BTC. The fixed 15% catastrophic stop and manual
panic liquidation still cancel SELLs and exit inventory when their own protocols
require it. Completed liquidation still requires human review and manual START.

Macro BUY-state changes queue a best-effort Telegram notification through the
existing background notifier. The pending event survives a process restart;
unchanged polls do not queue duplicate notices. Reasons distinguish unavailable
or stale data from a valid bearish SMA200 signal. Data becoming valid but still
bearish produces a reason-change notice and leaves BUYs paused. A positive
signal means BUY permission only: it never clears a manual pause, reset lock or
completed hard-stop halt. Delivery uses the existing best-effort queue; it is
not a guaranteed-delivery outbox.

The earlier Binance production Spot SMA200 gate for START/re-anchors is retained.
Yahoo `BTC-USD` and Binance `BTC/USDT` are separate sources/instruments and can
disagree; both applicable gates must permit new deployment/buying. Testnet
execution remains on Testnet. This changes the strategy; previous Binance-only
backtest metrics do not validate the combined Yahoo runtime rule.

This change does not deploy, start, stop or otherwise alter the EC2 instance.
Tests use deterministic history and injected trend providers, never Yahoo or
Binance credentials. `tests/test_macro_trend.py` covers data validation, cache
expiry/retries, slow downloads, order ownership, cancellation races, partial
fills, SELL accounting, restart recovery and hard-stop priority.

The missing-daily-close recovery was also checked read-only on EC2 on
2026-10-19. Yahoo's daily endpoint returned a null October 18 close; all 24
hourly closes were available. Recovery produced a completed close of
82,006.8515625 USD and SMA200 of 72,495.332265625 USD. This check ran the candidate
fetcher separately and did not replace the running release, place orders, or
restart the service. Any deployment requires a new release identity and a
recorded restart of prospective observation.

On 2026-10-21 the incident logs showed a missing daily close and rejected hourly
recovery through 08:15 Cambodia. Original provider responses from that interval
were not retained, so publication lag versus a transient feed gap cannot be
distinguished retrospectively. By 23:01 Cambodia the provider returned complete
daily/hourly data again. The new candidate was subsequently checked in a separate
EC2 process with the missing daily row and final hourly row deliberately removed.
It recovered from 96 valid quarter-hour bars, without reading trading credentials
or importing the engine. The recovered close was 82,938.2578125 USD versus the
then-published daily close of 82,941 USD; intraday reconstruction can differ
slightly from the canonical daily series. Published valid daily rows remain
authoritative on the next refresh. This probe exercised fallback behavior; it
did not reproduce or prove the precise historical provider failure and did not
deploy the candidate or restart/resume trading.
