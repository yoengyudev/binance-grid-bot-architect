"""Dedicated, fenced SQLite ledger. Grid databases are never migrated here."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import time

from . import STRATEGY_ID


class Store:
    def __init__(self, path, policy):
        self.path = Path(path).resolve()
        if self.path.suffix != '.sqlite' or 'trend' not in self.path.stem or 'paper' not in self.path.stem:
            raise ValueError('Use a dedicated *trend*paper*.sqlite database')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {'trend_state','trend_candles','trend_events','sqlite_sequence'}:
                raise ValueError('Refusing a database containing non-trend tables')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS trend_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS trend_candles (time INTEGER PRIMARY KEY, open REAL, high REAL, low REAL, close REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS trend_events (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)')
            row = db.execute('SELECT payload FROM trend_state WHERE id=1').fetchone()
            if row:
                state = json.loads(row[0])
                if state['strategy_id'] != STRATEGY_ID or state['policy'] != policy.payload():
                    raise ValueError('Existing paper policy/capital differs; use a new isolated ledger')
            else:
                state = dict(strategy_id=STRATEGY_ID, policy=policy.payload(), account=None, entries_enabled=False,
                             activated_at=None, signal_cursor=0, quote=None, error=None, signal_error=None,
                             history_complete=False, hour_highs={}, rules=None, owner=None, lease_until=0,
                             created_at=time.time(), decision='Start paper trading to wait for a fresh signal')
                db.execute('INSERT INTO trend_state VALUES (1,?)',(json.dumps(state),))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=5000')
        return db

    def transact(self, owner, now, action):
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                state = json.loads(db.execute('SELECT payload FROM trend_state WHERE id=1').fetchone()[0])
                if state['owner'] not in (None,owner) and state['lease_until'] > now:
                    raise RuntimeError('Another trend paper runner owns this ledger')
                state['owner'], state['lease_until'] = owner, now+90
                result = action(db,state)
                db.execute('UPDATE trend_state SET payload=? WHERE id=1',(json.dumps(state),))
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    def read(self):
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            state = json.loads(db.execute('SELECT payload FROM trend_state WHERE id=1').fetchone()[0])
            events = [dict(id=r['id'],time=r['time'],kind=r['kind'],**json.loads(r['payload']))
                      for r in db.execute('SELECT * FROM trend_events ORDER BY id DESC LIMIT 100')]
            candles = [dict(r) for r in db.execute('SELECT * FROM trend_candles ORDER BY time DESC LIMIT 800')][::-1]
            count, last = db.execute('SELECT count(*),max(time) FROM trend_candles').fetchone()
            return state,events,candles,count,last

    @staticmethod
    def event(db, now, kind, payload):
        db.execute('INSERT INTO trend_events(time,kind,payload) VALUES (?,?,?)',(now,kind,json.dumps(payload)))

    def release(self, owner):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            state = json.loads(db.execute('SELECT payload FROM trend_state WHERE id=1').fetchone()[0])
            if state['owner'] == owner:
                state.update(owner=None,lease_until=0,entries_enabled=False,decision='Runner stopped; entries paused on restart')
                db.execute('UPDATE trend_state SET payload=? WHERE id=1',(json.dumps(state),))
