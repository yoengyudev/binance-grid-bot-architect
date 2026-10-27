# SOL trend-following paper version

This is an isolated first implementation of the frozen SOL EMA strategy. It runs
with **100 simulated USDT** and public mainnet SOL/USDT prices. It has **no private
exchange client, API keys, live-order transport or live-trading switch**. The
existing grid service, configuration and databases remain separate.

## Run locally

From `binance-grid-bot-architect`, using the existing virtual environment:

```powershell
.\.venv\Scripts\python.exe trend_main.py --init-config
.\.venv\Scripts\python.exe trend_main.py --check-config
.\.venv\Scripts\python.exe trend_main.py
```

`--init-config` creates `trend-paper.env` once, with random private login
credentials. It refuses to overwrite an existing file. Read
`TREND_ADMIN_PASSWORD` in that local ignored file to log in. The grid `.env` is
never loaded. Do not put secrets in the dashboard environment.

From `web-dashboard` in another terminal:

```powershell
npm run dev:trend
```

Open **http://localhost:5174**. The paper API listens on loopback port **8010**.
The first startup downloads the historical indicator seed in bounded pages;
the dashboard displays progress. Once data is ready, choose **Start paper**.
Starting enables future entries; it does not immediately buy or replay old
signals. **Pause entries** preserves risk monitoring and strategy exits for any
open position. **Close paper position** simulates a market exit and pauses entries.

The new UI uses the existing visual infrastructure, with position information,
four-hour candles, EMA lines, entry/stop levels and recent simulated fills. The
desktop dashboard fits the viewport; long fill history may scroll inside its
own sidebar. A narrow mobile viewport uses normal vertical scrolling.

## Frozen strategy

- SOL/USDT Spot, long only, one position, no leverage/shorts/averaging/grid.
- UTC 4h EMA20 fresh bullish crossover over EMA50, with signal close above EMA200.
- Each EMA is seeded by its first N completed-bar SMA. ATR14 uses Wilder smoothing.
- Signals are eligible **one hour after the four-hour bar closes**.
- Initial stop = actual modeled entry price - 3× signal ATR14, rounded down by tick.
- A delayed completed-bar update can only raise the stop, using post-entry highs
  available through that signal close. Future/pre-entry highs cannot move it.
- Delayed bearish EMA state or observed stop breach exits. No fixed profit target.
- Planned stop cash loss <=1% of current marked paper equity after modeled costs,
  SELL rounding and carried residual basis. Gaps can exceed the planned loss.
- New purchase cost plus carried basis <=80% of paper equity and available cash.
- BUY commission in SOL, SELL commission in USDT, 0.1% per side. Each market
  execution additionally assumes 0.1% adverse price movement. Current public
  bid/ask spread is also borne. These are modeled costs, not queried account fees.
- Below-step residual quantity and proportional purchase cost remain in equity.
  BUYs add inventory; only SELLs realize P&L. No automatic deposits or annual reset.

The paper risk budget applies to this isolated bot equity, not the unrelated
wallet balance of the existing grid. Default starting capital is 100 USDT.
Changing capital/cost/risk settings for an existing ledger is rejected. Use a new
explicitly isolated ledger for a different trial; the service never silently
clears or credits an existing account.

## Paper execution and operational limits

The signal/wallet core exactly reproduces the frozen historical study under its
replay assumptions. Prospective execution is different: public book quotes arrive
approximately every five seconds, BUYs use observed asks, SELLs use observed bids,
and execution is simulated without queue, depth or partial fills. Quotes are
observed REST responses, not exchange executions. Brief between-poll crossings
may be missed. There is no exchange-hosted stop or downtime price guarantee.

A scheduled entry must be observed within **90 seconds** of its eligibility time
and after this trial's START. A missed entry window is consumed and logged as a
skip rather than buying retroactively. Bearish exits/trailing updates can catch
up using the first fresh quote; historical profitable fills are never invented.

Closed hourly history begins 1 September 2021 to preserve the research seed.
Only the three independently documented 2021/2023 exchange-closure hours are
allowed to be absent. Other interior gaps block signals. Quotes can still drive
an existing stop while history is unavailable. Stale quotes block simulated fills
and manual closes. The UI retains the last snapshot and marks it unavailable.

SQLite transactions atomically save account, signal cursor and events. A durable
runner lease prevents two writers. Restart recovers the position, basis, stop and
processed signals without a duplicate entry. Graceful shutdown pauses entries;
after restarting, review and START again. After a crash, another process may need
to wait up to 90 seconds for the lease to expire. Position protection is active
only while the service receives fresh quotes; it resumes when the runner returns.

Paths (relative to the private configuration):

- `state/sol-trend-paper.sqlite` — new paper account, closed candles and events.
- `logs/sol-trend-paper.log` — separate rotating cycle-error log.
- `trend-paper.env` — ignored local credentials and isolated paths.

The status API reports only this paper account. No grid P&L is carried across.
No Telegram messages are sent by this version; paper fills and decisions are
recorded in its ledger and dashboard. Existing grid notifications are untouched.

## Validation and builds

```powershell
# From backend directory:
.\.venv\Scripts\python.exe -m unittest tests.test_trend_following -v

# From workspace root; requires retained frozen research artifacts:
binance-grid-bot-architect/.venv/Scripts/python.exe artifacts/trend-paper-migration-20261027/verify_parity.py

# From web-dashboard:
node --test src/*.test.js
npm run lint
npm run build:trend
npm run build
```

`build:trend` writes `dist-trend`; the default build remains the grid dashboard.
The checked-in trend environment targets local port 8010. A later hosted release
must override `VITE_TREND_API_BASE` to its HTTPS paper API origin (or an empty
value for same-origin routing), configure the matching backend HTTPS origin, and
use a separate service and ledger. **No EC2 deployment is part of this change.**

Evidence and screenshots are in `artifacts/trend-paper-migration-20261027`.
Historical parity validates the frozen strategy/account model, not future
profitability, private exchange order handling, or mainnet deployment readiness.

Public-data transport follows Binance's documented [market-data-only
endpoints](https://developers.binance.com/en/docs/products/spot/faqs/market_data_only).
All outbound exchange requests in this service are allowlisted unsigned GETs to
`data-api.binance.vision`.
