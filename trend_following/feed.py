"""Unsigned GETs to Binance's market-data-only host; no trading endpoint."""
from decimal import Decimal as D
from dataclasses import dataclass
import time
import httpx

from .account import Rules
from .strategy import Candle, HOUR

SEED_START = 1630454400  # 2021-09-01 00:00 UTC; matches frozen research seed
HOST = 'https://data-api.binance.vision'
ALLOWED = frozenset(('/api/v3/time','/api/v3/klines','/api/v3/exchangeInfo','/api/v3/ticker/bookTicker'))


@dataclass(frozen=True)
class Quote:
    observed_at: float
    server_time: float
    bid: D
    ask: D

    def __post_init__(self):
        if not self.bid.is_finite() or not self.ask.is_finite() or not 0 < self.bid <= self.ask:
            raise ValueError('Invalid SOL quote')

    @property
    def mid(self):
        return (self.bid+self.ask)/2

    def payload(self):
        return dict(observed_at=self.observed_at,server_time=self.server_time,bid=str(self.bid),ask=str(self.ask),mid=str(self.mid))


def symbol_rules(payload):
    symbol = next(s for s in payload['symbols'] if s['symbol']=='SOLUSDT')
    if symbol['status'] != 'TRADING' or not symbol.get('isSpotTradingAllowed',True):
        raise ValueError('SOLUSDT Spot is not trading')
    filters = {f['filterType']:f for f in symbol['filters']}
    lot, market = filters['LOT_SIZE'], filters.get('MARKET_LOT_SIZE',{})
    steps = [D(f['stepSize']) for f in (lot,market) if D(f.get('stepSize','0'))>0]
    step = max(steps)
    if any(step%s != 0 for s in steps):
        raise ValueError('Incompatible quantity steps')
    price = filters['PRICE_FILTER']
    return Rules(step,max(step,D(lot['minQty']),D(market.get('minQty','0'))),
                 min(D(f['maxQty']) for f in (lot,market) if D(f.get('maxQty','0'))>0),
                 D(filters.get('NOTIONAL',filters.get('MIN_NOTIONAL',{}))['minNotional']),
                 D(price['tickSize']),max(D(price['minPrice']),D(price['tickSize'])))


class PublicFeed:
    def __init__(self, client=None, clock=time.time):
        self.client = client or httpx.Client(timeout=6,follow_redirects=False)
        self.clock = clock

    def get(self, path, params=None):
        if path not in ALLOWED:
            raise ValueError('Only approved public market-data GETs are supported')
        response = self.client.get(HOST+path,params=params)
        response.raise_for_status()
        return response.json()

    def rules(self):
        return symbol_rules(self.get('/api/v3/exchangeInfo',{'symbol':'SOLUSDT'}))

    def quote(self):
        started = self.clock()
        stamp = self.get('/api/v3/time')['serverTime']/1000
        raw = self.get('/api/v3/ticker/bookTicker',{'symbol':'SOLUSDT'})
        received = self.clock()
        if raw['symbol']!='SOLUSDT' or received-started>6 or abs(received-stamp)>6:
            raise ValueError('Quote response or server clock is stale')
        return Quote(received,stamp,D(raw['bidPrice']),D(raw['askPrice']))

    def candles(self, last, server_time):
        """Bound startup work to four pages per cycle; retain the exact seed.

        No padded bars; unexpected gaps are rejected by the signal engine.
        Only already-closed hourly bars enter the persistent candle table.
        """
        start = SEED_START if last is None else last+HOUR
        cutoff = int(server_time)//HOUR*HOUR
        result = []
        for _ in range(4):
            if start >= cutoff:
                break
            rows = self.get('/api/v3/klines',dict(symbol='SOLUSDT',interval='1h',startTime=start*1000,
                                               endTime=cutoff*1000-1,limit=1000))
            if not isinstance(rows,list) or not rows:
                raise ValueError('Closed hourly history is unavailable')
            page = [Candle(int(v[0])//1000,*map(float,v[1:5])) for v in rows if int(v[6]) < cutoff*1000]
            if not page or page[0].time<start or any(b.time<=a.time for a,b in zip(page,page[1:])):
                raise ValueError('Hourly history did not advance')
            result.extend(page)
            start = page[-1].time+HOUR
        return result,start>=cutoff

    def close(self):
        self.client.close()
