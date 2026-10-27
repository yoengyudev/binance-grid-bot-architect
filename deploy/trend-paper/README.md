# Git-tagged SOL paper deployment

Release `v1.2.0-paper.1` contains the Python paper service, the compiled dashboard
and a snapshot of its frontend source. The frontend has a separate local Git
repository with no configured remote; its commit and source hashes are recorded
in `release-manifest.json`. No local credentials, SQLite data, node_modules or
untracked research files are included.

The existing EC2 grid stays at `https://13-239-48-244.sslip.io`. The separate paper
site is `https://trend.13-239-48-244.sslip.io`. This release cannot submit private
exchange orders and is not a Mainnet trading deployment.

## Release contents and build

`dashboard/` is a verified, same-origin production build. `frontend/` contains
the tracked source from the recorded frontend commit. Rebuild from that source
with the pinned package lock and an explicit empty API origin:

```sh
npm ci
VITE_TREND_API_BASE='' npm run build:trend
```

Use the Node version supported by the pinned Vite version. An empty override
selects the current HTTPS origin, instead of the development localhost API.
Do not build npm assets on the small EC2 instance during a trading service rollout.

## EC2 layout

- `/home/ubuntu/sol-trend-paper/releases/v1.2.0-paper.1`: immutable tagged worktree.
- `/home/ubuntu/sol-trend-paper/trend-paper.env`: private paper configuration only.
- `/home/ubuntu/sol-trend-paper/state/sol-trend-paper.sqlite`: dedicated 100-USDT ledger.
- `/home/ubuntu/sol-trend-paper/logs/`: independent application/service logs.
- `/var/www/trend-paper/releases/v1.2.0-paper.1`: public static files only.
- `/var/www/trend-paper/current`: atomic public dashboard link.
- `sol-trend-paper.service`: separate loopback API on port 8010.

The initial release reuses the already-installed Python virtual environment from
the existing grid release; no dependency upgrades are made to that environment.
Run the paper tests against it before installation. Paper startup needs only
public unsigned HTTPS GETs; it must not load the grid `.env` into its process.

Fetch the tag through Git, verify its peeled commit against the release record,
then add the detached worktree:

```sh
git -C /home/ubuntu/prospective-testnet/releases/v1.1.0-testnet.1 fetch origin tag v1.2.0-paper.1
git -C /home/ubuntu/prospective-testnet/releases/v1.1.0-testnet.1 worktree add --detach \
  /home/ubuntu/sol-trend-paper/releases/v1.2.0-paper.1 v1.2.0-paper.1
ln -s /home/ubuntu/prospective-testnet/releases/v1.1.0-testnet.1/.venv \
  /home/ubuntu/sol-trend-paper/releases/v1.2.0-paper.1/.venv
cd /home/ubuntu/sol-trend-paper/releases/v1.2.0-paper.1
.venv/bin/python -m unittest tests.test_trend_following -q
```

Create the private file from `env.ec2.example`, with mode 0600 and a strong admin
password plus a new random JWT secret. For the reviewed initial installation the
existing dashboard password may be reused, without copying any exchange keys or
other grid environment values. The paper and grid session cookies have different
names and issuers. Create the state/log directories as ubuntu with mode 0700.

Install the provided unit, copy only the manifest-verified static assets to the
public directory, and append the supplied Caddy site block to a backed-up config.
Validate Caddy before reloading it; do not replace the old site's routing.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now sol-trend-paper.service
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

First startup restores the historical indicator seed in bounded pages. Verify
the authenticated status reports PAPER, SOL/USDT, 100 capital, complete history,
fresh market data and no error before choosing START. A START enables future
signals; no historical entry is backfilled. During the reviewed first deployment
this paper START is authorized after these checks. New TLS certificates may take
a short time to become available.

## Check and rollback

Check HTTPS dashboard 200, unauthenticated status 401, authenticated status PAPER,
secure HttpOnly paper cookie, current source/asset hashes and separate state/log
paths. Record the grid PID, environment/config hashes, engine status and ledger
before and after. No grid STOP, START, reset or liquidation is part of this rollout.

To roll back this initial addition, stop/disable only `sol-trend-paper.service`,
restore the saved Caddy configuration and reload Caddy after validation. Retain
the paper ledger and logs; never delete them or copy grid history into them.
Keep the old grid service, source worktree, static files and database untouched.

For paper strategy semantics and execution limits see `../../TREND_PAPER.md`.
