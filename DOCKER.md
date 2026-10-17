# Optional Docker deployment

Docker is not required for this bot. The existing EC2 `systemd` service remains
an available deployment method. Docker packages Python 3.12 and the dependencies
for easier rebuilds, but moving a live bot requires preserving its SQLite state.

The Compose service publishes the dashboard API to host loopback on port 8000.
It mounts `.env` read-only and keeps the mutable `config.json` and SQLite state
in `docker-data/`. Both `.env` and `docker-data/` are excluded from the image
and Git. The image runs as UID 1000, matching the `ubuntu` user on the current VM.
The container starts in `--ready` mode: startup reconciles and cancels bot-owned
orders before resting in IDLE. Trading requires an authenticated dashboard START.
If cancellation cannot be verified, the cleanup alarm persists and START stays
blocked while the service retries. An incomplete liquidation resumes on startup;
a completed liquidation still requires manual review and a new START.

## Environment and persistent files

Set exactly one environment in `.env`, with the credentials for that environment:

| TRADING_ENVIRONMENT | Required credentials | Database in docker-data/ |
| --- | --- | --- |
| TESTNET | BINANCE_TESTNET_API_KEY, BINANCE_TESTNET_API_SECRET | grid_testnet.sqlite |
| MAINNET | BINANCE_MAINNET_API_KEY, BINANCE_MAINNET_API_SECRET | grid_mainnet.sqlite |

The application reads the read-only `/app/.env` mount using python-dotenv.
Generic BINANCE_API_KEY/BINANCE_SECRET_KEY aliases are not used. Configure
BOT_ADMIN_PASSWORD, BOT_JWT_SECRET and the exact HTTPS FRONTEND_URL for the
production dashboard. Keep `.env` local and outside source control.

Compose mounts the entire `docker-data/` directory at `/data`, preserving both
database filenames and their `-wal`/`-shm` companions. Only the selected
environment's database is opened. The database's internal environment marker
must match; never rename or convert a Testnet ledger to a Mainnet ledger.
Create `docker-data/` explicitly: missing bind sources are rejected instead of
being silently created. Use separate deployment directories/config files if
operating both environments, and only one runner per account.

## Build without starting a second bot

```bash
docker compose config --quiet
docker compose build
```

Building the image does not connect to Binance or start trading. `config --quiet`
validates without printing resolved configuration. This image runs
`python docker_runner.py`, which checks the persistent files before launching
`main.py --ready`.

## Switch the current EC2 VM to Docker (only if desired)

Do this during a maintenance window. Keep the same Binance keys, Telegram token,
config and database. Do not run the `systemd` bot and Compose bot together.
Before stopping the old service, use dashboard STOP and verify bot order cleanup.
STOP keeps bot BTC in Spot and suspends risk monitoring during the cutover.

Run the following in Bash on Ubuntu. Build the image first as above, and install
the SQLite CLI with `sudo apt-get install -y sqlite3` if needed. Select the
filename matching `.env`; the example below uses TESTNET. If the old service
uses a custom database directory, use its actual current database path as the
source rather than the project-root path below.

```bash
cd ~/binance-grid-bot-architect
set -euo pipefail
BOT_DB_NAME=grid_testnet.sqlite
# For TRADING_ENVIRONMENT=MAINNET, use instead:
# BOT_DB_NAME=grid_mainnet.sqlite

# Refuse to overwrite a possibly newer Docker ledger or config.
test ! -e "docker-data/$BOT_DB_NAME"
test ! -e docker-data/config.json
test -f "$BOT_DB_NAME"
test -f config.json

sudo systemctl stop binance-grid-bot.service
BOT_BACKUP="$HOME/grid-bot-backups/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$BOT_BACKUP"
cp config.json "$BOT_BACKUP/config.json"
sqlite3 -readonly "$BOT_DB_NAME" ".backup '$BOT_BACKUP/$BOT_DB_NAME'"
test "$(sqlite3 -readonly "$BOT_BACKUP/$BOT_DB_NAME" 'PRAGMA quick_check;')" = ok

# SQLite's backup includes committed WAL data in one consistent database file.
sudo install -d -o 1000 -g 1000 -m 0700 docker-data
sudo install -o 1000 -g 1000 -m 0600 "$BOT_BACKUP/config.json" docker-data/config.json
sudo install -o 1000 -g 1000 -m 0600 "$BOT_BACKUP/$BOT_DB_NAME" "docker-data/$BOT_DB_NAME"
chmod 600 .env
docker compose run --rm --no-deps bot python -c 'from docker_runner import ready_to_start; raise SystemExit(0 if ready_to_start() else 1)'
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 bot
```

If `docker-data/` already contains state, review which ledger is current before
proceeding. The commands intentionally refuse to overwrite it; reuse the existing
mount only after confirming its environment, config and current state. Retain
the backup and original installation. The startup check refuses to create a
fresh database implicitly; missing or invalid state leaves the container stopped.
For a brand-new account/run, initialize the selected ledger explicitly with
GridDatabase before starting, rather than substituting an unrelated database.

Verify the HTTPS dashboard and saved engine state before disabling the old
service's automatic startup, then run:

```bash
sudo systemctl disable binance-grid-bot.service
```

Review the grid and press START manually when appropriate.

The container restarts after a transient nonzero bot exit, such as a network
error. A trading safety halt (exit code 2) does not restart automatically.
Docker's `on-failure` policy **does not restore the container
after a Docker daemon or VM restart**. For a 24/7 deployment across reboots,
the existing `systemd` service is simpler and already configured for this.

To go back to `systemd`, first use dashboard STOP, verify cancellation, and stop
Compose. Take a consistent SQLite backup of the latest selected
`docker-data/grid_testnet.sqlite` or `docker-data/grid_mainnet.sqlite`, preserving
its environment marker. Restore that backup and its matching config to the
systemd service's original paths before enabling/starting it. Preserve any
newer original state separately before replacing it. Never restart the old
service with its stale ledger, and never run both services together.
