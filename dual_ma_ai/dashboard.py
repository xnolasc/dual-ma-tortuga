"""Local PAPER dashboard: funding authority only, never strategy execution."""
import argparse
import hashlib
import hmac
import json
import logging
import re
import secrets
import signal
import sqlite3
import socket
import time
import threading
import multiprocessing
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .wallet import Wallet, WalletError, TICKERS, cents, usd
from .market_data import HistoryCache, DataError, USCalendar, strict_json
from .strategy import PRESETS, signals_many
from .alpaca_data import AlpacaCache, CACHE_ROOT as ALPACA_ROOT, SOURCE as ALPACA_SOURCE, daily_ready_at
from .research import ROOT, CACHE_ROOT, REPORT_ROOT
from .monitor import status as monitor_status, STATE_ROOT as MONITOR_ROOT
from .alert_store import AlertStore

LOG = logging.getLogger('dual_ma_ai.dashboard')
RESEARCH_REQUEST_TIMEOUT_SECONDS = 20
MAX_RETAINED_RESEARCH_JOBS = 16
MAX_ACTIVE_RESEARCH_JOBS = 4
MAX_MEMO_FLIGHTS = 16
MAX_MEMO_WAITERS = 32


class DashboardBusy(DataError):
    """Bounded read-only research work is busy; retry after the response."""


def _timed_compute(fn, args):
    started = time.monotonic_ns()
    try:
        value = fn(*args)
        return True, value, started, time.monotonic_ns(), None
    except Exception as exc:
        # Pass only safe exception metadata back from the child process.
        return False, None, started, time.monotonic_ns(), type(exc).__name__


def _initialize_research_worker():
    # Terminal Ctrl-C belongs to the dashboard process. A spawned worker must
    # finish/close its current bounded job so the parent can shut down cleanly.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

STATIC = Path(__file__).with_name('static')
DEFAULT_DB = ROOT / 'data/dual_ma_ai/wallet.sqlite3'
DEFAULT_ALERT_DB = ROOT / 'data/dual_ma_ai/alerts/alerts.sqlite3'


def wallet_view(wallet):
    # One read transaction includes balances, marks AND the audit trail.
    with wallet._connection() as db:
        db.execute('BEGIN')
        wallet._check(db)
        w = db.execute('SELECT * FROM wallet WHERE id=1').fetchone()
        positions = [dict(x) for x in db.execute('SELECT * FROM positions ORDER BY ticker')]
        events = [dict(x) for x in db.execute('SELECT * FROM events ORDER BY id DESC')]
        for event in events:
            event['payload'] = json.loads(event['payload'])
        market = sum(p['quantity'] * p['mark'] for p in positions)
        unrealized = market - sum(p['cost'] for p in positions)
        added = sum(e['payload']['amount'] * (1 if e['kind'] == 'deposit' else -1)
                    for e in events if e['kind'] in ('deposit', 'withdraw'))
        for p in positions:
            p['cost_basis'] = usd(p.pop('cost'))
            p['market_value'] = usd(p['quantity'] * p['mark'])
            p['mark'] = usd(p['mark'])
            p['mark_source'] = 'Last explicit paper fill / manual mark; not a live quote'
            p['marked_at'] = next(e['created_at'] for e in events
                                  if e['payload'].get('ticker') == p['ticker'])
        return dict(mode='PAPER', currency='USD', equity=usd(w['cash'] + market),
                    cash=usd(w['cash']), net_contributions=usd(w['contributions']),
                    net_deposits=usd(added), realized_pnl=usd(w['realized']),
                    unrealized_pnl=usd(unrealized), trading_pnl=usd(w['realized'] + unrealized),
                    positions=positions, events=events, revision=events[0]['id'],
                    as_of=datetime.now(timezone.utc).isoformat())


def data_kind(report):
    sources = [p.get('source', '') for c in report.get('comparisons', [])
               for r in c.get('runs', []) for p in r.get('provenance', {}).values()]
    if report.get('synthetic') or any('SYNTHETIC' in s.upper() for s in sources):
        return 'SYNTHETIC'
    provenance = [p for c in report.get('comparisons', []) for r in c.get('runs', []) for p in r.get('provenance', {}).values()]
    if provenance and all(p.get('source') == ALPACA_SOURCE and p.get('feed') in ('iex', 'sip') and p.get('validation_status') == 'VALIDATED' for p in provenance):
        return 'REAL'
    if sources and all(s == 'Alpha Vantage TIME_SERIES_DAILY_ADJUSTED' for s in sources):
        return 'HISTORICAL'
    return 'UNAVAILABLE / UNVERIFIED'


def _read_report(reports, identity):
    if not re.fullmatch('[a-f0-9]{64}', identity):
        raise WalletError('Invalid report identifier')
    path=Path(reports)/(identity+'.json')
    if path.is_symlink() or path.stat().st_size>25_000_000:
        raise WalletError('Report is unsafe or too large')
    report=strict_json(path.read_bytes())
    if report.get('mode')!='PAPER_BACKTEST_COMPARISON':
        raise WalletError('Unsupported report')
    from .market_data import digest
    if digest(report)!=identity:
        raise WalletError('Report content hash mismatch')
    report['data_kind']=data_kind(report)
    return report


def _catalog_from_path(reports):
    started=time.monotonic_ns();rows=[];warnings=[]
    paths=sorted(Path(reports).glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    for path in paths:
        try:
            report=_read_report(reports,path.stem)
            rows.append(dict(id=path.stem,data_kind=report['data_kind'],end=report.get('evaluation_end'),
                             horizons=[c['years'] for c in report['comparisons']]))
        except (ValueError,OSError,KeyError,TypeError) as exc:
            warnings.append(f'{path.name}: rejected ({type(exc).__name__})')
    LOG.info('stage=reports.catalog files=%d accepted=%d rejected=%d processing_ms=%.3f',
             len(paths),len(rows),len(warnings),(time.monotonic_ns()-started)/1_000_000)
    return dict(reports=rows,warnings=warnings)


_PROCESS_ALPACA_CACHES={}
_PROCESS_ALPACA_LOCK=threading.Lock()


def _process_alpaca_cache(path):
    key=str(Path(path).resolve())
    with _PROCESS_ALPACA_LOCK:
        return _PROCESS_ALPACA_CACHES.setdefault(key,AlpacaCache(key))


def _signal_panel_from_paths(cache_path,alpaca_path,provider,feed):
    return _signal_panel_compute(HistoryCache(cache_path),_process_alpaca_cache(alpaca_path),provider,feed)


def _signal_panel_compute(history,alpaca,provider,feed):
    signal_started=time.monotonic_ns();data_validation_ns=calendar_ns=strategy_ns=0
    if provider not in ('alpaca','alpha-vantage') or feed not in ('iex','sip'):
        raise DataError('Invalid explicit provider/feed')
    now=datetime.now(timezone.utc);rows=[]
    calendar_started=time.monotonic_ns()
    calendar=USCalendar(date(2000,1,1),now.date()+timedelta(days=7))
    expected=[d for d in calendar.sessions(now.date()-timedelta(days=10),now.date())
              if (daily_ready_at(d) if provider=='alpaca' else calendar.bounds(d)[1])<=now]
    calendar_ns+=time.monotonic_ns()-calendar_started
    for ticker in TICKERS:
        try:
            stage_started=time.monotonic_ns()
            if provider=='alpaca':(ds,validation),_=alpaca.latest(ticker,feed,calendar=calendar)
            else:ds,_=history.latest(ticker,calendar);validation={}
            data_validation_ns+=time.monotonic_ns()-stage_started
            completed=[b for b in ds.bars if b.usable_at<=now]
            if not completed:raise DataError('No completed candles')
            last=completed[-1]
            row=dict(ticker=ticker,status='REAL' if provider=='alpaca' else 'HISTORICAL',source=ds.source,
                     feed=ds.feed,validation_status=ds.validation_status or 'VALIDATED',
                     coverage_start=str(completed[0].day),coverage_end=str(last.day),warnings=list(ds.warnings),
                     fetched_at=ds.fetched_at.isoformat(),policy=ds.adjustment_policy,
                     latest_completed_candle=last.closed_at.isoformat(),latest_usable_at=last.usable_at.isoformat(),
                     stale=bool(expected and last.day<expected[-1]),strategies=[])
            stage_started=time.monotonic_ns()
            crosses_by_strategy=signals_many(ds,PRESETS,as_of=now)
            strategy_ns+=time.monotonic_ns()-stage_started
            for strategy in PRESETS:
                crosses=crosses_by_strategy[strategy.name]
                latest=crosses[-1] if crosses else None
                row['strategies'].append(dict(name=strategy.name,
                    signal=latest.side if latest and latest.day==last.day else 'NO CROSS',
                    last_crossover=dict(side=latest.side,day=str(latest.day)) if latest else None,
                    sufficient_warmup=len(completed)>strategy.long))
            rows.append(row)
        except (DataError,OSError,ValueError,ImportError) as exc:
            rows.append(dict(ticker=ticker,status='UNAVAILABLE',warning=str(exc),strategies=[]))
    LOG.info('stage=signals.panel provider=%s feed=%s data_validation_ms=%.3f calendar_ms=%.3f strategies_ms=%.3f total_ms=%.3f',
             provider,feed,data_validation_ns/1_000_000,calendar_ns/1_000_000,strategy_ns/1_000_000,
             (time.monotonic_ns()-signal_started)/1_000_000)
    return dict(as_of=now.isoformat(),provider=provider,feed=feed if provider=='alpaca' else None,
                execution_enabled=False,rows=rows,
                warning='Read-only research signals. Stale candles are not actionable. No order endpoint exists.')


def _coverage_from_path(alpaca_path,feed):
    return _process_alpaca_cache(alpaca_path).coverage(feed)


class Dashboard:
    def __init__(self, wallet, reports=REPORT_ROOT, cache=CACHE_ROOT, alpaca_cache=ALPACA_ROOT, monitor_root=MONITOR_ROOT, read_only=False, alert_store=None):
        self.wallet, self.reports, self.cache = wallet, Path(reports), HistoryCache(cache)
        self.alpaca = AlpacaCache(alpaca_cache)
        self.monitor_root=Path(monitor_root)
        self.read_only=read_only
        self.alert_store=alert_store
        self.secret = secrets.token_bytes(32)
        self.csrf = secrets.token_urlsafe(32)
        self._memo_guard = threading.Lock()
        # Same immutable research key shares one bounded Future. The owner
        # computes once; duplicate HTTP requests wait only within the standard
        # research deadline and receive the same result or safe exception.
        self._memo_flights = {}
        self._memo = {}
        self._compute_pool = None
        self._compute_pool_lock = threading.Lock()
        # Two workers plus a queue of two independent calculations. This admits
        # the dashboard's four initial research reads, while capping executor
        # backlog; duplicate immutable requests share a bounded in-flight job.
        self._compute_slots = threading.BoundedSemaphore(MAX_ACTIVE_RESEARCH_JOBS)
        self._compute_jobs = {}
        self._compute_samples = deque(maxlen=128)
        self._server_metrics = {'accepted': 0, 'rejected': 0,
                                'request_samples':deque(maxlen=1024)}
        self._server_metrics_lock = threading.Lock()

    def _isolated(self,fn,*args,stage=None):
        # Report parsing/hash checks, coverage validation, and strategy work run
        # in bounded OS processes so their CPU work cannot starve health reads
        # through Python's process-wide interpreter lock.
        key=(fn.__name__,args)
        with self._compute_pool_lock:
            if self._compute_pool is None:
                self._compute_pool=ProcessPoolExecutor(max_workers=2,
                    mp_context=multiprocessing.get_context('spawn'),
                    initializer=_initialize_research_worker)
            pool=self._compute_pool
            job=self._compute_jobs.get(key)
            future=job[0] if job is not None else None
            # Completed jobs which no timed-out caller consumed are safe to
            # discard. Bound retained Future objects across arbitrary report IDs.
            for old_key,(old_future,_) in list(self._compute_jobs.items()):
                if old_key != key and old_future.done():self._compute_jobs.pop(old_key,None)
            if future is None:
                if len(self._compute_jobs)>=MAX_RETAINED_RESEARCH_JOBS:
                    raise DashboardBusy('Research result capacity is full; retry shortly')
                if not self._compute_slots.acquire(blocking=False):
                    raise DashboardBusy('Research capacity is full; retry shortly')
                submitted=time.monotonic_ns()
                try:
                    future=pool.submit(_timed_compute,fn,args)
                except Exception:
                    self._compute_slots.release()
                    raise
                self._compute_jobs[key]=(future,submitted)
                future.add_done_callback(lambda _f:self._compute_slots.release())
            else:
                submitted=job[1]
        try:
            ok,value,child_started,child_finished,error_type=future.result(timeout=RESEARCH_REQUEST_TIMEOUT_SECONDS)
        except FutureTimeout:
            timeout_ms=RESEARCH_REQUEST_TIMEOUT_SECONDS*1000
            LOG.warning('stage=%s result=timeout timeout_ms=%d',stage or fn.__name__,timeout_ms)
            raise DashboardBusy(f'Research calculation exceeded its {RESEARCH_REQUEST_TIMEOUT_SECONDS} second request budget; retry shortly')
        except Exception as exc:
            LOG.warning('stage=%s result=pool_error exception=%s',stage or fn.__name__,type(exc).__name__)
            with self._compute_pool_lock:
                if self._compute_jobs.get(key,(None,None))[0] is future:
                    self._compute_jobs.pop(key,None)
            raise DashboardBusy('Research worker is unavailable; retry shortly')
        finally:
            if future.done():
                with self._compute_pool_lock:
                    if self._compute_jobs.get(key,(None,None))[0] is future:
                        self._compute_jobs.pop(key,None)
        pool_wait=max(0,(child_started-submitted)/1_000_000)
        compute=max(0,(child_finished-child_started)/1_000_000)
        LOG.info('stage=%s process_pool_wait_ms=%.3f compute_ms=%.3f',stage or fn.__name__,pool_wait,compute)
        with self._compute_pool_lock:
            self._compute_samples.append(dict(stage=stage or fn.__name__,pool_wait_ms=pool_wait,compute_ms=compute))
        if not ok:
            LOG.warning('stage=%s result=failed exception=%s',stage or fn.__name__,error_type)
            raise DataError('Research calculation failed safely')
        return value

    def close(self):
        with self._compute_pool_lock:pool=self._compute_pool;self._compute_pool=None
        if pool is not None:pool.shutdown(wait=True,cancel_futures=True)

    @staticmethod
    def _fingerprint(paths):
        rows=[]
        for path in sorted(paths, key=lambda p: p.name):
            try:
                stat=path.stat()
                rows.append((path.name,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino))
            except OSError:
                rows.append((path.name,None,None,None,None))
        return tuple(rows)

    def _memoized(self, name, identity, fingerprint, producer, ttl=30, fingerprint_reader=None):
        key=(name,identity)
        fingerprint=fingerprint_reader() if fingerprint_reader else fingerprint
        flight_key=(key,fingerprint)
        owner=False
        with self._memo_guard:
            cached=self._memo.get(key)
            if cached and cached[0]==fingerprint and time.monotonic()<cached[1]:
                return cached[2]
            flight=self._memo_flights.get(flight_key)
            if flight is None:
                if len(self._memo_flights)>=MAX_MEMO_FLIGHTS:
                    raise DashboardBusy('Research in-flight capacity is full; retry shortly')
                flight={'future':Future(),'waiters':0}
                self._memo_flights[flight_key]=flight
                owner=True
            elif flight['waiters']>=MAX_MEMO_WAITERS:
                raise DashboardBusy('Research duplicate-request capacity is full; retry shortly')
            else:
                flight['waiters']+=1

        if not owner:
            try:
                completed_fingerprint,value=flight['future'].result(timeout=RESEARCH_REQUEST_TIMEOUT_SECONDS)
            except FutureTimeout:
                LOG.warning('stage=%s result=shared_timeout timeout_ms=%d',name,
                            RESEARCH_REQUEST_TIMEOUT_SECONDS*1000)
                raise DashboardBusy(f'Research calculation exceeded its {RESEARCH_REQUEST_TIMEOUT_SECONDS} second request budget; retry shortly')
            current=fingerprint_reader() if fingerprint_reader else fingerprint
            if current!=completed_fingerprint:
                raise DataError('Research inputs changed during calculation; retry')
            return value

        try:
            started=time.monotonic_ns()
            for _ in range(2):
                value=producer()
                current=fingerprint_reader() if fingerprint_reader else fingerprint
                if current==fingerprint:break
                fingerprint=current
            else:raise DataError('Research inputs changed during calculation; retry')
            elapsed=(time.monotonic_ns()-started)/1_000_000
            with self._memo_guard:
                self._memo[key]=(fingerprint,time.monotonic()+ttl,value)
            flight['future'].set_result((fingerprint,value))
            LOG.info('stage=%s.refresh duration_ms=%.3f',name,elapsed)
            return value
        except BaseException as exc:
            if not flight['future'].done():
                flight['future'].set_exception(exc)
            raise
        finally:
            with self._memo_guard:
                if self._memo_flights.get(flight_key) is flight:
                    self._memo_flights.pop(flight_key,None)

    def cached_catalog(self):
        reader=lambda:self._fingerprint(self.reports.glob('*.json'))
        return self._memoized('reports.catalog','all',reader(),self._cached_catalog_compute,
                              fingerprint_reader=reader)

    def cached_coverage(self, feed):
        reader=lambda:self._fingerprint(self.alpaca.root.glob(f'*-{feed}-*.json'))
        fingerprint=reader()
        return self._memoized('alpaca.coverage',feed,fingerprint,
                              lambda:self._isolated(_coverage_from_path,str(self.alpaca.root),feed,stage='alpaca.coverage'),
                              fingerprint_reader=reader)

    def cached_signal_panel(self, provider='alpaca', feed='iex'):
        if provider=='alpaca':
            reader=lambda:self._fingerprint(p for ticker in TICKERS for p in self.alpaca.root.glob(f'{ticker}-{feed}-*.json'))
        else:
            reader=lambda:self._fingerprint(self.cache.root.glob('*.json'))
        fingerprint=reader()
        return self._memoized('signals.panel',(provider,feed),fingerprint,
                              lambda:(self.__dict__['signal_panel'](provider,feed)
                                      if 'signal_panel' in self.__dict__ else
                                      self._isolated(_signal_panel_from_paths,str(self.cache.root),str(self.alpaca.root),provider,feed,stage='signals.panel')),
                              ttl=15,fingerprint_reader=reader)

    def _cached_catalog_compute(self):
        if 'catalog' in self.__dict__:return self.__dict__['catalog']()
        return self._isolated(_catalog_from_path,str(self.reports),stage='reports.catalog')

    def report(self, identity):
        return self._isolated(_read_report,str(self.reports),identity,stage='reports.detail')

    def catalog(self):
        return _catalog_from_path(self.reports)

    def signal_panel(self, provider="alpha-vantage", feed="iex"):
        return _signal_panel_compute(self.cache,self.alpaca,provider,feed)

    def funding_fields(self, body):
        if not isinstance(body, dict) or body.get('operation') not in ('deposit', 'withdraw'):
            raise WalletError('Choose deposit or withdraw')
        amount, key = body.get('amount'), body.get('key')
        if not isinstance(amount, str) or not re.fullmatch(r'[0-9]{1,13}(?:\.[0-9]{1,2})?', amount):
            raise WalletError('Use a positive USD amount with at most two decimal places')
        amount = usd(cents(amount))
        if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,100}', key):
            raise WalletError('Invalid idempotency key')
        return dict(operation=body['operation'], amount=amount, key=key)

    def prepare(self, body):
        fields = self.funding_fields(body)
        expires = int(time.time()) + 300
        payload = json.dumps(dict(**fields, expires=expires), sort_keys=True).encode()
        signature = hmac.new(self.secret, payload, hashlib.sha256).hexdigest()
        return dict(**fields, expires=expires, confirmation=signature,
                    cash=wallet_view(self.wallet)['cash'])

    def confirm(self, body):
        fields = self.funding_fields(body)
        expires = body.get('expires')
        if type(expires) is not int or expires < time.time() or expires > time.time() + 301:
            raise WalletError('Confirmation expired; review the operation again')
        payload = json.dumps(dict(**fields, expires=expires), sort_keys=True).encode()
        expected = hmac.new(self.secret, payload, hashlib.sha256).hexdigest()
        if body.get('confirmed') is not True or not isinstance(body.get('confirmation'), str) or not hmac.compare_digest(expected, body['confirmation']):
            raise WalletError('Explicit valid confirmation required')
        event = getattr(self.wallet, fields['operation'])(fields['amount'], fields['key'])
        return dict(event=event, wallet=wallet_view(self.wallet))

    def alerts_view(self, query):
        if self.alert_store is None:
            raise DashboardBusy('Phase 5B alerts are not initialized')
        allowed={'ticker','strategy','state','actionable','limit'}
        if set(query)-allowed or any(len(v)!=1 for v in query.values()):
            raise DataError('Invalid alert filters')
        ticker=query.get('ticker',[None])[0]
        strategy=query.get('strategy',[None])[0]
        state=query.get('state',[None])[0]
        actionable=query.get('actionable',[None])[0]
        if ticker is not None and ticker not in TICKERS:
            raise DataError('Unknown alert ticker')
        if strategy is not None and strategy not in {s.name for s in PRESETS}:
            raise DataError('Unknown alert strategy')
        if state is not None and state not in ('ENTRY_REVIEW','EXIT_REVIEW','HOLD_REVIEW','NO_ACTION'):
            raise DataError('Unknown advisory state')
        if actionable not in (None,'true','false'):
            raise DataError('Invalid actionable filter')
        try:limit=int(query.get('limit',['100'])[0])
        except (TypeError,ValueError):raise DataError('Invalid alert limit') from None
        rows=self.alert_store.list_signals(ticker=ticker,strategy=strategy,
            decision_state=state,actionable=None if actionable is None else actionable=='true',limit=limit)
        if actionable != 'true' and state in (None,'NO_ACTION'):
            existing={row['signal_id'] for row in rows}
            for item in self.alert_store.list_suppressions(ticker=ticker,strategy=strategy,limit=limit):
                if item['signal_id'] in existing:
                    continue
                rows.append(dict(signal_id=item['signal_id'],observation_id=item['observation_id'],
                    ticker=item['ticker'],strategy=item['strategy'],timeframe=item['timeframe'],
                    technical_side='SUPPRESSED',decision_state='NO_ACTION',effective_decision_state='NO_ACTION',
                    lifecycle_state='SUPPRESSED',validation_state='SUPPRESSED',
                    validation_reason=item['reason'],source='Phase 5A monitor validation',feed='iex',
                    candle_date=item['candle_date'] or 'Unavailable',expires_at=None,
                    expiry_policy_id='No review expiry; suppressed input',actionable=0,effective_actionable=False,
                    effective_suppression_reason=item['reason'],created_at=item['observed_at']))
            rows.sort(key=lambda row:(row.get('created_at',''),row.get('signal_id','')),reverse=True)
            rows=rows[:limit]
        now=datetime.now(timezone.utc)
        try:
            monitor=monitor_status(self.monitor_root,now)
        except Exception:
            monitor={'status':'ERROR','in_progress':True,'daily':[],'schedule':{}}
        expected=(monitor.get('schedule') or {}).get('expected_candle')
        by_ticker={r.get('ticker'):r for r in monitor.get('daily',[]) if isinstance(r,dict)}
        for row in rows:
            if row.get('lifecycle_state')=='SUPPRESSED':
                row['effective_actionable']=False
                row['effective_decision_state']='NO_ACTION'
                row['effective_suppression_reason']=row.get('validation_reason')
                continue
            source=by_ticker.get(row['ticker'],{})
            source_signal=next((s for s in source.get('signals',[]) if s.get('strategy')==row['strategy']),{})
            eligible=(monitor.get('status') in ('RUNNING','DEGRADED') and not monitor.get('in_progress')
                and source.get('status')=='VALIDATED' and source.get('candle')==expected==row['candle_date']
                and source_signal.get('signal')==row['technical_side'].replace('NO_CROSS','NO CROSS'))
            row['effective_actionable']=bool(row['actionable'] and eligible and row.get('expires_at') and now.isoformat()<row['expires_at'])
            if not row['effective_actionable'] and row['actionable']:
                row['effective_decision_state']='NO_ACTION'
                row['effective_suppression_reason']='Current monitor data is stale, blocked, incomplete, or no longer matches this alert.'
            else:
                row['effective_decision_state']=row['decision_state']
                row['effective_suppression_reason']=row.get('validation_reason')
        return dict(alerts=rows,as_of=now.isoformat(),read_only=True,
                    message='Advisory review states only. No order or wallet operation is available.')

    def acknowledge_alert(self, body):
        if self.alert_store is None:
            raise DashboardBusy('Phase 5B alerts are not initialized')
        if not isinstance(body,dict) or set(body)!={'signal_id','choice','key','actor'}:
            raise DataError('Invalid acknowledgment request')
        if not isinstance(body['signal_id'],str) or not re.fullmatch(r'[a-f0-9]{64}',body['signal_id']):
            raise DataError('Invalid alert identity')
        current=self.alert_store.list_signals(limit=200)
        signal=next((r for r in current if r['signal_id']==body['signal_id']),None)
        if signal is None:
            raise DataError('Alert was not found')
        view=self.alerts_view({'ticker':[signal['ticker']], 'strategy':[signal['strategy']], 'limit':['200']})
        row=next((r for r in view['alerts'] if r['signal_id']==signal['signal_id']),None)
        if not row or not row['effective_actionable']:
            raise DataError('Current market data no longer permits acknowledgment')
        return self.alert_store.acknowledge(body['signal_id'],choice=body['choice'],
            key=body['key'],actor=body['actor'],now=datetime.now(timezone.utc))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Funding request bodies, tokens and keys never enter access logs.

    def send(self, status, value, content_type='application/json'):
        metrics=getattr(self,'_request_metrics',None)
        started=time.monotonic_ns()
        body = json.dumps(value, allow_nan=False).encode() if content_type == 'application/json' else value
        if metrics is not None:
            metrics['serialization_ms']+=(time.monotonic_ns()-started)/1_000_000
            metrics['status']=status
        try:
            started=time.monotonic_ns()
            connection=getattr(self,'connection',None)
            if connection is not None:connection.settimeout(5.0)
            self.send_response(status)
            for k, v in {'Content-Type': content_type, 'Content-Length': str(len(body)),
                         'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                         'Referrer-Policy': 'no-referrer',
                         'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            # Bound stalled localhost clients and absorb browsers canceling slow responses.
            if metrics is not None:metrics['client_disconnected']=True
        finally:
            if metrics is not None:
                metrics['socket_write_ms']+=(time.monotonic_ns()-started)/1_000_000
            try:
                connection=getattr(self,'connection',None)
                if connection is not None:connection.settimeout(None)
            except OSError:pass

    def trusted_host(self):
        port = self.server.server_port
        return self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}')

    def do_GET(self):
        started=time.monotonic_ns()
        accepted,thread_started=getattr(self.server,'request_clock',lambda _s:(started,started))(self.connection)
        try:
            client_started=int(self.headers.get('X-Client-Start-Mono-Ns',''))
            client_to_accept=max(0,(accepted-client_started)/1_000_000)
        except (TypeError,ValueError):client_to_accept=None
        raw_path=urlsplit(self.path).path
        known={'/':'/','/api/session':'/api/session','/api/monitor':'/api/monitor',
               '/api/wallet':'/api/wallet','/api/reports':'/api/reports',
               '/api/signals':'/api/signals','/api/coverage':'/api/coverage',
               '/api/alerts':'/api/alerts'}
        route=known.get(raw_path,'/api/reports/{id}' if raw_path.startswith('/api/reports/') else '/static-or-other')
        self._request_metrics={'route':route,'status':None,'serialization_ms':0.0,
                               'socket_write_ms':0.0,'client_disconnected':False,
                               'accept_to_handler_ms':max(0,(started-accepted)/1_000_000),
                               'thread_dispatch_ms':max(0,(thread_started-accepted)/1_000_000),
                               'client_to_accept_ms':client_to_accept,
                               'request_id':re.sub('[^a-zA-Z0-9-]','',self.headers.get('X-Request-ID',''))[:64]}
        try:self._dispatch_get()
        finally:self._record_request_timing('GET',route,started)

    def _record_request_timing(self, method, route, started):
        m=self._request_metrics
        total=(time.monotonic_ns()-started)/1_000_000
        processing=max(0.0,total-m['serialization_ms']-m['socket_write_ms'])
        LOG.info('request method=%s route=%s status=%s request_id=%s client_to_accept_ms=%s thread_dispatch_ms=%.3f accept_to_handler_ms=%.3f handler_ms=%.3f serialization_ms=%.3f socket_write_ms=%.3f disconnected=%s',
                 method,route,m['status'],m['request_id'],m.get('client_to_accept_ms'),m['thread_dispatch_ms'],m['accept_to_handler_ms'],
                 total,m['serialization_ms'],m['socket_write_ms'],m['client_disconnected'])
        with self.server.app._server_metrics_lock:
            self.server.app._server_metrics.setdefault('request_samples',[]).append(dict(
                route=route,client_to_accept_ms=m.get('client_to_accept_ms'),
                thread_dispatch_ms=m['thread_dispatch_ms'],accept_to_handler_ms=m['accept_to_handler_ms'],
                handler_ms=total,status=m['status']))
            self.server._request_samples.append(dict(route=route,client_to_accept_ms=m.get('client_to_accept_ms'),
                thread_dispatch_ms=m['thread_dispatch_ms'],accept_to_handler_ms=m['accept_to_handler_ms'],
                handler_ms=total,status=m['status']))

    def _dispatch_get(self):
        if not self.trusted_host():
            return self.send(403, {'error': 'Local Host required'})
        path = urlsplit(self.path).path
        app = self.server.app
        try:
            if path == '/api/session':
                return self.send(200, dict(csrf=app.csrf,read_only=app.read_only))
            if path == '/api/monitor':
                return self.send(200, monitor_status(app.monitor_root))
            if path == '/api/alerts':
                return self.send(200, app.alerts_view(parse_qs(urlsplit(self.path).query)))
            if path == '/api/wallet':
                return self.send(200, wallet_view(app.wallet))
            if path == '/api/reports':
                return self.send(200, app.cached_catalog())
            if path == '/api/signals':
                query = parse_qs(urlsplit(self.path).query)
                return self.send(200, app.cached_signal_panel(query.get('provider', ['alpaca'])[0], query.get('feed', ['iex'])[0]))
            if path == '/api/coverage':
                query = parse_qs(urlsplit(self.path).query)
                return self.send(200, app.cached_coverage(query.get('feed', ['iex'])[0]))
            if path.startswith('/api/reports/'):
                return self.send(200, app.report(path.rsplit('/', 1)[-1]))
            assets = {'/favicon.svg': ('favicon.svg', 'image/svg+xml'), '/': ('index.html', 'text/html; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if path in assets:
                filename, mime = assets[path]
                return self.send(200, (STATIC / filename).read_bytes(), mime)
            return self.send(404, {'error': 'Not found'})
        except DashboardBusy:
            return self.send(503, {'error':'Research capacity is busy; retry shortly'})
        except (WalletError, DataError, OSError, KeyError, TypeError, sqlite3.Error):
            self.send(400, {'error': 'Unavailable or invalid local resource'})

    def do_POST(self):
        started=time.monotonic_ns()
        accepted,thread_started=getattr(self.server,'request_clock',lambda _s:(started,started))(self.connection)
        raw_path=urlsplit(self.path).path
        route=raw_path if raw_path in ('/api/funding/prepare','/api/funding/confirm','/api/alerts/ack') else '/api/other'
        self._request_metrics={'route':route,'status':None,'serialization_ms':0.0,
                               'socket_write_ms':0.0,'client_disconnected':False,
                               'accept_to_handler_ms':max(0,(started-accepted)/1_000_000),
                               'thread_dispatch_ms':max(0,(thread_started-accepted)/1_000_000),
                               'client_to_accept_ms':None,
                               'request_id':re.sub('[^a-zA-Z0-9-]','',self.headers.get('X-Request-ID',''))[:64]}
        try:self._dispatch_post()
        finally:self._record_request_timing('POST',route,started)

    def _dispatch_post(self):
        if not self.trusted_host():
            return self.send(403, {'error': 'Local Host required'})
        app = self.server.app
        path = urlsplit(self.path).path
        if app.read_only and path != '/api/alerts/ack':
            return self.send(403, {'error': 'Read-only service; wallet writes and execution are disabled.'})
        if self.headers.get('Origin') != 'http://' + self.headers['Host'] or not hmac.compare_digest(self.headers.get('X-CSRF-Token', '').encode(), app.csrf.encode()):
            return self.send(403, {'error': 'Same-origin confirmation required'})
        try:
            if self.headers.get('Content-Type') != 'application/json' or self.headers.get('Transfer-Encoding'):
                raise WalletError('JSON request required')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise WalletError('Invalid request size')
            body = strict_json(self.rfile.read(length))
            path = urlsplit(self.path).path
            if path == '/api/alerts/ack':
                return self.send(200, app.acknowledge_alert(body))
            if path == '/api/funding/prepare':
                return self.send(200, app.prepare(body))
            if path == '/api/funding/confirm':
                return self.send(200, app.confirm(body))
            return self.send(404, {'error': 'No execution endpoint'})
        except WalletError as exc:
            self.send(400, {'error': str(exc)})
        except (DataError, ValueError, TypeError):
            self.send(400, {'error': 'Invalid read-only dashboard request'})
        except sqlite3.Error:
            self.send(503, {'error': 'Wallet unavailable; retry with the same idempotency key'})


class DashboardHTTPServer(ThreadingHTTPServer):
    # A 60-request local burst must not sit behind TCPServer's default backlog
    # of five. The kernel queue is finite and handler admission is separately
    # bounded so overload cannot create unbounded threads.
    request_queue_size = 128
    MAX_ACTIVE_HANDLERS = 96

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._handler_slots=threading.BoundedSemaphore(self.MAX_ACTIVE_HANDLERS)
        self._accepted_times={}
        self._accepted_lock=threading.Lock()
        self._request_local=threading.local()
        self._request_samples=deque(maxlen=1024)

    def process_request(self,request,client_address):
        accepted=time.monotonic_ns()
        if not self._handler_slots.acquire(blocking=False):
            with self.app._server_metrics_lock:self.app._server_metrics['rejected']+=1
            try:request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\nRetry-After: 1\r\n\r\n')
            except OSError:pass
            self.shutdown_request(request)
            LOG.warning('connection rejected reason=handler_capacity client=%s',client_address[0])
            return
        with self.app._server_metrics_lock:self.app._server_metrics['accepted']+=1
        with self._accepted_lock:self._accepted_times[request.fileno()]=accepted
        try:super().process_request(request,client_address)
        except Exception:
            with self._accepted_lock:self._accepted_times.pop(request.fileno(),None)
            self._handler_slots.release()
            raise

    def process_request_thread(self,request,client_address):
        thread_started=time.monotonic_ns()
        with self._accepted_lock:accepted=self._accepted_times.pop(request.fileno(),thread_started)
        self._request_local.clock=(accepted,thread_started)
        try:super().process_request_thread(request,client_address)
        finally:
            self._request_local.clock=None
            self._handler_slots.release()

    def request_clock(self,_connection):
        return getattr(self._request_local,'clock',(time.monotonic_ns(),time.monotonic_ns()))

    def serve_forever(self,*args,**kwargs):
        try:return super().serve_forever(*args,**kwargs)
        finally:self.app.close()

    def server_close(self):
        try:super().server_close()
        finally:
            app=getattr(self,'app',None)
            if app is not None:app.close()


def make_server(app, port=8895):
    server = DashboardHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.app = app
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8895)
    parser.add_argument('--read-only',action='store_true',help='Disable every wallet write for Phase 5A background operation')
    parser.add_argument('--open-browser',action='store_true',help='Open the localhost dashboard at service startup (throttled after crashes)')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Port must be 1024–65535')
    # Require an existing Phase 2 wallet: dashboard startup cannot invent a balance.
    if not DEFAULT_DB.is_file():
        parser.error('Initialize the persistent wallet explicitly with python -m dual_ma_ai init')
    app = Dashboard(Wallet.open_existing(DEFAULT_DB),read_only=args.read_only,
                    alert_store=AlertStore(DEFAULT_ALERT_DB))
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    with make_server(app, args.port) as server:
        print(f'PAPER ONLY · http://127.0.0.1:{args.port} · Ctrl+C to stop', flush=True)
        if args.open_browser:
            import webbrowser
            marker=MONITOR_ROOT/'browser-opened-at.txt'
            marker.parent.mkdir(parents=True,exist_ok=True)
            try:previous=float(marker.read_text()) if marker.exists() else 0
            except (OSError,ValueError):previous=0
            if time.time()-previous>3600:
                if webbrowser.open(f'http://127.0.0.1:{args.port}'):
                    marker.write_text(str(time.time()))
        def request_shutdown(_signum,_frame):raise KeyboardInterrupt
        signal.signal(signal.SIGTERM,request_shutdown)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
