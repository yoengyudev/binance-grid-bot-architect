# Prospective Testnet release

`v1.0.0-production` freezes `sma200-compressed-atr-v1`, including Residual Fill
and 0.5x closed-hour ATR compression. No strategy parameter is changed here.

Runtime layout:

- Source: `/home/ubuntu/prospective-testnet/releases/v1.0.0-production`
- Environment: `/home/ubuntu/prospective-testnet/.env` (private, outside Git)
- Ledger: `/home/ubuntu/prospective-testnet/state/grid_testnet.sqlite`
- Configuration: `/home/ubuntu/prospective-testnet/state/config.json`
- Logs: `/home/ubuntu/prospective-testnet/logs/engine.stdout.log` and
  `engine.stderr.log`
- Evidence: `/home/ubuntu/prospective-testnet/evidence/`
- Service: `binance-grid-prospective.service`

The required TESTNET database basename is preserved in a completely separate
directory. Never copy the previous ledger into this run or use a symlink to it.
Keep the old ledger/configuration/service available for reviewed recovery.

Copy `env.testnet.example` to the private environment location and supply only
Testnet credentials. The example deliberately disables Telegram. The protected
server setup may retain the operator's existing admin password for dashboard
access; generate a fresh JWT secret to separate sessions. Never print secrets.

Use the tagged `deploy/mainnet/config.mainnet.example.json` as the locked IDLE
bootstrap profile; the path name does not select Mainnet. The service always
uses `main.py --ready`. After provisioning and reconciliation, expect IDLE with
no grid run. Calculate, review and save the grid in the matching dashboard, then
use manual START with Auto-cover unchecked. A service restart is not a START.

`preflight.py` checks the tagged checkout, constants and isolated paths. Its
`--initialize` mode refuses an existing ledger and only reads exchange orders
and balances before creating fresh local state. Existing wallet BTC requires
`--reserve-existing-btc`, an explicit deployment acknowledgement, and stays
outside the new grid. New API keys for the same account do not isolate wallets.
Stop the old engine and verify zero open orders before switching services.

`verify.py` reads authenticated status and process identity; it never STARTs
trading. `release-manifest.json` pins tested source hashes and dashboard assets.
The dashboard build is included in Git so backend and UI are fetched together;
its separate frontend source commit is recorded in that manifest.

Keep code/settings fixed during prospective observation. Record the actual
ACTIVE time as burn-in start. Historical studies and the previous interrupted
observation are not prospective evidence for this release. No Mainnet deployment
or automatic post-stop restart is enabled by these files.
