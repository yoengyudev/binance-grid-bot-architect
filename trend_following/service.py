"""Local paper API and public-data worker, independent of the grid service."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
from urllib.parse import urlsplit

import jwt
from dotenv import dotenv_values
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, StrictBool

from .account import Policy, decimal
from .feed import PublicFeed
from .paper import PaperEngine

LOGGER = logging.getLogger('trend_paper')
COOKIE = 'trend_paper_session'
ISSUER = 'trend-paper-v1'


@dataclass(frozen=True)
class Config:
    database: Path
    log_path: Path
    origin: str
    password: str = field(repr=False)
    secret: str = field(repr=False)
    policy: Policy = Policy()

    def __post_init__(self):
        url = urlsplit(self.origin)
        if (url.scheme not in ('http','https') or not url.netloc or url.path or url.query or url.fragment or
                url.username or url.password or (url.scheme=='http' and url.hostname not in ('localhost','127.0.0.1'))):
            raise ValueError('Dashboard origin must be HTTPS, or HTTP on localhost')
        if (len(self.password)<12 or len(self.secret)<32 or self.password.startswith('CHANGE_') or self.secret.startswith('CHANGE_')):
            raise ValueError('Configure a password of 12+ characters and a JWT secret of 32+ characters')
        if self.log_path.suffix!='.log' or 'trend' not in self.log_path.stem or 'paper' not in self.log_path.stem:
            raise ValueError('Use a dedicated trend-paper log filename')

    @classmethod
    def load(cls, filename):
        filename = Path(filename).resolve(strict=True)
        env = dict(dotenv_values(filename))
        if env.get('TREND_EXECUTION_MODE')!='PAPER':
            raise ValueError('This service supports PAPER only; live trading is not implemented')
        if env.get('TREND_SYMBOL')!='SOLUSDT':
            raise ValueError('This frozen strategy supports SOLUSDT only')
        def path(key):
            p = Path(env[key])
            return (filename.parent/p).resolve() if not p.is_absolute() else p.resolve()
        return cls(path('TREND_PAPER_DB_PATH'),path('TREND_PAPER_LOG_PATH'),env['TREND_FRONTEND_ORIGIN'].rstrip('/'),
                   env['TREND_ADMIN_PASSWORD'],env['TREND_JWT_SECRET'],Policy(capital=decimal(env['TREND_INITIAL_CAPITAL'])))


class Worker:
    def __init__(self, engine, feed):
        self.engine, self.feed = engine, feed
        self.rules, self.rules_time = None, 0

    def step(self):
        try:
            _,_,_,_,last = self.engine.store.read()
            quote = self.feed.quote()
            signal_error, new, complete = None, [], False
            try:
                new,complete = self.feed.candles(last,quote.server_time)
            except Exception as error:
                # Keep quote-based stop monitoring available during history failures.
                signal_error = 'Hourly data unavailable: '+type(error).__name__
            if self.rules is None or time.time()-self.rules_time>=3600:
                try:
                    self.rules = self.feed.rules()
                    self.rules_time = time.time()
                except Exception as error:
                    signal_error = 'Market rules unavailable: '+type(error).__name__
            # Requests for bootstrap history may take time. Execute only on a
            # newly obtained quote, never the old pre-fetch snapshot.
            if new or signal_error or time.time()-quote.observed_at>2:
                quote = self.feed.quote()
            self.engine.process(quote,self.rules,new,history_complete=complete,signal_error=signal_error)
        except Exception as error:
            LOGGER.error('Paper cycle failed: %s',type(error).__name__)
            self.engine.error('Paper market/processing error: '+type(error).__name__)


class Login(BaseModel):
    password: str = Field(min_length=1,max_length=128)


class Control(BaseModel):
    action: str = Field(pattern='^(start|pause|close)$')
    confirmed: StrictBool = False


def create_app(config, *, engine=None, feed_factory=PublicFeed, run_worker=True):
    """Injected engine/feed keep authentication and execution tests offline."""
    @asynccontextmanager
    async def lifespan(app):
        app.state.engine = engine or PaperEngine(config.database,config.policy)
        stop = asyncio.Event()
        feed = feed_factory() if run_worker else None
        async def loop():
            worker = Worker(app.state.engine,feed)
            while not stop.is_set():
                await asyncio.to_thread(worker.step)
                try:
                    await asyncio.wait_for(stop.wait(),timeout=5)
                except asyncio.TimeoutError:
                    pass
        task = asyncio.create_task(loop()) if run_worker else None
        try:
            yield
        finally:
            stop.set()
            if task:
                await asyncio.wait_for(task,timeout=55)
            if feed:
                feed.close()
            app.state.engine.close()

    app = FastAPI(title='SOL Trend Paper',lifespan=lifespan)
    app.state.engine = engine
    app.add_middleware(CORSMiddleware,allow_origins=[config.origin],allow_credentials=True,
                       allow_methods=['GET','POST'],allow_headers=['Content-Type'])
    failures = {}
    secure = config.origin.startswith('https://')

    @app.middleware('http')
    async def no_cache(request,call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def origin(request: Request):
        if request.headers.get('origin')!=config.origin:
            raise HTTPException(403,'Untrusted dashboard origin')

    def auth(request: Request):
        try:
            claims = jwt.decode(request.cookies.get(COOKIE,''),config.secret,algorithms=['HS256'],
                                issuer=ISSUER,audience='trend-paper-dashboard',
                                options={'require':['sub','iss','aud','iat','nbf','exp']})
            if claims['sub']!='admin':
                raise jwt.InvalidTokenError()
        except jwt.InvalidTokenError:
            raise HTTPException(401,'Login required') from None

    def cookie(response):
        now = datetime.now(timezone.utc)
        token = jwt.encode(dict(sub='admin',iss=ISSUER,aud='trend-paper-dashboard',iat=now,nbf=now,
                                exp=now+timedelta(hours=1)),config.secret,algorithm='HS256')
        response.set_cookie(COOKIE,token,max_age=3600,httponly=True,secure=secure,samesite='strict',path='/api')

    @app.get('/api/health')
    def health():
        return dict(service='trend-paper',mode='PAPER')

    @app.post('/api/auth/login',dependencies=[Depends(origin)])
    def login(payload: Login,request: Request,response: Response):
        address = request.client.host if request.client else 'unknown'
        now = time.monotonic()
        attempts = [t for t in failures.get(address,[]) if now-t<60]
        if len(attempts)>=5:
            raise HTTPException(429,'Too many attempts; wait one minute')
        if not hmac.compare_digest(payload.password.encode(),config.password.encode()):
            failures[address] = attempts+[now]
            raise HTTPException(401,'Invalid password')
        failures.pop(address,None)
        cookie(response)
        return dict(authenticated=True)

    @app.get('/api/auth/me',dependencies=[Depends(auth)])
    def me():
        return dict(authenticated=True)

    @app.post('/api/auth/refresh',dependencies=[Depends(origin),Depends(auth)])
    def refresh(response: Response):
        cookie(response)
        return dict(authenticated=True)

    @app.post('/api/auth/logout',dependencies=[Depends(origin)])
    def logout(response: Response):
        response.delete_cookie(COOKIE,path='/api',secure=secure,httponly=True,samesite='strict')
        return dict(authenticated=False)

    @app.get('/api/trend/status',dependencies=[Depends(auth)])
    def status():
        return app.state.engine.status()

    @app.post('/api/trend/control',dependencies=[Depends(origin),Depends(auth)])
    def control(payload: Control):
        if payload.action=='close' and not payload.confirmed:
            raise HTTPException(422,'Explicit paper exit confirmation is required')
        try:
            return app.state.engine.control(payload.action)
        except ValueError as error:
            raise HTTPException(409,str(error)) from None

    return app


def configure_logging(config):
    config.log_path.parent.mkdir(parents=True,exist_ok=True)
    handler = RotatingFileHandler(config.log_path,maxBytes=2_000_000,backupCount=4,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
