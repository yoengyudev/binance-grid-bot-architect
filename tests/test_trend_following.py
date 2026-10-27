"""Financial and service tests for the isolated paper strategy; no networking."""
from contextlib import closing
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from trend_following.account import Account, Policy, Rules
from trend_following.feed import PublicFeed, Quote, symbol_rules
from trend_following.paper import PaperEngine
from trend_following.service import Config, create_app, Worker
from trend_following.store import Store
from trend_following.strategy import Candle, Signal, indicators, DataGap

T = 1704067200
RULES = Rules(D('.001'),D('.001'),D('100000'),D('5'),D('.01'),D('.01'))
SIG = Signal(T,100.,1.,101.,100.,99.,99.,100.,True,False)


class WalletTests(unittest.TestCase):
    def test_exact_risk_includes_rounding_costs_and_carried_basis(self):
        a = Account(RULES)
        a.qty,a.basis,a.cash = D('.0005'),D('.05'),D('99.95')
        self.assertTrue(a.buy(T+3600,100,SIG.payload()))
        f = a.fills[-1]
        loss = D(f['quantity'])*D(f['price'])-a.floor(D(f['quantity'])*D('.999'))*D(f['stop'])*D('.999')*D('.999')
        self.assertEqual(loss,D(f['planned_new_loss']))
        self.assertLessEqual(loss+D(f['carried_basis']),D(f['risk_budget']))
        self.assertLessEqual(a.basis,D('80'))

    def test_same_price_roundtrip_loses_and_retains_dust_basis(self):
        a = Account(RULES)
        a.buy(T,100,SIG.payload());a.sell(T+60,100,'manual_paper_exit')
        self.assertLess(a.realized,0)
        self.assertGreater(a.qty,0)
        self.assertGreater(a.basis,0)
        a.invariant()

    def test_gap_can_exceed_risk_and_does_not_reenter(self):
        a = Account(RULES);a.buy(T,100,SIG.payload())
        a.boundary(T+3600,70,SIG.payload(),allow_entry=True)
        self.assertFalse(a.active)
        self.assertEqual(len([x for x in a.fills if x['side']=='buy']),1)
        self.assertLess(a.realized,-D(a.fills[0]['risk_budget']))

    def test_trailing_stop_never_lowers_and_no_fresh_cross_means_no_reentry(self):
        a = Account(RULES);a.buy(T,100,SIG.payload())
        old = a.stop
        a.update_stop(dict(SIG.payload(),atr=100),100)
        self.assertEqual(a.stop,old)
        a.sell(T+60,100,'trailing_stop')
        a.boundary(T+3600,100,dict(SIG.payload(),ema_entry=False))
        self.assertFalse(a.active)

    def test_unfundable_reverse_order_is_rejected_without_spending(self):
        a = Account(replace(RULES,notional=D('1000')))
        self.assertFalse(a.buy(T,100,SIG.payload()))
        self.assertEqual(a.cash,D(100));self.assertFalse(a.fills)


class SignalTests(unittest.TestCase):
    def bars(self,n=1000):
        return [Candle(T+i*3600,100+i/100,101+i/100,99+i/100,100+i/100) for i in range(n)]

    def test_future_bars_cannot_change_closed_signals_and_seed_is_sma(self):
        bars=self.bars();past=indicators(bars[:900]);future=indicators(bars)
        self.assertEqual(past,[s for s in future if s.closed_at<=bars[899].time+3600])
        first=indicators(bars[:800])[0]
        self.assertAlmostEqual(first.long,sum(bars[i*4+3].close for i in range(200))/200)
        self.assertEqual(first.scheduled_at,first.closed_at+3600)

    def test_internal_gap_duplicate_invalid_prices_rejected(self):
        bars=self.bars()
        with self.assertRaises(DataGap):indicators(bars[:500]+bars[501:])
        with self.assertRaises(DataGap):indicators(bars[:500]+bars[499:])
        with self.assertRaises(ValueError):Candle(T,100,99,90,100)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'sol-trend-paper.sqlite'
        self.now=T+3590
        self.engine=PaperEngine(self.path,clock=lambda:self.now)
        self.patch=patch('trend_following.paper.indicators',return_value=[SIG])
        self.signal_mock=self.patch.start()

    def tearDown(self):
        self.engine.close();self.patch.stop();self.temp.cleanup()

    def quote(self,bid='100',ask='100.02'):
        return Quote(self.now,self.now,D(bid),D(ask))

    def ready(self):
        bars=[Candle(T+i*3600,100,101,99,100) for i in range(-800,0)]
        self.engine.process(self.quote(),RULES,bars)

    def opened(self):
        self.ready();self.engine.control('start');self.now=T+3600
        self.engine.process(self.quote(),RULES,[Candle(T,100,101,99,100)])
        self.assertTrue(self.engine.status()['position']['active'])

    def test_start_does_not_buy_and_duplicate_tick_does_not_duplicate_entry(self):
        self.ready();self.engine.control('start')
        self.assertFalse(self.engine.status()['position']['active'])
        self.now=T+3600;self.engine.process(self.quote(),RULES,[Candle(T,100,101,99,100)])
        self.engine.process(self.quote(),RULES)
        fills=[x for x in self.engine.status()['events'] if x['kind']=='fill']
        self.assertEqual(len(fills),1)

    def test_pause_keeps_stop_monitoring_and_stop_does_not_rebuy(self):
        self.opened();self.engine.control('pause');self.now+=5
        self.engine.process(self.quote('90','90.02'),RULES,history_complete=False,signal_error='history offline')
        s=self.engine.status()
        self.assertFalse(s['position']['active']);self.assertLess(D(s['realized_pnl']),0)
        self.engine.process(self.quote(),RULES)
        self.assertFalse(self.engine.status()['position']['active'])

    def test_restart_restores_position_and_cost_without_duplicate_buy(self):
        self.opened();before=self.engine.status()
        self.engine.close();self.now+=2
        self.engine=PaperEngine(self.path,clock=lambda:self.now)
        self.engine.process(self.quote(),RULES)
        after=self.engine.status()
        self.assertEqual(after['position'],before['position'])
        self.assertEqual(after['cash'],before['cash'])
        self.assertEqual(len([e for e in after['events'] if e['kind']=='fill']),1)

    def test_stale_market_cannot_execute_or_close(self):
        self.opened();old=self.quote();self.now+=11
        with self.assertRaises(ValueError):self.engine.process(old,RULES)
        with self.assertRaises(ValueError):self.engine.control('close')
        self.assertFalse(self.engine.status()['market_fresh'])
        self.assertTrue(self.engine.status()['position']['active'])

    def test_missed_window_is_skipped_forever(self):
        self.ready();self.engine.control('start');self.now=T+3720
        self.engine.process(self.quote(),RULES,[Candle(T,100,101,99,100)])
        s=self.engine.status()
        self.assertFalse(s['position']['active']);self.assertIn('missed',s['decision'])
        self.engine.process(self.quote(),RULES)
        self.assertFalse(self.engine.status()['position']['active'])

    def test_candle_revision_rolls_back_whole_cycle(self):
        self.ready();before=self.engine.status()
        with self.assertRaises(ValueError):
            self.engine.process(self.quote('101','101.02'),RULES,[Candle(T-3600,100,102,99,100)])
        self.assertEqual(self.engine.status(),before)

    def test_forming_candle_rejected(self):
        with self.assertRaises(ValueError):self.engine.process(self.quote(),RULES,[Candle(T,100,101,99,100)])

    def test_live_trailing_update_excludes_highs_after_signal_close(self):
        self.opened()
        later=replace(SIG,closed_at=T+14400,ema_entry=False)
        self.signal_mock.return_value=[SIG,later]
        self.now=T+18000
        bars=[Candle(T+i*3600,100,102 if i<4 else 999,99,100) for i in range(1,5)]
        self.engine.process(self.quote(),RULES,bars)
        self.assertEqual(D(self.engine.status()['position']['stop']),D(99))

    def test_stale_closed_history_blocks_entries_with_fresh_quotes(self):
        self.ready();self.engine.control('start');self.now=T+3600
        self.engine.process(self.quote(),RULES,history_complete=True)
        s=self.engine.status()
        self.assertFalse(s['position']['active'])
        self.assertEqual(s['status'],'SIGNALS_PAUSED')
        self.assertIn('missing',s['error'])

    def test_other_process_is_fenced_and_existing_policy_cannot_be_reset(self):
        with self.assertRaises(RuntimeError):PaperEngine(self.path,clock=lambda:self.now)
        with self.assertRaises(ValueError):Store(self.path,Policy(capital=D(500)))

    def test_manual_close_realizes_actual_modeled_loss_and_pauses_entries(self):
        self.opened();self.now+=1
        self.engine.control('close')
        s=self.engine.status()
        self.assertFalse(s['entries_enabled']);self.assertFalse(s['position']['active'])
        self.assertLess(D(s['realized_pnl']),0)

    def test_api_requires_auth_origin_and_explicit_close_confirmation(self):
        self.ready()
        cfg=Config(self.path,Path(self.temp.name)/'trend-paper.log','http://localhost:5174','a-private-password','s'*40)
        app=create_app(cfg,engine=self.engine,run_worker=False)
        with TestClient(app,base_url='http://localhost:8010') as c:
            self.assertEqual(c.get('/api/trend/status').status_code,401)
            self.assertEqual(c.post('/api/auth/login',json={'password':cfg.password}).status_code,403)
            headers={'Origin':cfg.origin}
            login=c.post('/api/auth/login',headers=headers,json={'password':cfg.password})
            self.assertEqual(login.status_code,200)
            self.assertIn('HttpOnly',login.headers['set-cookie'])
            self.assertEqual(c.get('/api/trend/status').json()['mode'],'PAPER')
            self.assertEqual(c.post('/api/trend/control',headers={'Origin':'http://localhost:5173'},json={'action':'start'}).status_code,403)
            self.assertEqual(c.post('/api/trend/control',headers=headers,json={'action':'close'}).status_code,422)
            self.assertEqual(c.post('/api/trend/control',headers=headers,json={'action':'close','confirmed':'true'}).status_code,422)
            self.assertEqual(c.post('/api/trend/control',headers=headers,json={'action':'live'}).status_code,422)
            self.assertEqual(c.get('/api/trend/status').headers['cache-control'],'no-store')
            self.assertEqual(c.post('/api/auth/logout',headers=headers).status_code,200)
            self.assertEqual(c.get('/api/trend/status').status_code,401)


class IsolationTests(unittest.TestCase):
    def test_grid_database_is_refused_without_altering_it(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'trend-paper.sqlite'
            with closing(sqlite3.connect(p)) as db,db:db.execute('CREATE TABLE bot_state(key TEXT)')
            before=p.read_bytes()
            with self.assertRaises(ValueError):Store(p,Policy())
            self.assertEqual(p.read_bytes(),before)

    def test_live_mode_or_other_symbol_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config.env'
            for mode,symbol in [('MAINNET','SOLUSDT'),('PAPER','BTCUSDT')]:
                p.write_text(f'TREND_EXECUTION_MODE={mode}\nTREND_SYMBOL={symbol}\n')
                with self.assertRaises(ValueError):Config.load(p)

    def test_public_transport_has_no_private_or_post_path(self):
        calls=[]
        def respond(req):
            calls.append(req)
            self.assertEqual(req.method,'GET')
            self.assertEqual(req.url.host,'data-api.binance.vision')
            self.assertNotIn('x-mbx-apikey',req.headers)
            if req.url.path.endswith('/time'):return httpx.Response(200,json={'serverTime':T*1000})
            return httpx.Response(200,json={'symbol':'SOLUSDT','bidPrice':'100','askPrice':'100.01'})
        feed=PublicFeed(httpx.Client(transport=httpx.MockTransport(respond)),clock=lambda:T)
        self.assertEqual(feed.quote().bid,D(100))
        with self.assertRaises(ValueError):feed.get('/api/v3/order')
        self.assertEqual(len(calls),2)
        feed.close()

    def test_history_reader_never_admits_the_forming_hour(self):
        def respond(req):
            self.assertEqual(req.url.params['interval'],'1h')
            rows=[[T*1000,'100','101','99','100','1',(T+3600)*1000-1],
                  [(T+3600)*1000,'100','102','98','101','1',(T+7200)*1000-1]]
            return httpx.Response(200,json=rows)
        feed=PublicFeed(httpx.Client(transport=httpx.MockTransport(respond)))
        bars,complete=feed.candles(T-3600,T+3700)
        self.assertEqual([b.time for b in bars],[T])
        self.assertTrue(complete)
        feed.close()


if __name__=='__main__':unittest.main()
