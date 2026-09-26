# Optional Docker deployment

Docker is not required for this bot. The existing EC2 `systemd` service remains
the recommended live deployment. Docker packages Python and the dependencies
for easier rebuilds, but moving a live bot requires moving its SQLite state.

The Compose service uses outbound network access only; it publishes no ports.
It mounts `.env` read-only and keeps the mutable `config.json` and SQLite state
in `docker-data/`. Both `.env` and `docker-data/` are excluded from the image
and Git. The image runs as UID 1000, matching the `ubuntu` user on the current VM.

## Build without starting a second bot

```bash
docker compose config
docker compose build
```

Building the image does not connect to Binance or start trading.

## Switch the current EC2 VM to Docker (only if desired)

Do this during a maintenance window. Keep the same Binance keys, Telegram token,
config and database. Do not run the `systemd` bot and Compose bot together: they
would poll the same Telegram bot and could place duplicate exchange orders.

```bash
cd ~/binance-grid-bot-architect
sudo systemctl stop binance-grid-bot.service
sudo systemctl disable binance-grid-bot.service
mkdir -p docker-data
cp config.json grid_bot.sqlite3 docker-data/
chmod 700 docker-data
chmod 600 docker-data/config.json docker-data/grid_bot.sqlite3 .env
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 bot
```

If SQLite has `grid_bot.sqlite3-wal` or `grid_bot.sqlite3-shm` beside the
database after stopping the old service, copy those files into `docker-data/`
too before `docker compose up`. Keep a backup of the original files until the
new container has reconciled its orders. The Docker startup check refuses to
create a fresh database implicitly; a missing or invalid database leaves the
container stopped. A missing `.env` or config also leaves it stopped.

The container restarts after a transient nonzero bot exit, such as a network
error. A trading safety halt (exit code 2) or a normal `/stop` does not restart
automatically. Docker's `on-failure` policy **does not restore the container
after a Docker daemon or VM restart**. For a 24/7 deployment across reboots,
the existing `systemd` service is simpler and already configured for this.

To go back to `systemd`, first stop Compose. Copy the latest config and
database (plus any SQLite WAL file) from `docker-data/` back to the project
directory before enabling and starting `binance-grid-bot.service`; use only one
runner at a time. Never restart the old service with its stale database.
