# Prospective Testnet release

`v1.1.0-testnet.1` preserves `sma200-compressed-atr-v1`, including Residual Fill
and 0.5x closed-hour ATR compression. No strategy parameter is changed here.

Runtime layout:

- Source: `/home/ubuntu/prospective-testnet/releases/v1.1.0-testnet.1`
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

Patch release 1.0.1 repairs an execution recovery issue found during authorized
Telegram maintenance. A reviewed same-grid resume previously waited for Yahoo
even before rebuilding SELL protection for already-owned BTC. That resume now
restores only protective SELLs while the Yahoo BUY gate is closed; no new BUY or
market seed is permitted. New deployments and other reanchors retain their
existing macro gates. Fixed stop, allocation cap, reserved wallet BTC boundary,
grid geometry and strategy constants are unchanged. The original 1.0.0 tag is
retained. Record the maintenance interruption and begin a new observation window.

Patch release 1.0.2 recovers missing recent Yahoo daily closes only from all 24
validated same-feed UTC hourly bars. It preserves the strict completed-day
SMA200 rule and fails closed when recovery is incomplete. Existing valid daily
closes are unchanged. See MACRO_TREND.md for provenance and validation.

Patch 1.0.3 keeps BUY entries post-only and permits GTC limit SELL exits to
execute at their saved target or better after a rebound. For an explicitly
reviewed TESTNET maintenance handover only, `main.py --ready --resume-reviewed`
retains orders/parent lots after reconciliation. It refuses Mainnet, IDLE/HALTED,
fault/cleanup/pause/reset/liquidation states and unexplained wallet BTC. The
canonical unit remains plain `--ready`; the operator removes the one-time
runtime override after verification. This does not enable post-stop restarts.

Patch 1.0.4 adds durable between-poll stop-trade catch-up, preserves verified
zero commissions, and rejects stale dashboard market events. See
EXECUTION_SAFETY.md for behavior, limitations and validation. This remains
Testnet; fixed strategy settings and manual post-stop review are retained.

Patch 1.0.5 adds complete same-feed 15-minute recovery when hourly Yahoo
recovery is incomplete, validates refresh results across UTC rollover, and
adds deduplicated macro BUY-state alerts. It retains the strict daily
SMA200 gate and manual post-stop review. During this deployment the
existing completed liquidation remains HALTED; no grid is restarted.

Patch 1.0.6 verifies liquidation commissions through private executions,
includes consolidated MARKET exits in Recent Fills, and exits 75 for
retryable runner-lease contention. Configuration/review errors remain
exit 2. Keep the completed liquidation HALTED during deployment.
Back up SQLite before an explicitly audited, journaled fee correction;
never change purchase basis, timestamps, strategy rules or wallet BTC.


## Experimental six-feature code release

`v1.1.0-testnet.1` includes six independently opt-in Testnet policies: hourly
regime, inventory sizing, campaign dollar risk, automatic ATR BUY adaptation,
confirmed-SELL capital recycling and fee-aware net-edge validation. The base
profile remains `sma200-compressed-atr-v1`. All feature flags default to false.
This is not a Mainnet release or evidence of improved profitability.

The current-campaign rollout preserves the private environment, existing
ledger/configuration, reserved wallet BTC, orders, and strategy settings.
Install the tagged code and use the documented reviewed Testnet handover;
do not run fresh initialization against an existing ledger. Check the six
API feature objects after rollout: all must remain disabled for this handover.

Activating the entire bundle is a separate configuration step requiring an
isolated ledger, explicit dollar-risk budget and reviewed fee assumptions.
Never copy old trade history into a supposedly fresh dollar-risk campaign,
erase previous losses, silently reserve carried bot BTC as manual wallet BTC,
or force-liquidate an existing position merely to enable the features.
Their policy documents describe local implementation validation; deployment
and activation are recorded separately in release evidence.

Validation: 805 local backend tests, including all six features together.
The dashboard assets and frontend source are unchanged in this release.
No production dependencies were added. Run Linux offline smoke tests before
handover, back up SQLite, and retain the previous release for rollback.
