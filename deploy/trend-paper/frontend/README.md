# Grid Master dashboard

React, Vite, and Tailwind frontend for the Binance Spot Testnet grid bot. The complete system architecture, trading workflows, risk controls, API surface, and operational limits are documented in `binance-grid-bot-architect/README.md` in the development workspace.

## Local preview

```powershell
npm install
npm run dev
```

Open `http://localhost:5173`. In development, the dashboard calls the FastAPI backend on port 8000 of the same hostname. Run the backend in `--standby` mode on a fresh database for an idle bot awaiting an explicit **Start Grid**, or use `--monitor-only` for a read-only API. Do not start a second trading engine against the same exchange account and state.

The dashboard polls bot status and the authenticated order ledger every 10 seconds, and free Spot USDT about every 20 seconds. Its public BTC/USDT WebSocket ticker and 1-minute candlestick chart use **Spot Testnet** streams; the chart loads its initial candles from the Spot Testnet REST API. The chart's BUY/SELL price lines represent bot-tracked open limits and appear after admin login, when the protected order ledger is available. The status, chart, and market-intelligence cards remain available without login. Wallet balance, order ledger, and execution controls require the admin session.

For a production build, run `npm run build`. The build uses same-origin `/api` requests, so HTTPS hosting must proxy `/api` to the backend's loopback port 8000. Set backend `FRONTEND_URL` to the dashboard's exact origin. The admin session is a `Secure`, `HttpOnly`, `SameSite=Strict` cookie; serve the dashboard over HTTPS outside localhost.

## Checks

```powershell
npm run lint
npm run build
```
# SOL trend paper mode

The default app remains the grid dashboard. Run `npm run dev:trend` to open the
separate SOL paper dashboard at http://localhost:5174 with its API on port 8010.
`npm run build:trend` writes `dist-trend`. The trend backend, private credentials
and isolated ledger are documented in the backend's `TREND_PAPER.md`.

For a hosted same-origin build, override `VITE_TREND_API_BASE` with an empty value;
otherwise `.env.trend` intentionally targets the local development API. The
Git-tagged EC2 release bundles a source snapshot and verified build of this local
frontend commit because this frontend repository has no remote configured.
