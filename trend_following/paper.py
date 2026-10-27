"""Persistent paper coordinator; never imports the grid engine or credentials."""
from datetime import datetime, timezone
from decimal import Decimal as D
import threading
import time
import uuid

from .account import Account, Policy, Rules, decimal
from .strategy import Candle, HOUR, FOUR_HOURS, indicators, DataGap
from .store import Store

# Proven exchange closure hours admitted by the frozen historical study.
KNOWN_CLOSURES = frozenset(int(datetime.fromisoformat(t).timestamp()) for t in
                          ('2021-09-29T07:00:00+00:00','2021-09-29T08:00:00+00:00','2023-03-24T13:00:00+00:00'))
ENTRY_LATENESS = 90  # Missed opportunities are skipped, never bought retrospectively.
QUOTE_MAX_AGE = 10


class PaperEngine:
    def __init__(self, path, policy=Policy(), clock=time.time):
        self.policy, self.clock = policy, clock
        self.store = Store(path,policy)
        self.owner = uuid.uuid4().hex
        self.lock = threading.RLock()
        self._cache_key, self._signals = None, []
        self.store.transact(self.owner,self.clock(),lambda db,state: None)

    @staticmethod
    def _account(state, policy):
        if not state['rules']:
            return None
        return Account(Rules(**{k:decimal(v) for k,v in state['rules'].items()}),policy,state['account'])

    def _save(self, db, state, account, now):
        for fill in account.fills:
            self.store.event(db,now,'fill',fill)
        for trade in account.trades:
            self.store.event(db,now,'trade',trade)
        for reject in account.rejects:
            self.store.event(db,now,'rejection',reject)
            state['decision'] = 'Entry skipped: '+reject['reason'].replace('_',' ')
        state['account'] = account.saved()

    def error(self, detail):
        now = self.clock()
        with self.lock:
            def record(db,state):
                if state['error'] != detail:
                    self.store.event(db,now,'data_error',dict(message=detail))
                state['error'] = detail
            self.store.transact(self.owner,now,record)

    def process(self, quote, rules, candles=(), *, history_complete=True, signal_error=None):
        now = self.clock()
        if not -1 <= now-quote.observed_at <= QUOTE_MAX_AGE or abs(now-quote.server_time)>QUOTE_MAX_AGE:
            raise ValueError('Fresh market data is required; no paper fill attempted')
        with self.lock:
            def cycle(db,state):
                state.update(quote=quote.payload(),error=None,signal_error=signal_error)
                if rules is not None and rules.payload()!=state['rules']:
                    self.store.event(db,now,'market_rules',rules.payload())
                    state['rules'] = rules.payload()
                account = self._account(state,self.policy)
                if account is None:
                    state['decision'] = 'Waiting for valid SOL market rules'
                    return
                for c in candles:
                    if c.time+HOUR > int(quote.server_time)//HOUR*HOUR:
                        raise ValueError('Forming/future candles cannot enter the signal history')
                    prior = db.execute('SELECT * FROM trend_candles WHERE time=?',(c.time,)).fetchone()
                    values = (c.time,c.open,c.high,c.low,c.close)
                    if prior and tuple(prior)!=values:
                        raise ValueError('Previously admitted closed candle changed')
                    db.execute('INSERT OR IGNORE INTO trend_candles VALUES (?,?,?,?,?)',values)
                state['history_complete'] = bool(history_complete)
                was_active = account.active
                if was_active:
                    hour = str(int(now)//HOUR*HOUR)
                    state['hour_highs'][hour] = str(max(decimal(state['hour_highs'].get(hour,'0')),quote.mid))
                # Stops continue while entries are paused or candle signals fail.
                account.boundary(now,quote.bid)
                exited = was_active and not account.active
                if was_active and account.active and quote.bid<=account.stop:
                    state['decision'] = 'Exit blocked by order minimum; residual position remains monitored'
                    state['signal_error'] = 'Stop exit cannot satisfy modeled exchange minimum'
                if state['history_complete'] and not signal_error:
                    count,last = db.execute('SELECT count(*),max(time) FROM trend_candles').fetchone()
                    if last is None or last+HOUR < int(quote.server_time)//HOUR*HOUR:
                        state['signal_error'] = 'Latest closed hourly candle is missing'
                    else:
                        key = (count,last)
                        bars = None
                        try:
                            if key!=self._cache_key:
                                bars = [Candle(*row) for row in db.execute('SELECT * FROM trend_candles ORDER BY time')]
                                self._signals = indicators(bars,allowed_missing=KNOWN_CLOSURES)
                                self._cache_key = key
                            signals = self._signals
                        except DataGap as error:
                            signals = []
                            state['signal_error'] = str(error)
                        if signals:
                            state['signal'] = signals[-1].payload()
                            state['signal_history'] = [s.payload() for s in signals[-200:]]
                            for sig in signals:
                                if sig.closed_at<=state['signal_cursor'] or sig.scheduled_at>quote.server_time:
                                    continue
                                eligible = (state['entries_enabled'] and not exited and rules is not None and
                                            state['activated_at'] is not None and sig.scheduled_at>=state['activated_at'] and
                                            0<=quote.server_time-sig.scheduled_at<=ENTRY_LATENESS)
                                if account.active:
                                    high = db.execute('SELECT max(high) FROM trend_candles WHERE time>=? AND time<?',
                                                      (account.opened,sig.closed_at)).fetchone()[0]
                                    highs = [decimal(v) for h,v in state['hour_highs'].items()
                                             if account.opened//HOUR*HOUR<=int(h)<sig.closed_at]
                                    peak = max([account.entry]+([decimal(high)] if high is not None else [])+highs)
                                else:
                                    peak = None
                                before = account.active
                                account.boundary(now,quote.bid,sig.payload(),peak,allow_entry=eligible,
                                                 ask=quote.ask,mark=quote.mid)
                                if before and not account.active:
                                    exited = True
                                if not before and account.active:
                                    state['hour_highs'] = {str(int(now)//HOUR*HOUR):str(quote.mid)}
                                    state['decision'] = 'Paper position opened on delayed bullish crossover'
                                elif sig.ema_entry and not eligible:
                                    state['decision'] = 'Entry skipped: paused, pre-start or missed execution window'
                                elif not account.active:
                                    state['decision'] = 'Waiting for a fresh bullish EMA crossover above EMA200'
                                state['signal_cursor'] = sig.closed_at
                        elif not state['signal_error']:
                            state['signal_error'] = 'Waiting for 200 completed 4-hour candles'
                if not account.active:
                    state['hour_highs'] = {}
                if exited:
                    state['decision'] = 'Position closed; waiting for a new qualifying crossover'
                account.mark(quote.mid)
                self._save(db,state,account,now)
            self.store.transact(self.owner,now,cycle)

    def control(self, action):
        if action not in ('start','pause','close'):
            raise ValueError('Unknown paper action')
        now = self.clock()
        with self.lock:
            def apply(db,state):
                if action=='start':
                    quote = state['quote']
                    if (not quote or now-quote['observed_at']>QUOTE_MAX_AGE or state['error'] or
                            state['signal_error'] or not state['history_complete']):
                        raise ValueError('Wait for fresh market data and complete signal history before START')
                    if not state['entries_enabled']:
                        state['activated_at'] = now
                    state['entries_enabled'] = True
                    state['decision'] = 'Entries enabled; waiting for a fresh qualifying signal'
                else:
                    state['entries_enabled'] = False
                    state['decision'] = 'Entries paused; any open position remains monitored'
                    if action=='close':
                        quote = state['quote']
                        if not quote or now-quote['observed_at']>QUOTE_MAX_AGE or state['error']:
                            raise ValueError('A fresh quote is required to simulate closing a position')
                        account = self._account(state,self.policy)
                        if account and account.qty>0:
                            if not account.sell(now,quote['bid'],'manual_paper_exit'):
                                raise ValueError('Position is below the modeled SELL minimum')
                            state['hour_highs'] = {}
                            self._save(db,state,account,now)
                        state['decision'] = 'Paper position closed; entries paused'
                self.store.event(db,now,'control',dict(action=action,message=state['decision']))
            self.store.transact(self.owner,now,apply)
        return self.status()

    def status(self):
        now = self.clock()
        state,events,candles,count,last = self.store.read()
        quote = state['quote']
        fresh = bool(quote and -1<=now-quote['observed_at']<=QUOTE_MAX_AGE and not state['error'])
        account = self._account(state,self.policy)
        raw = decimal(quote['mid']) if quote else None
        equity = account.cash+account.qty*raw if account and raw else self.policy.capital
        position = None
        if account:
            position = dict(quantity=str(account.qty),cost_basis=str(account.basis),
                            average_cost=str(account.basis/account.qty) if account.qty else None,
                            entry=str(account.entry) if account.entry is not None else None,
                            opened_at=account.opened,stop=str(account.stop) if account.stop is not None else None,
                            active=account.active,residual=not account.active and account.qty>0)
        chart = []
        groups = {}
        for bar in candles:
            groups.setdefault(bar['time']//FOUR_HOURS*FOUR_HOURS,[]).append(bar)
        for t, group in groups.items():
            if len(group)==4:
                chart.append(dict(time=t+FOUR_HOURS,open=group[0]['open'],high=max(c['high'] for c in group),
                                  low=min(c['low'] for c in group),close=group[-1]['close']))
        status = ('DATA_UNAVAILABLE' if not fresh else 'INITIALIZING' if not state['history_complete'] else
                  'SIGNALS_PAUSED' if state['signal_error'] else 'IN_POSITION' if account and account.active else
                  'WAITING_FOR_SIGNAL' if state['entries_enabled'] else 'ENTRIES_PAUSED')
        return dict(strategy_id=state['strategy_id'],symbol='SOL/USDT',mode='PAPER',market_source='BINANCE_PUBLIC_MAINNET',
                    status=status,entries_enabled=state['entries_enabled'],market_fresh=fresh,quote=quote,
                    error=state['error'] or state['signal_error'],decision=state['decision'],policy=self.policy.payload(),
                    equity=str(equity),cash=str(account.cash if account else self.policy.capital),
                    realized_pnl=str(account.realized if account else D(0)),
                    unrealized_pnl=str(account.qty*raw-account.basis) if account and raw else '0',
                    fees=str(account.fees if account else D(0)),drawdown_percent=str(account.dd if account else D(0)),
                    position=position,signal=state.get('signal'),signal_history=state.get('signal_history',[]),
                    candles=chart[-160:],events=events,hourly_bars=count,last_closed_hour=last,
                    history_complete=state['history_complete'],updated_at=now,created_at=state['created_at'],
                    entry_delay_seconds=HOUR,entry_lateness_seconds=ENTRY_LATENESS,quote_max_age_seconds=QUOTE_MAX_AGE)

    def close(self):
        with self.lock:
            self.store.release(self.owner)
