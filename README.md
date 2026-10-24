# Grid Master — System Architecture & Feature Summary

Grid Master is a human-directed BTC/USDT grid-trading system for **Binance Spot**. A Python engine manages orders and persistent risk state; a FastAPI service exposes telemetry and authenticated controls; a React dashboard presents the account, grid, and market data. Telegram is an outbound alert channel only.

**Selected production baseline:** `sma200-compressed-atr-v1` retains SMA200,
uses a shortest adjacent gap of 0.5× closed 1h ATR with a 0.4% gross floor,
3 BUY / 2 SELL lanes and 50% seed inventory. The strict allocation cap,
one final residual BUY / persistent CAP_EXHAUSTED, fixed 15%-below-lower stop
and manual recovery remain enforced. All RSI entry variants are rejected.
See [the Mainnet checklist and exact configuration layout](MAINNET_DEPLOYMENT.md).
Existing Testnet ledgers retain their saved bounds and tighter stop floors;
historical simulator defaults remain unchanged for reproducibility.

The [runtime Yahoo SMA200 BUY kill switch](MACRO_TREND.md) also suspends and
cancels bot BUY limits when the latest completed `BTC-USD` daily close is not
strictly above SMA200. SELL orders and catastrophic protection remain active.

The selected trading environment controls execution, database state, and browser market feeds. The backend publishes `trading_environment` in every `/api/bot/status` response. Mainnet routing is covered by automated isolation tests; those tests do not place real orders.

## Environment configuration and isolation

Set `TRADING_ENVIRONMENT` explicitly to exactly `TESTNET` or `MAINNET` in the process environment or backend `.env`. Missing, lowercase, unknown, or whitespace-padded values fail startup. There is no default or credential fallback. The choice is cached for the lifetime of the process; changing networks requires restarting it.

| Environment | Required credentials | CCXT sandbox | SQLite filename |
| --- | --- | --- | --- |
| `TESTNET` | `BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET` | `True` | `grid_testnet.sqlite` |
| `MAINNET` | `BINANCE_MAINNET_API_KEY`, `BINANCE_MAINNET_API_SECRET` | `False` | `grid_mainnet.sqlite` |

Both selected credentials must be nonblank. Validation runs before the FastAPI app is created, including direct Uvicorn imports and `--monitor-only`. The JSON `exchange.sandbox` field is ignored for network selection. Spot remains the only supported market type.

`GRID_BOT_DB_DIR` optionally selects the state directory. `GRID_BOT_DB_PATH` may select an absolute file path, but its filename and resolved filename must exactly match the selected environment. Each SQLite file also contains an immutable environment marker checked on every open. Renaming or copying a Testnet database to the Mainnet filename is rejected. Unbound legacy databases are rejected and `grid_bot.sqlite3` is never automatically migrated, renamed, or deleted. Use a fresh environment database; review any legacy positions/state separately before retiring it.

The dashboard waits for a recognized backend environment before opening REST or WebSocket chart feeds. Network changes remount the ticker/chart and clear old market data; unavailable or invalid status closes the feeds. Form drafts are stored under separate environment keys, and legacy drafts are ignored. The one-off `hard_reset.py` utility remains restricted to TESTNET.

Docker stores the selected database under `/data` using `GRID_BOT_DB_DIR`. Its existing preflight still requires an initialized database and persistent config; provision the corresponding environment file before starting Docker.

Run the backend tests with explicit dummy TESTNET settings (never real keys):

```powershell
$env:TRADING_ENVIRONMENT = "TESTNET"
$env:BINANCE_TESTNET_API_KEY = "unit-test-key"
$env:BINANCE_TESTNET_API_SECRET = "unit-test-secret"
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

Frontend checks: `npm run lint`, `npm run build`, and `node --test src/tradingEnvironment.test.js`.

## Architecture at a glance

```mermaid
flowchart LR
    UI[React + Vite dashboard] -->|10 s status and order polling| API[FastAPI on port 8000]
    UI -->|20 s, authenticated| Wallet[Free Spot USDT endpoint]
    UI -->|Admin cookie, explicit actions| API
    Public[Binance public WebSocket] -->|BTC/USDT ticker display| UI
    API --> Engine[GridBot execution loop]
    Engine <--> DB[(SQLite: orders, fills, trades, state)]
    Engine <--> CCXT[CCXT Binance Spot]
    Engine -->|One-way HTTPS alerts| TG[Telegram owner chat]
```

The backend source is in this directory. The frontend is the sibling `web-dashboard/` project in the development workspace. The services still run in one process, with these module boundaries:

| Module | Responsibility |
| --- | --- |
| `main.py` | CLI entry point, service lifecycle, runner heartbeat, API and engine task supervision |
| `api.py` | FastAPI app, authentication, status projections, dashboard controls |
| `engine.py` | Grid execution, risk state machine, order reconciliation, startup recovery |
| `models.py` | Pydantic request schemas, immutable grid config, exceptions, result structures |
| `settings.py` | Validated environment settings, shared constants and state keys |
| `market_data.py` | Hourly strategy ATR and dashboard order-book cache, with injected application state |
| `market_regime.py` | Opt-in hourly range/trend BUY filter; see [experimental policy](MARKET_REGIME.md) |
| `inventory_sizing.py` | Opt-in cost-based BUY taper; see [experimental policy](INVENTORY_SIZING.md) |
| `campaign_risk.py` | Opt-in dollar-risk sizing and campaign exit; see [policy](CAMPAIGN_RISK.md) |
| `auto_atr.py` | Opt-in hourly ATR BUY-net adaptation; see [policy](AUTO_ATR.md) |
| `capital_recycling.py` | Opt-in confirmed-SELL cap release; see [policy](CAPITAL_RECYCLING.md) |
| `fee_validation.py` | Opt-in net-edge and commission validation; see [policy](FEE_VALIDATION.md) |
| `portfolio.py` | Persisted fill snapshots, wallet cost basis, realized/unrealized fee calculations |
| `telemetry.py` | Exception formatting and durable fault journaling |
| `database.py` | Environment-isolated SQLite ledger, transactions, state and transition journal |
| `exchange_handler.py` | CCXT client construction and API circuit breaker |
| `stop_controller.py` | Stop coordination and verified tracked-order cancellation |

CLI flags and dashboard endpoints are unchanged. `uvicorn main:app` remains valid; the app is defined in `api.py`. Programmatic engine/config imports should use `engine.GridBot` and `models.GridConfig`.

SQLite records orders, fills, trades, active settings, trailing-stop state, pause modes, and reset progress. Monetary values remain decimal strings. `GridDatabase.transaction()` groups related local writes and journal entries into a single `BEGIN IMMEDIATE` transaction; nested operations use savepoints, and connections are isolated per thread. Engine activation, preparation, reset/pause controls, confirmed order snapshots/status, liquidation accounting, and terminal liquidation state now commit as coherent units. Exchange requests and notification delivery remain outside these transactions. Order intents commit before HTTP submission so an uncertain response remains recoverable by its exact client order ID.

`tests/fakes.py` supplies shared deterministic exchange fixtures. `tests/test_fault_injections.py` covers ready-mode restart with active orders, cleanup/stop timeouts, no-fill liquidation across restart followed by a severe-slippage fill, failed fill/start/prepare/liquidation commits, and an abruptly terminated subprocess inside a multi-write SQLite transaction. The database tests also verify nested rollback and connection isolation across threads. Run the fault scenarios alone with `.venv/Scripts/python.exe -m unittest tests.test_fault_injections -v` using the dummy TESTNET settings above.

## Core execution engine

### START recovery

If a raised trailing stop overtakes a saved exact BUY level, START remains blocked. The API returns `GRID_STOP_CONFLICT` with the lowest BUY and current stop prices; the dashboard's **Review grid settings** action opens re-anchor controls. Keep all new BUY levels above the retained stop. Saving while IDLE queues settings for review; it does not start trading. The stop cannot be lowered to revive a stale grid.

Small carried BTC can also limit the number of SELL lanes: each exchange-rounded SELL must meet the minimum notional. Explicitly select fewer levels if the retained inventory cannot fund every SELL lane. Increasing quote allocation alone does not enlarge carried BTC. Re-anchors preserve any tighter saved floor; price observations do not ratchet it upward.

### Start and trade lifecycle

1. **Ready service:** `main.py --ready` serves the API and market intelligence with `engine_status=IDLE`, even if a saved grid exists. Before it rests in IDLE, startup verifies Testnet BTC/USDT orders against SQLite and cancels all bot-owned open orders. The cleanup obligation and alarm remain in SQLite until exchange cancellation is verified; START is blocked while cleanup is unresolved. An in-progress hard-stop liquidation remains a separate safety exception. The authenticated top-bar Master Switch is the only normal path from IDLE to RUNNING.
2. **Human approval:** The operator supplies center price, ceiling, hard stop, allocated USDT, and an **exact total** of BUY plus SELL levels. The API validates the live Testnet price, exchange precision, order size, and available capital before accepting the request. In ready mode this saves a pending grid; it places no orders until the operator presses **START**.
3. **Initial inventory:** The configured `initial_inventory_percent` (50% in the current configuration) funds a seed market BUY. Its BTC is divided among upper SELL limits. The remaining quote allocation funds lower BUY limits.
4. **Geometric grid:** The engine calculates exact geometric price levels between the center and each bound. For an odd level count, the BUY side gets one extra level. BUY limits are **Post-Only** (`timeInForce: PO`); SELL exits use **GTC limit orders** at the saved target price. A SELL may execute immediately at its target or better when a rebound has already crossed that price, and can incur taker fees. Both sides retain durable bot-specific client order IDs and exact-ID recovery. A maker-only BUY rejected at the spread is deferred to a later cycle.
5. **Fill rotation:** A filled BUY creates a corresponding SELL sized from its executed BTC minus BTC-denominated commissions, rounded through CCXT's exchange precision, and checked against free bot-owned BTC. If execution details or free BTC have not caught up, placement waits for another cycle. A definitive insufficient-balance SELL rejection also leaves the lane available for retry. A filled SELL records realized trade profit and re-arms a BUY at the corresponding grid lane when conditions allow. The grid rotates through price levels; a completed sell does not permanently reduce its configured lane count.

This is spread-capture grid trading, **not risk-free arbitrage**. Fees, gaps, inventory exposure, and adverse trends can exceed the spread earned by completed cycles. The order ledger and status card count currently active/open orders; that number can temporarily differ from the configured number of price levels during fills, pauses, Post-Only rejections, and reconciliation.

### Master engine switch

The authenticated `POST /api/engine/stop` persists `engine_status=IDLE` before canceling and reconciling **both BUY and SELL bot-owned grid orders**. It leaves unrelated account orders and bot-held BTC alone. The ready-mode loop skips all new grid placement while IDLE; the Testnet dashboard ticker and market-intelligence refresh continue. **IDLE also suspends automated trailing-stop enforcement.** Bot-held BTC remains exposed to price changes.

`POST /api/engine/start` requires saved grid settings and revalidates Testnet open orders, BTC balance, market price, exchange precision, quote balance, and carried BTC. A clean IDLE state queues a carry-aware rebuild. If a saved grid reset is pending, `/api/bot/status` exposes its settings and a token; START requires that exact token after the dashboard shows a review dialog. A changed pending grid invalidates the token. While IDLE, submitting a new re-anchor replaces a pending one only after verifying that no bot orders remain open. Sellable free BTC requires manual clearance, or an explicit `{ "auto_cover_inventory": true }` START request when its bot cost basis is verifiable; in that case upper-bound SELL limits are placed before any new BUY limits. If the order book and SQLite disagree, START keeps the engine IDLE, persists a cleanup alarm, and requires exchange verification before START. A fresh grid needs settings saved first. A liquidation lock, unresolved trading fault, cleanup obligation, or incompatible safety pause blocks START. Both endpoints require the admin cookie and trusted dashboard origin. Manual Safety Pause remains a separate BUY-only risk control.

### ATR-assisted parameters

The backend fetches 15 one-hour Testnet candles and computes 14-period Average True Range (ATR) with `pandas_ta`; it also reports ATR as a percentage of the latest close. The dashboard's **Auto-Fill via ATR** button is an optional, frontend-only proposal based on the operator's entered center `C` and ATR `A`:

| Proposed value | Formula |
| --- | --- |
| Ceiling | `C + max(2A, fee-defended half-width)` |
| Projected lower bound | `2C - ceiling` |
| Initial hard stop | `floor_to_tick(lower * 0.85)` |
| Width per side | `(ceiling - C) / C × 100` |

The stop is fixed **15% below the projected lower bound** for a fresh campaign.
Existing tighter floors remain retained. Fee defense widens narrow ATR proposals
until every rounded adjacent cycle clears 0.8% gross return. Auto-Fill creates a
draft only; review and save it, then press **START**. START also requires price
strictly above the last closed Daily SMA200. ATR is not itself an entry signal.

## Capital and order-risk controls

| Layer | Implemented behavior |
| --- | --- |
| Frontend capital gate | `MIN_NOTIONAL = 7 USDT`; the form shows `allocated capital / total levels` and disables submission when capital is below `levels × 7 × 1.15`. It also warns when allocation exceeds the displayed free USDT balance. |
| Backend order-size check | Before placement, CCXT rounds prices and quantities to exchange precision. The engine checks amount and notional market limits plus its own **7 USDT per grid order** floor. It also checks conservative reverse SELL sizes, since a valid BUY can otherwise produce an undersized SELL. |
| Pre-flight balances | The backend sums exchange-rounded BUY costs (plus the seed BUY when needed) against **free Spot USDT**; it checks bot-available BTC for initial SELL limits. Insufficient capital rejects the start/re-anchor before a partial grid is created. |
| Runtime sizing pause | If a later order becomes undersized, the engine persists `PAUSED_SIZING`, cancels bot BUY limits, leaves SELL limits and risk monitoring in place, and requires a valid re-anchor to resume. |
| Order ownership | New orders use a persisted, unique `gridbot…` client-order namespace. Normal pause, reset, and liquidation cancellation targets matching bot orders and reconciles tracked SQLite rows; unrelated account orders are not intentionally canceled. |
| Uncertain submissions | An ambiguous exchange response is treated as an uncertain order requiring reconciliation, rather than blindly retrying and risking a duplicate. |

The frontend estimate is deliberately coarse. Binance filters, precision rounding, seed allocation, and the size of the reverse order are decided by the backend's more exact checks. A green UI preview is **not** a guarantee that an exchange request will be accepted.

## Fixed protection, pauses, and liquidation

### Fixed catastrophic hard stop

When a new campaign is anchored, its stop is calculated 15% below the lower
bound. Higher market prices update high-water telemetry only and never raise
the floor. A successful re-anchor may raise the fixed floor from its new lower
bound, but never lowers retained protection. Existing tighter legacy floors
remain protected. Liquidation requires manual recovery and START.

**Raise Hard Stop** remains an explicit authenticated tightening override. It
accepts a value above the current floor and below the grid's lower bound, updates
saved risk state, and does not rebuild the grid or enable automatic trailing.
Re-anchors cannot lower the retained floor.

### Safety pause

The operator can enable **Manual Safety Pause**, which cancels **bot-owned BUY limits only** and leaves existing SELL limits active. The mode survives a restart and remains until manually released. If the operator releases a manual pause while price is at or below the lower bound, the bot records `PAUSED_DOWNSIDE`; that mode clears automatically after price rises above the lower bound. Missing BUYs are restored only when each price is safely below the current market and above the hard stop, respecting Post-Only placement.

**Current implementation gap:** Crossing below the lower bound during ordinary active trading prevents *new* BUY placement, but does **not** independently enter `PAUSED_DOWNSIDE` or cancel already-open BUYs. To cancel those BUYs before the hard stop, use Manual Safety Pause. The hard-stop liquidation trigger remains active.

### Hard-stop liquidation

When the bot's polled Testnet ticker falls **strictly below** the dynamic hard stop, it enters a durable `LIQUIDATING` workflow:

1. Cancel and verify all **bot-owned** open BUY and SELL orders for the pair.
2. Reconcile bot-tracked BTC lots against the exchange's freshly fetched **free BTC**. Never submit more than `min(tracked BTC, free BTC)`.
3. Check precision, limit-order lot size, and notional. Submit a GTC limit SELL for tradable bot BTC at 99.95% of the triggered hard-stop price, rounded to exchange price precision. Record unsellable dust separately in the liquidation audit state.
4. Keep the bot locked in `LIQUIDATING` while the limit is open, including after a process restart. Confirm fills with the exchange order ID and quantity before marking the run `LIQUIDATED`. If a partially filled limit is canceled or expires, reconcile the fill and submit a new limit for the sellable remainder. Transient exchange/network errors retry with delay and reconciliation; rate-limit errors use a longer delay. Permanent or unverifiable conditions enter `HALTED` for manual review rather than silently claiming success.

After liquidation, trading remains locked until an authenticated new-grid reset. **The stop is software-polled, not a native exchange stop order:** an offline VM, stale ticker, exchange outage, or fast market gap can delay execution. The limit caps the minimum sale price but may remain unfilled indefinitely if the market stays below it, leaving BTC exposed while the bot is locked in `LIQUIDATING`. Dust may remain in the Spot wallet even when active bot inventory is closed.

## Upward breakout and “Infinity Grid” behavior

When price stays **strictly above the upper bound** across uninterrupted observations for four hours, the bot queues an upward grid shift. The timer is stored in SQLite and reset if price returns inside the bound, the grid changes, a pause begins, or polling has a material gap. Before the shift, it rechecks the breakout and the active trailing floor. It then cancels and reconciles bot orders, carries bot-owned BTC and its cost basis into the new grid, re-centers the bounds around the current Testnet price using the configured width, and places replacement post-only BUYs and GTC limit SELLs. Old lower BUYs are removed as part of that full re-anchor. If price fades during the transition, the engine can withdraw the shift or rebuild the old bounds under its safety checks.

**Important boundary:** This is an automatic *upward re-anchoring* mechanism; the current code does **not** increase `investment_quote` using realized profits. Trade profit is recorded in SQLite, while the grid's USDT allocation remains the configured amount until an operator changes it. “Auto-compounding realized profits” is a proposed feature, not an implemented one.

## Dashboard and operator experience

The responsive React/Tailwind terminal uses three panes: **Execution**, **Engine Telemetry**, and **Market Intelligence**. On smaller screens the panes stack and scroll within the viewport. Skeletons indicate initial loading; failed data requests show unavailable or error states.

| Area | What it shows or does |
| --- | --- |
| Sticky header | Network badge, admin lock state, and authenticated **Wallet: X USDT** badge showing **free Spot USDT**, not total account equity or bot allocation. It refreshes about every 20 seconds. |
| Streaming ticker | A BTC/USDT price from Binance's public WebSocket, with brief green/red tick direction and reconnect behavior. This display is **not** the Testnet execution price. |
| Trading status | Online/standby/paused/liquidated state, pair, count and price range of currently open bot LIMIT orders, high-water mark, and current hard stop. The status endpoint is polled every 10 seconds. |
| Portfolio Exposure | Bot-tracked BTC, average remaining cost **per BTC**, and unrealized PnL using recorded fills and the bot's last observed Testnet price. PnL displays a `+` and green above zero, red below zero, neutral at zero. Manual BTC holdings are excluded from the bot baseline. Missing fills can make these metrics unavailable; unreported fees are excluded. |
| Live Order Ledger | Authenticated table of bot-tracked open/pending limits and the 20 most recent filled orders, with time, side, status, price, and BTC amount. Open data comes from SQLite reconciliation, not a fresh exchange query for every dashboard poll. Older fills may show an approximate order price when an execution snapshot is unavailable. |
| Market Intelligence | 1h ATR and its percentage; top-50 bid/ask BTC depth and their volume ratio, labeled buy pressure, sell pressure, or neutral. These are human review aids, not automatic trading signals. |
| Execution forms | Start/re-anchor absolute-price form, ATR Auto-Fill, capital and exact-level validation, Manual Safety Pause, and Raise Hard Stop. Controls are hidden until an admin session is unlocked. |
| Factory Reset | A custom danger modal with an explicit second confirmation, loading state, and server error displayed in place. The dashboard refreshes after a successful reset. |

### Authentication and API surface

FastAPI serves a public, read-only `/api/bot/status`. Wallet balance, order ledger, and control endpoints require a single-admin JWT in a `Secure`, `HttpOnly`, `SameSite=Strict` cookie. The session lasts 15 minutes; the open dashboard attempts a silent refresh every 10 minutes. Writes check the request's exact `FRONTEND_URL` origin. CORS permits that configured origin rather than a wildcard. Secrets stay in `.env` and are not returned to browser JavaScript.

| Endpoint | Purpose | Admin session |
| --- | --- | --- |
| `GET /api/bot/status` | Grid, protection, portfolio, ATR, and depth snapshot | No |
| `GET /api/orders/live` | Open bot limits and recent fills | Yes |
| `GET /api/wallet-balance` | Free Spot USDT | Yes |
| `POST /api/auth/login`, `/refresh`, `/logout` | Session lifecycle | Login uses password; refresh uses cookie |
| `GET /api/auth/me` | Check current admin session | Yes |
| `POST /api/bot/pause` | Manual Safety Pause | Yes |
| `POST /api/bot/stop-loss` | Raise trailing hard stop | Yes |
| `POST /api/bot/grid/recenter` | Explicit new grid or queued re-anchor | Yes |
| `POST /api/admin/factory-reset` | Erase bot accounting after safety checks | Yes |

## State reset and recovery

**Manual re-anchor** preserves tracked BTC and cost basis, validates the proposed stop against the active trailing floor, checks future order sizes and balances, then persists a reset request. It reconciles/cancels old bot orders before placing a replacement grid. The API returns a queued response; the UI reflects live settings after the engine finishes. Existing BTC is carried rather than automatically sold at a loss.

**Dashboard Factory Reset** deletes bot orders, trades, and inventory accounting only after verifying that no BTC/USDT orders remain open on the exchange, no tracked orders remain active, and no bot-owned BTC remains. It halts the existing run; it does not create a new grid. If exposure is present or cannot be verified, it rejects the wipe.

`hard_reset.py` is a separate, interactive maintenance utility for the **Testnet** host. It requires the service to be stopped and typed confirmation, cancels **all** BTC/USDT Testnet orders (including manual orders), verifies exposure, then deletes the SQLite database. Its `--keep-btc-manual` option explicitly moves a reconciled remainder outside the new bot baseline. This utility has a wider cancellation scope than routine bot operations.

## Running and verification

1. Create a Python 3.12 virtual environment in the backend directory and install `requirements.txt`. Configure `.env` from `.env.example` with **Spot Testnet** API keys, Telegram token/chat ID, `BOT_ADMIN_PASSWORD`, and a random `BOT_JWT_SECRET` of at least 32 characters. Keep `.env` and all SQLite state files out of Git.
2. Use `python main.py --check` for a non-trading grid preview or `python main.py --ready` for the dashboard-controlled service. `--standby` uses the ready lifecycle when it finds a saved run. The legacy `--execute` mode performs full exchange reconciliation before trading and refuses a saved IDLE or cleanup-required state. `--monitor-only` serves a read-only API without the engine. Avoid running a second trading process against the same account and database.
3. In the sibling frontend directory run `npm install` and `npm run dev`; open `http://localhost:5173`. Development API requests go to port 8000 on the same hostname. A production build uses same-origin `/api` and needs an HTTPS reverse proxy. The backend API binds to loopback by default; Docker Compose maps port 8000 to loopback on its host.
4. Verify the backend with `python -m unittest discover -s tests -v`. Verify the frontend with `npm run lint` and `npm run build`.

The existing EC2 setup can run under `systemd`; see `deploy/systemd/`. An optional container path is described in [DOCKER.md](DOCKER.md). Keep exactly one trading runner active. SQLite and `.env` are operational state, not disposable build artifacts.

### Diagnose a hard-stop event

Run the read-only `diagnose_stop_event.py` on the VM with journal access. Times must include `Z` or a UTC offset. It combines the bot's logged trigger with all public Binance **Spot Testnet** aggregate trades in a short window, paginating beyond the API's 1,000-trade response limit:

```bash
sudo .venv/bin/python diagnose_stop_event.py \
  --start 2026-09-30T07:19:00Z --end 2026-09-30T07:21:00Z \
  --floor 81948.20 --testnet-trades \
  --csv diagnostics/2026-09-30-hard-stop.csv --summary-only
```

Omit `--summary-only` to print every row in timestamp order. The bot did **not** historically log every ticker poll; SQLite's `last_market_price` is overwritten. Public aggregate trades establish traded-price movements but are not a replay of the bot's own ticker responses. The dashboard header now uses a Binance Spot **Testnet** WebSocket, but its current price cannot reconstruct a past wick or prove which ticker value the bot received at that moment.

## Current limits and operational risks

- Mainnet routing is explicit and requires the locked profile, `--ready`, a reviewed grid and authenticated START. [The deployment checklist](MAINNET_DEPLOYMENT.md) records the required prospective Testnet and live host checks; retrospective results alone do not establish Mainnet readiness.
- The Testnet WebSocket price in the header and the backend's polled Testnet ticker can differ briefly because they are separate feeds. The backend does not receive every tick, so stop and breakout decisions can lag fast markets.
- Post-only BUY limits may be rejected at the spread; their lane can remain temporarily empty until a later safe cycle. SELL exits keep a minimum price but permit immediate execution and taker fees; partial remainders remain working until reconciled. Exchange precision and balances can change between validation and submission.
- The order-book ratio is a top-50 snapshot, susceptible to rapidly changing or canceled displayed liquidity. It is not a predictor of future fills.
- Portfolio PnL is an estimate from bot-recorded lots and the last observed price. It is not exchange-wide equity, a tax ledger, or a guarantee of realized profit.
- Outbound Telegram alerts improve visibility but do not control the bot or replace process and exchange monitoring. The system has no automatic realized-profit compounding.

### Reviewed Testnet maintenance handover

`main.py --ready --resume-reviewed` is an explicit Testnet-only maintenance
mode for a saved RUNNING campaign. It retains working order IDs and parent lots
after startup reconciliation instead of performing the usual ready-mode order
cleanup. It rejects stopped/halted grids, pending resets, cleanup/fault/risk
states and unexplained free wallet BTC. It retains the runner fence and the
normal dashboard controls. Deployment must prevent two active owners and allow
the previous lease to expire; it must never edit or bypass the lease. Use the
ordinary `--ready` service for subsequent starts. No Mainnet or post-liquidation
automatic resume is enabled.
