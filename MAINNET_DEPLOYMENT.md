# Mainnet deployment checklist — sma200-compressed-atr-v1

This is the selected production policy and deployment procedure, **not a record
of a live deployment**. No Mainnet credentials have been installed and no live
orders have been submitted by this change. RSI<45/<50, both AND and replacement,
are rejected for production; their retrospective evidence remains archived.

The policy improves the inspected compression experiments. Those results do not
prove durable Alpha, institutional certification, or survival through every
market or infrastructure failure. Mainnet release remains blocked until the
prospective Testnet and host checks below have been completed.

## Locked policy

| Setting | Required behavior |
|---|---|
| Policy ID | `sma200-compressed-atr-v1` |
| Entry/reanchor macro gate | Price strictly above completed Binance daily SMA200 |
| Ongoing BUY gate, including seed | Completed Yahoo BTC-USD daily close strictly above its SMA200; missing/stale data blocks BUYs |
| RSI | Absent; no RSI configuration is accepted |
| Initial geometry | Minimum adjacent return `max(0.5 * closed_1h_ATR / center, 0.004)`; exchange-rounded symmetric bounds |
| ATR | Research-equivalent fresh 15 closed hourly candles; 14-period TR/RMA calculation; forming hour excluded |
| Lanes / initial inventory | Exactly 3 BUY + 2 SELL; 50% quote allocation seeds BTC |
| Reanchor | Existing 4-hour breakout rule and retained fractional width; no hourly automatic ATR reset |
| Fees | Model ceiling 0.1% per side; 0.4% gross floor leaves 0.1993004% after the modeled base/quote commissions |
| Allocation | BTC purchase cost + outstanding BUY notional cannot exceed the explicit allocation, with existing numeric tolerance |
| Residual fill | At most one final BUY rounded **down** to remaining budget, valid under live filters and the bot's 7-USDT minimum; then persistent `CAP_EXHAUSTED` |
| Cap recovery | SELL proceeds, RSI/macro recovery, restarts and STOP/START cannot release the latch; reviewed manual new grid required |
| Stop | Fixed stop 15% below lower bound; retain any tighter existing floor; no price trailing or automatic post-stop restart |

The 0.5 multiplier targets the **shortest adjacent percentage gap**, not the
overall half-width. Three BUY and two SELL lanes have different adjacent ratios.
The fee floor can dominate the ATR target. Changing bounds changes the numerical
initial stop; its formula and retained-floor protection stay fixed.

`production_baseline.py` contains the authoritative constants and bounds helper;
they are not environment knobs. The profile marker is required by the Mainnet
config loader, forbids extra strategy fields, and fixes BTC/USDT, five lanes and
50% inventory. The server checks reviewed initial/manual proposals against the
current closed ATR. A stale or differently sized draft is rejected rather than
silently substituted. The browser computes the same policy. Existing Testnet
legacy state is preserved; `strategy_baseline_locked=false` identifies it.

Historical simulator defaults are intentionally unchanged to preserve saved
experiments. Use the archived compressed/RSI experiment runners for those exact
comparisons, not the legacy default backtest CLI.

## Exact file layout (Linux/systemd deployment)

```text
/opt/grid-bot/
  releases/<reviewed-release>/
    binance-grid-bot-architect/
      .venv/                         # Python 3.12+ Linux environment
      main.py, engine.py, api.py, models.py, ...
      production_baseline.py         # immutable policy, reviewed source
      allocation.py, budget_policy.py
      dashboard_reads.py, dashboard_snapshot.py
      mainnet_preflight.py
      requirements.txt
      tests/
      deploy/mainnet/
    web-dashboard/dist/             # matching production frontend build
  current -> releases/<reviewed-release>
/etc/grid-bot/mainnet.env             # selected credentials and process settings
/etc/systemd/system/grid-bot-mainnet.service
/var/lib/grid-bot/mainnet/
  config.json                        # copied from config.mainnet.example.json
  grid_mainnet.sqlite                # fresh, explicitly MAINNET-bound ledger
  grid_mainnet.sqlite-wal             # SQLite-managed, when present
  grid_mainnet.sqlite-shm             # SQLite-managed, when present
/var/cache/grid-bot-mainnet/          # writable Yahoo/cache directory
/var/backups/grid-bot/mainnet/        # private consistent backups, separate from releases
```

Keep Testnet in a separate config/state directory with its own credentials and
`grid_testnet.sqlite`. Never copy, rename or edit a Testnet ledger to make it a
Mainnet ledger. Environment selection comes exclusively from the exact value
`TRADING_ENVIRONMENT=MAINNET`; JSON `exchange.sandbox` cannot override it.
Stop the old Testnet runner during cutover, or isolate its host/API/process
completely. Never run two trading processes against one live account/ledger.

Copy these templates; replace credentials and domain **on the host**, without
putting secrets into Git, deployment archives, shell history, screenshots or logs:

- `deploy/mainnet/env.mainnet.example` → `/etc/grid-bot/mainnet.env`
- `deploy/mainnet/config.mainnet.example.json` → `/var/lib/grid-bot/mainnet/config.json`
- `deploy/mainnet/grid-bot-mainnet.service` → `/etc/systemd/system/grid-bot-mainnet.service`

The config's 1/3-USDT bounds and 0.85 stop are **bootstrap sentinels, never a
tradable BTC grid**. They allow a fresh service to start IDLE. Mainnet direct
`--execute`, `--standby` and `--check` paths are disabled; `--ready` cannot START
without a separately saved grid. Calculate and review live bounds in the
dashboard, then Save and START. Do not replace sentinels with guessed prices.
The approved active bounds, counts and retained stop subsequently live in SQLite
and override the bootstrap template on restart. `spacing_percent` is a required
legacy field; with exact lane counts it is **not** an ATR half-width setting.

## 1. Release and Testnet acceptance — mandatory before Mainnet

- [ ] Review the source manifest/patch and confirm no RSI or allocation-cap
  bypass reaches the engine. Preserve the earlier source, database and config.
- [ ] Build on the target Linux/Python version. Install `requirements.txt`, run
  `python -m unittest discover -s tests -v` with **dummy TESTNET keys**, and record
  the dependency freeze, interpreter version and immutable release/source hashes.
  Windows test results alone do not validate the Linux runtime.
- [ ] Build the matching frontend with `npm ci`, `node --test src/*.test.js`,
  `npm run lint` and `npm run build`. Serve `dist/` from the same HTTPS origin
  whose exact value is `FRONTEND_URL`. Proxy `/api/` to loopback port 8000.
- [ ] Deploy this same reviewed source to a **fresh isolated Testnet state
  directory**, using the locked config marker, five levels and 50% seed.
  Do not relabel an existing wide-grid ledger as a compressed run.
- [ ] Confirm closed-hour ATR, floor-bound and ATR-bound geometry, both SMA
  gates, Post-Only rejection/retry, partial fills, actual commission assets,
  minimum notional and precision behavior.
- [ ] Demonstrate the marginal residual BUY without exceeding purchase-cost
  cap; SELLs and hard stop still work after `CAP_EXHAUSTED`; restart and
  STOP/START cannot clear it.
- [ ] Exercise disconnect/timeout with exact client-ID reconciliation, process
  restart, pending order cleanup, software stop/liquidation recovery, unsellable
  dust, and stale Yahoo/Binance data. Record evidence and unresolved alarms.
- [ ] Complete a prospective soak through at least one UTC daily signal refresh
  and a service restart. Simulate rare cap/stop/transport cases with the fake
  exchange tests as well; a quiet soak does not establish those behaviors.
- [ ] Mark the release accepted only after all above outcomes are reviewed. The
  prior retrospective runs are not a substitute for this new production-code soak.

## 2. Account, host and credentials

- [ ] Use a dedicated, legally available Binance Spot account/subaccount. Enable
  API read and Spot trading, restrict the key to the host's static outbound IP,
  and leave withdrawals/transfers, futures and margin execution disabled wherever
  separately configurable. If Binance bundles Spot & Margin permission, use only
  this Spot client; do not enable margin borrowing or fund margin/futures.
  [Binance API-key setup](https://www.binance.com/en-IN/support/faq/detail/360002502072).
- [ ] Match the tested commission model: disable BNB fee payment for this
  rollout; query current standard, special and tax commissions and confirm each
  effective maker/taker BUY/SELL rate is at most 0.1%. The preflight blocks higher
  rates or BNB discount mode rather than quietly widening the locked policy.
  [Binance commission API](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#query-commission-rates-user_data).
- [ ] For a fresh rollout, verify zero existing BTC and zero BTC/USDT open
  orders. Fund the explicitly approved allocation (template: 500 USDT) plus any
  separately chosen operational reserve. A reserve never enlarges the bot's cap.
- [ ] Synchronize UTC time, secure SSH/firewall access, restrict port 8000 to
  loopback, configure TLS and the exact frontend origin, unique admin password
  and independent random JWT secret (32+ characters). Set private outbound
  alert destinations and verify delivery through the operator's normal channel.
- [ ] Create the `gridbot` OS user and required state/cache/backup directories.
  Give `gridbot` ownership of writable state/cache, directory mode 0700 and config
  mode 0600. Make `mainnet.env` root:root 0600 for systemd EnvironmentFile access.
  Keep application/releases root-owned and not writable by the service account.
- [ ] Preserve Testnet using STOP and verified order cancellation. STOP leaves
  BTC in Spot and suspends the process-managed stop: inventory needs an explicit
  operator handling decision before a maintenance gap.

## 3. Initialize and inspect — no orders

Install the release and templates first. The following commands are for the
Linux host, with the service still stopped. They intentionally refuse to
overwrite an existing Mainnet database. They initialize local state only.

After placing the reviewed release at `/opt/grid-bot/current`, provision the
account, directories and templates. Run this in Bash; `set -e` stops rather than
overwriting an existing secret or configuration. If the account already exists,
review its identity and group before reusing it.

```bash
set -euo pipefail
cd /opt/grid-bot/current/binance-grid-bot-architect
getent passwd gridbot >/dev/null || sudo useradd --system --user-group \
  --home-dir /var/lib/grid-bot --shell /usr/sbin/nologin gridbot
sudo install -d -o root -g root -m 0700 /etc/grid-bot
sudo install -d -o gridbot -g gridbot -m 0700 \
  /var/lib/grid-bot/mainnet /var/cache/grid-bot-mainnet /var/backups/grid-bot/mainnet
sudo test ! -e /etc/grid-bot/mainnet.env
sudo test ! -e /var/lib/grid-bot/mainnet/config.json
sudo install -o root -g root -m 0600 deploy/mainnet/env.mainnet.example /etc/grid-bot/mainnet.env
sudo install -o gridbot -g gridbot -m 0600 deploy/mainnet/config.mainnet.example.json /var/lib/grid-bot/mainnet/config.json
sudo install -o root -g root -m 0644 deploy/mainnet/grid-bot-mainnet.service /etc/systemd/system/grid-bot-mainnet.service
sudoedit /etc/grid-bot/mainnet.env
```

Replace the API/admin/JWT placeholders, exact HTTPS domain and alert settings
using the protected editor. Do not start the service with template credentials.

```bash
cd /opt/grid-bot/current/binance-grid-bot-architect
sudo test ! -e /var/lib/grid-bot/mainnet/grid_mainnet.sqlite
sudo -u gridbot env \
  TRADING_ENVIRONMENT=MAINNET \
  BINANCE_MAINNET_API_KEY=local-state-initialization-only \
  BINANCE_MAINNET_API_SECRET=local-state-initialization-only \
  GRID_BOT_DB_PATH=/var/lib/grid-bot/mainnet/grid_mainnet.sqlite \
  .venv/bin/python -c 'import os; os.umask(0o077); from database import GridDatabase; GridDatabase()'
```

These noncredential strings only satisfy the environment validator; that command
does not construct an exchange client. Do not use them to run the service.
For the real read-only checks, systemd loads credentials privately from the
EnvironmentFile without printing them:

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --unit=grid-bot-mainnet-preflight \
  --property=User=gridbot --property=Group=gridbot \
  --property=WorkingDirectory=/opt/grid-bot/current/binance-grid-bot-architect \
  --property=EnvironmentFile=/etc/grid-bot/mainnet.env \
  /opt/grid-bot/current/binance-grid-bot-architect/.venv/bin/python \
  /opt/grid-bot/current/binance-grid-bot-architect/mainnet_preflight.py --fresh

sudo systemd-run --quiet --wait --pipe --collect \
  --unit=grid-bot-mainnet-preflight \
  --property=User=gridbot --property=Group=gridbot \
  --property=WorkingDirectory=/opt/grid-bot/current/binance-grid-bot-architect \
  --property=EnvironmentFile=/etc/grid-bot/mainnet.env \
  /opt/grid-bot/current/binance-grid-bot-architect/.venv/bin/python \
  /opt/grid-bot/current/binance-grid-bot-architect/mainnet_preflight.py --fresh --exchange
```

- [ ] Both preflights PASS. The first is offline; the second uses read-only
  public/private endpoints. Neither submits/cancels orders, writes a ledger or
  starts trading. Do not run `--fresh` after the service has created runtime state.
- [ ] Record the live PRICE_FILTER, LOT_SIZE, MARKET_LOT_SIZE, notional and
  percent-price/order-count filters. Do not assume a fixed $5 or $10 minimum.
  The engine revalidates exchange-rounded quantities, reverse SELL validity and
  funding immediately before order placement. A preflight is not a reservation
  of prices, filters or liquidity. [Binance filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md).
- [ ] Confirm the API key's trading permission in Binance. An optional
  `/api/v3/order/test` checks request acceptance without an order entering the
  matching engine; it does not demonstrate real fills, cancellation or stop
  execution. [Binance test-order endpoint](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#test-new-order-trade).
- [ ] If a SMA gate is closed, remain IDLE and wait; do not substitute RSI,
  weaken SMA200, change the spacing floor or bypass the allocation pause.

## 4. Start IDLE, then explicitly approve trading

```bash
sudo systemd-analyze verify /etc/systemd/system/grid-bot-mainnet.service
sudo systemctl daemon-reload
sudo systemctl start grid-bot-mainnet.service
sudo systemctl status grid-bot-mainnet.service --no-pager
```

- [ ] Confirm the authenticated dashboard/status: `trading_environment=MAINNET`,
  `strategy_version=sma200-compressed-atr-v1`, `strategy_baseline_locked=true`,
  `minimum_grid_return_pct=0.4`, fixed stop, SMA200, engine IDLE and no unresolved
  cleanup, ghost-order, liquidation, cap or lease alarm. The browser must show
  Mainnet quotes. No seed should exist before approved START.
- [ ] Enter exactly five total levels and the approved capital allocation.
  Calculate from a fresh center and **closed 1h ATR**. Review each rounded gap,
  lower/upper bounds and 15%-below-lower stop (or any retained tighter floor).
  Save. Refresh and inspect the pending grid token/settings. If the closed hourly
  ATR changed or the draft differs, recalculate; the server rejects stale geometry.
- [ ] Press authenticated START only after that review and an open macro gate.
  Use a human-attended first campaign. The template's 500 USDT is the tested
  allocation, not permission to use the entire account balance.
- [ ] Verify the actual seed's fills/fees, owned BTC, upper protective SELLs,
  pending lower BUYs, free quote and purchase-cost-plus-BUY commitments. Confirm
  the total stays within the cap; do not enlarge the cap to revive a paused grid.
- [ ] Observe at least the first BUY/SELL cycle and restart/reconciliation behavior
  under supervision. Enable boot startup only after acceptance:
  `sudo systemctl enable grid-bot-mainnet.service`.

The hard stop is **software-managed market liquidation**, not an exchange-hosted
guaranteed stop. Host/API outages, polling/cancel latency and market slippage can
delay or worsen an exit. Startup `--ready` normally returns IDLE and cancels
tracked orders; it does not automatically restore active risk coverage. An
unfinished liquidation is the safety exception and resumes verification/exit.

## 5. Incident handling and rollback

- [ ] Maintain one current ledger and order namespace. For an uncertain request,
  reconcile its exact client ID; never resend it as a new BUY. Do not delete the
  database, reset cap state or clear alarms to make START succeed.
- [ ] For rollback, use STOP and verify exchange cancellations, inspect remaining
  BTC and decide how it will stay protected. STOP or stopping systemd is not a
  full account liquidation. The dashboard's confirmed liquidation action is a
  separate market-exit operation and can leave exchange-filter dust.
- [ ] Once the service is stopped, create a consistent SQLite backup, keeping its
  current config, source release and environment together. Preserve committed WAL
  data using SQLite backup rather than copying a running `.sqlite` file alone:

```bash
# Choose a NEW private timestamped backup directory; do not overwrite an older one.
sqlite3 -readonly /var/lib/grid-bot/mainnet/grid_mainnet.sqlite \
  ".backup '/var/backups/grid-bot/mainnet/CHOOSE_NEW_TIMESTAMP/grid_mainnet.sqlite'"
```

- [ ] Reverting code must retain the newest Mainnet ledger and open-order facts.
  Do not boot an old ledger against newer exchange orders. Reject any rollback
  binary that cannot read the current schema/profile or honor residual/cap state;
  remain stopped with an explicit inventory-handling decision instead.
- [ ] After reconciliation, restart in `--ready`, inspect current Mainnet state,
  and review/START manually. Rotate exposed credentials independently of rollback.
