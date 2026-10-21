"""Deployment-only checks and explicit fresh TESTNET initialization; no orders."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal as D

RUN = Path('/home/ubuntu/prospective-testnet')
RELEASE = RUN/'releases/v1.0.5-production'
sys.path.insert(0, str(RELEASE))
from dotenv import dotenv_values

parser = argparse.ArgumentParser()
parser.add_argument('--initialize', action='store_true')
parser.add_argument('--reserve-existing-btc', action='store_true')
args = parser.parse_args()
os.umask(0o077)
env = dotenv_values(RUN/'.env')
assert env.get('TRADING_ENVIRONMENT') == 'TESTNET'
for name in ('BINANCE_TESTNET_API_KEY','BINANCE_TESTNET_API_SECRET','BOT_ADMIN_PASSWORD','BOT_JWT_SECRET'):
    value = env.get(name) or ''
    assert value and 'REPLACE' not in value, f'Configure {name} in the protected editor.'
assert len(env['BOT_JWT_SECRET']) >= 32
assert env.get('BOT_AUTH_MOCK_ENABLED') == '0'
assert env['GRID_BOT_DB_PATH'] == str(RUN/'state/grid_testnet.sqlite')
assert env['GRID_BOT_CONFIG_PATH'] == str(RUN/'state/config.json')
os.environ.update({k:v for k,v in env.items() if v is not None})
assert not (RELEASE/'.env').exists(), 'Release must not carry another .env.'
assert not subprocess.check_output(['git','-C',str(RELEASE),'status','--porcelain','--untracked-files=no'],text=True).strip()
commit = subprocess.check_output(['git','-C',str(RELEASE),'rev-parse','HEAD'],text=True).strip()
assert commit == subprocess.check_output(['git','-C',str(RELEASE),'rev-parse','refs/tags/v1.0.5-production^{commit}'],text=True).strip()
assert commit == (RUN/'evidence/release-commit.txt').read_text().strip()

from database import GridDatabase, resolve_database_path
from models import GridConfig
from production_baseline import BASELINE_ID, ATR_SPACING_FRACTION, MIN_GRID_RETURN, GRID_LEVELS, validate_profile
from budget_policy import residual_quantity, CAP_EXHAUSTED
assert BASELINE_ID == 'sma200-compressed-atr-v1'
assert ATR_SPACING_FRACTION == D('.5') and MIN_GRID_RETURN == D('.004') and GRID_LEVELS == 5
assert callable(residual_quantity) and CAP_EXHAUSTED == 'CAP_EXHAUSTED'
validate_profile(json.loads((RUN/'state/config.json').read_text()), required=True)
config = GridConfig.load()
assert config.strategy_baseline == BASELINE_ID
db = resolve_database_path()
assert db == RUN/'state/grid_testnet.sqlite'
assert db != Path('/home/ubuntu/binance-grid-bot-architect/grid_testnet.sqlite').resolve()
report = {'checked_at_utc':datetime.now(timezone.utc).isoformat(),'commit':commit,
          'baseline':BASELINE_ID,'environment':'TESTNET','database':str(db),'offline_checks':'PASS'}
if args.initialize:
    for service in ('binance-grid-bot.service','binance-grid-prospective.service'):
        state = subprocess.run(['systemctl','is-active',service],capture_output=True,text=True).stdout.strip()
        assert state not in ('active','activating','deactivating','reloading'), f'Stop {service} first.'
    assert not db.exists() and not Path(str(db)+'-wal').exists() and not Path(str(db)+'-shm').exists(), 'Never overwrite an existing prospective ledger.'
    from exchange_handler import create_exchange
    exchange = create_exchange()
    # Reads only. A fresh ledger must never take ownership of another run's orders.
    assert not exchange.fetch_open_orders(config.symbol), 'Resolve existing BTC/USDT orders before changing ledgers.'
    balance = exchange.fetch_balance()
    btc = D(str(balance['BTC']['total']))
    assert btc == 0 or args.reserve_existing_btc, 'Existing BTC requires an explicit reserve decision; see runbook.'
    assert D(str(balance['USDT']['free'])) >= config.investment_quote
    evidence = RUN/'evidence/initialization.json'
    assert not evidence.exists()
    GridDatabase()
    report.update(initialized=True,existing_btc_reserved=str(btc),free_usdt=str(balance['USDT']['free']))
    evidence.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
