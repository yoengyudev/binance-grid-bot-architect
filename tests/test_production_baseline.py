"""Production/research geometry parity, closed ATR and Mainnet lock boundaries."""
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd
import engine
import main
from backtest_grid import Config, atr_bounds, closed_atr
from database import GridDatabase
from models import GridConfig
from production_baseline import (BASELINE_ID, HOUR_MS, compressed_bounds,
                                 closed_hourly_atr, validate_profile)
from tests.fakes import OfflineGridBot, FakeSpotExchange
from mainnet_preflight import commission_limits

NOW = 1792368000000//HOUR_MS*HOUR_MS


def candles():
    return [[NOW-i*HOUR_MS,100,101,99,100,1] for i in range(15,0,-1)]


class ProductionBaselineTests(unittest.TestCase):
    def test_bounds_match_saved_research_definition_exactly(self):
        for center in (D(100),D('62772.01'),D('110000.99')):
            for atr in (center*D('.001'),center*D('.01'),center*D('.03')):
                for tick in (D('.01'),D('.05')):
                    target = max(D('.5')*atr/center,D('.004'))
                    expected = atr_bounds(center,atr,Config(levels=5,atr_multiple=tick/atr,
                        min_grid_return=target,price_tick=tick))
                    self.assertEqual(compressed_bounds(center,atr,tick=tick),expected[:2])

    def test_closed_atr_rejects_stale_gaps_and_ignores_forming_hour(self):
        data = candles()
        forming = [NOW,100,90000,1,100,1]
        atr,close,available = closed_hourly_atr(data+[forming],NOW+10000)
        self.assertEqual((atr,close,available),(D(2),D(100),NOW))
        for bad in (data[:-1],data[1:],data+[data[-1]],data[:-1]+[[NOW-HOUR_MS,100,99,101,100,1]]):
            with self.assertRaises(ValueError):
                closed_hourly_atr(bad,NOW)
        varied = [[NOW-(15-i)*HOUR_MS,100+i,103+i,98+i,101+i,1] for i in range(15)]
        frame = pd.DataFrame(varied,columns=['timestamp','open','high','low','close','volume'])
        self.assertAlmostEqual(float(closed_hourly_atr(varied,NOW)[0]),closed_atr(frame).iloc[-1],places=11)

    def test_locked_profile_cannot_enable_rsi_or_change_count_or_seed(self):
        raw = {'strategy_baseline':BASELINE_ID,'grid':{'symbol':'BTC/USDT',
            'grid_levels':5,'initial_inventory_percent':50,'auto_center_percent':None}}
        self.assertTrue(validate_profile(raw,required=True))
        with self.assertRaises(ValueError):
            validate_profile({'grid':{}},required=True)
        for field,value in (('rsi',45),('atr_multiple',2),('grid_levels',8),('initial_inventory_percent',80)):
            bad = json.loads(json.dumps(raw));bad['grid'][field]=value
            with self.assertRaises(ValueError):
                validate_profile(bad,required=True)

    def test_mainnet_loader_requires_lock_and_config_sets_exact_counts(self):
        raw = {'exchange':{'name':'binance','market_type':'spot'},'grid':{
            'symbol':'BTC/USDT','investment_quote':500,'lower_price':1,'upper_price':3,
            'spacing_percent':.4,'initial_inventory_percent':50,'grid_levels':5,
            'stop_loss_price':.85,'poll_seconds':10,'auto_center_percent':None}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'config.json'
            with patch('models.get_trading_settings',return_value=SimpleNamespace(sandbox=False)):
                path.write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError,'strategy_baseline'):
                    GridConfig.load(path)
                raw['strategy_baseline']=BASELINE_ID;path.write_text(json.dumps(raw))
                config = GridConfig.load(path)
                self.assertEqual((config.buy_grid_levels,config.sell_grid_levels,config.strategy_baseline),(3,2,BASELINE_ID))

    def test_reviewed_proposal_rejects_drift_and_wrong_count_before_orders(self):
        with tempfile.TemporaryDirectory() as temp:
            exchange = FakeSpotExchange()
            config = GridConfig('BTC/USDT',D(500),D(80),D(120),D('.4'),D(50),D(68),10,
                                None,3,2,BASELINE_ID)
            bot = OfflineGridBot(config,exchange,GridDatabase(Path(temp)/'grid_testnet.sqlite'))
            exchange.fetch_ohlcv = Mock(return_value=candles())
            lower,upper = compressed_bounds(D(100),D(2))
            width = (upper-100)/100*100
            with patch.object(engine.time,'time',return_value=NOW/1000):
                bot._assert_locked_proposal(D(100),width,5)
                for changed,count in ((width+1,5),(width,8)):
                    with self.assertRaises(ValueError):
                        bot._assert_locked_proposal(D(100),changed,count)
            self.assertEqual(exchange.orders,{})
            with self.assertRaises(ValueError):
                bot.request_grid_reset('80','120')

    def test_direct_mainnet_cli_cannot_trade_without_review(self):
        with patch('main.TRADING_SETTINGS',SimpleNamespace(sandbox=False)), \
             patch('sys.argv',['main.py','--execute']),patch('main.create_exchange') as client:
            self.assertEqual(main.main(),2)
            client.assert_not_called()

    def test_commissions_include_tax_special_taker_and_bnb_behavior(self):
        rates = lambda maker='0',taker='0': dict(maker=maker,taker=taker,buyer='0',seller='0')
        response = dict(standardCommission=rates('.001','.001'),specialCommission=rates(),
                        taxCommission=rates(),discount={})
        self.assertEqual(D(commission_limits(response)['minimum_net_cycle_return']),D('.001993004'))
        for group,key in (('standardCommission','taker'),('taxCommission','maker'),('specialCommission','buyer')):
            bad = json.loads(json.dumps(response));bad[group][key]='.002'
            with self.assertRaises(ValueError):
                commission_limits(bad)
        response['discount']={'enabledForAccount':True,'enabledForSymbol':True}
        with self.assertRaisesRegex(ValueError,'BNB'):
            commission_limits(response)

    def test_saving_locked_grid_is_idle_and_does_not_submit_seed(self):
        with tempfile.TemporaryDirectory() as temp:
            exchange = FakeSpotExchange()
            exchange.base_free=D(0)
            config=GridConfig('BTC/USDT',D(500),D(80),D(120),D('.4'),D(50),D(68),10,
                              None,3,2,BASELINE_ID)
            bot=OfflineGridBot(config,exchange,GridDatabase(Path(temp)/'grid_testnet.sqlite'))
            bot.ready_mode=True
            exchange.fetch_ohlcv=Mock(return_value=candles())
            lower,upper=compressed_bounds(D(100),D(2))
            with patch.object(engine.time,'time',return_value=NOW/1000):
                bot.request_initial_grid('100',str(upper-100),'20','500',5)
            self.assertEqual(exchange.orders,{})
            self.assertEqual(bot.database.get_state('engine_status'),'IDLE')
            self.assertEqual(bot.database.get_state('total_allocation_cap'),'1')


if __name__=='__main__':
    unittest.main()
