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
  recent days, duplicates, nonfinite/nonpositive closes and stale daily tails
  cannot produce an uptrend. Single-ticker MultiIndex output is supported.
- Refresh lazily every five minutes while the engine calls the accessor. Keep
  an existing valid result during a background refresh for at most 15 minutes.
  A refresh failure invalidates it immediately and retries after 30 seconds.
  UTC day rollover invalidates the previous day's signal immediately.
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

When True again, eligible BUY lanes can resume subject to bounds, balance,
Post-Only precision and the retained fixed floor. Manual pauses and reset locks
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
