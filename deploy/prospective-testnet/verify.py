"""Read-only post-deployment identity/status check; never STARTs trading."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen

RUN=Path('/home/ubuntu/prospective-testnet')
RELEASE=RUN/'releases/v1.0.3-production'
sys.path.insert(0,str(RELEASE))
from dotenv import dotenv_values
env=dotenv_values(RUN/'.env')
pid=subprocess.check_output(['systemctl','show','binance-grid-prospective.service','-p','MainPID','--value'],text=True).strip()
assert pid!='0'
process_env=dict(item.decode().split('=',1) for item in Path('/proc',pid,'environ').read_bytes().split(b'\0') if b'=' in item)
for key in ('TRADING_ENVIRONMENT','GRID_BOT_DB_PATH','GRID_BOT_CONFIG_PATH'):
    assert process_env[key] == env[key]
origin=env['FRONTEND_URL'].rstrip('/')
request=Request('http://127.0.0.1:8000/api/auth/login',
    data=json.dumps({'password':env['BOT_ADMIN_PASSWORD']}).encode(),
    headers={'Origin':origin,'Content-Type':'application/json'})
with urlopen(request,timeout=15) as response:
    cookie=response.headers['Set-Cookie'].split(';',1)[0]
with urlopen(Request('http://127.0.0.1:8000/api/bot/status',headers={'Cookie':cookie}),timeout=20) as response:
    status=json.load(response)
assert status['trading_environment']=='TESTNET'
assert status['strategy_version']=='sma200-compressed-atr-v1' and status['strategy_baseline_locked'] is True
assert status['engine_fault'] is None
assert not status.get('order_cleanup_required') and not status.get('ghost_order_alarm')
db=Path(env['GRID_BOT_DB_PATH'])
with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=2) as conn:
    assert conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    counts=dict(conn.execute('SELECT status,COUNT(*) FROM grid_orders GROUP BY status'))
assert Path('/proc',pid,'cwd').resolve() == RELEASE
report={'checked_at_utc':datetime.now(timezone.utc).isoformat(),'pid':pid,
        'database':str(db),'strategy_version':status['strategy_version'],
        'strategy_baseline_locked':status['strategy_baseline_locked'],
        'engine_status':status['engine_status'],'trading_state':status['trading_state'],
        'engine_fault':status['engine_fault'],'has_grid_run':status['has_grid_run'],
        'orders_by_status':counts,'sqlite_quick_check':'ok'}
print(json.dumps(report,indent=2))
