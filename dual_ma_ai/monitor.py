"""Phase 5A READ ONLY market monitor. No wallet, portfolio, or fill capability."""
import argparse
import fcntl
import getpass
import json
import logging
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import time
import threading
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
from dataclasses import replace

from .alpaca_data import (Client, AlpacaCache, AlpacaHTTPError, AlpacaTransportError,
                          load_credentials, parse_pair, daily_ready_at, SOURCE)
from .market_data import USCalendar, DataError, HistoryUnavailable, digest, timestamp, number
from .strategy import PRESETS, signals

ROOT = Path(__file__).resolve().parent.parent
STATE_ROOT = ROOT/'data/dual_ma_ai/monitor'
TICKERS = ('SPCX','LITE','MRVL','TSM','CIEN','COHR','MU','NVDA','AAOI')
UTC = timezone.utc
LOG = logging.getLogger('dual_ma_ai.dashboard')


class PhaseTimeout(Exception):
    """Contains only an allowlisted stage name, never provider/credential details."""


@contextmanager
def deadline(seconds, stage):
    """Bound main-thread POSIX work, including imports and response-body reads.

    Nested deadlines preserve the shorter outer budget. No thread is left doing
    work after a timeout. The service always runs on the main thread.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Monitor deadlines require the main thread')
    previous_handler=signal.getsignal(signal.SIGALRM)
    remaining, interval=signal.getitimer(signal.ITIMER_REAL)
    started=time.monotonic()
    def expired(*_):raise PhaseTimeout(stage)
    signal.signal(signal.SIGALRM,expired)
    signal.setitimer(signal.ITIMER_REAL,min(seconds,remaining) if remaining else seconds)
    try:yield
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        signal.signal(signal.SIGALRM,previous_handler)
        if remaining:signal.setitimer(signal.ITIMER_REAL,max(.001,remaining-(time.monotonic()-started)),interval)


def keychain_credentials():
    """Capture only authorized items, never inherit credentials through a shell."""
    values = {}
    for variable, service in (('ALPACA_API_KEY','dual-ma-alpaca-api-key'),('ALPACA_SECRET_KEY','dual-ma-alpaca-secret-key')):
        try:
            result = subprocess.run(['/usr/bin/security','find-generic-password','-a',getpass.getuser(),'-s',service,'-w'],capture_output=True,timeout=30)
            if result.returncode:raise DataError('Authorized Keychain item unavailable or locked')
            values[variable] = result.stdout.decode().strip()
        except (OSError,subprocess.TimeoutExpired,UnicodeError):
            raise DataError('Secure Keychain retrieval failed') from None
    return replace(load_credentials(values),origin='macOS Keychain')


@contextmanager
def exclusive(path):
    path = Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise DataError('Another monitor process owns the lock') from None
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)


def schedule(now, calendar=None):
    if now.tzinfo is None:raise DataError('Timezone-aware clock required')
    cal=calendar or USCalendar(now.date()-timedelta(days=14),now.date()+timedelta(days=16))
    sessions=cal.sessions(now.date()-timedelta(days=10),now.date()+timedelta(days=14))
    active=next((day for day in sessions if cal.bounds(day)[0]<=now<cal.bounds(day)[1]),None)
    opens=[cal.bounds(d)[0] for d in sessions if cal.bounds(d)[0]>now]
    completed=[d for d in sessions if daily_ready_at(d)<=now]
    future_daily=[daily_ready_at(d) for d in sessions if daily_ready_at(d)>now]
    return dict(regular_hours=active is not None,expected_candle=str(completed[-1]),
                market_open=cal.bounds(active)[0].isoformat() if active else None,
                market_close=cal.bounds(active)[1].isoformat() if active else None,
                next_market_refresh=(min(now+timedelta(seconds=60),cal.bounds(active)[1]) if active else opens[0]).isoformat(),
                next_daily_refresh=min(future_daily).isoformat(),calendar=cal.name)


class Store:
    def __init__(self,root=STATE_ROOT):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'monitor.sqlite3'
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,at TEXT NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS health_snapshot(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS state_insert_invalidates_snapshot AFTER INSERT ON state BEGIN
              INSERT INTO health_snapshot(id,payload) VALUES(1,'{"_invalidated":true}')
              ON CONFLICT(id) DO UPDATE SET payload=excluded.payload;
            END;
            CREATE TRIGGER IF NOT EXISTS state_update_invalidates_snapshot AFTER UPDATE ON state BEGIN
              INSERT INTO health_snapshot(id,payload) VALUES(1,'{"_invalidated":true}')
              ON CONFLICT(id) DO UPDATE SET payload=excluded.payload;
            END;
            CREATE TRIGGER IF NOT EXISTS state_delete_invalidates_snapshot AFTER DELETE ON state BEGIN
              INSERT INTO health_snapshot(id,payload) VALUES(1,'{"_invalidated":true}')
              ON CONFLICT(id) DO UPDATE SET payload=excluded.payload;
            END;
            CREATE TRIGGER IF NOT EXISTS immutable_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable audit event'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable audit event'); END;
            ''')
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=5);db.execute('PRAGMA busy_timeout=5000')
        try:
            with db:yield db
        finally:db.close()
    def read(self):
        with self.connect() as db:return {k:json.loads(v) for k,v in db.execute('SELECT key,value FROM state')}
    def recover_invalid_state(self,now):
        """Drop only undecodable mutable state rows and record the repair before retrying."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('SELECT key,value FROM state').fetchall();values={};invalid=[]
            for key,raw in rows:
                try:values[key]=json.loads(raw)
                except (ValueError,TypeError):invalid.append(key)
            if not invalid:return values,[]
            db.executemany('DELETE FROM state WHERE key=?',((key,) for key in invalid))
            db.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('health',json.dumps('DEGRADED')))
            db.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('message',json.dumps('Invalid monitor state repaired; read-only validation restarted.')))
            slot=now.replace(second=0,microsecond=0).isoformat()
            audit=event(('invalid-state-recovery',slot,tuple(sorted(invalid))),now,'ERROR','Undecodable mutable monitor state removed; read-only validation restarted.',keys=sorted(invalid))
            db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)',(audit['id'],audit['at'],audit['kind'],audit['message'],json.dumps(audit['payload'],sort_keys=True)))
            return values,invalid
    def save(self,fields,events=()):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for key,value in fields.items():db.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value,sort_keys=True)))
            for event in events:
                db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)',(event['id'],event['at'],event['kind'],event['message'],json.dumps(event.get('payload',{}),sort_keys=True)))
            # Publish one bounded, read-only view atomically with the state and
            # immutable events. HTTP health reads never build calendars or
            # validate historical price data.
            values={}
            for key,raw in db.execute('SELECT key,value FROM state'):
                try:values[key]=json.loads(raw)
                except (ValueError,TypeError):continue
            recent=[dict(id=r[0],at=r[1],kind=r[2],message=r[3])
                    for r in db.execute('SELECT id,at,kind,message FROM events ORDER BY at DESC,id DESC LIMIT 30')]
            snapshot=dict(values);snapshot.pop('_health_snapshot',None);snapshot['recent_events']=recent
            db.execute('INSERT INTO health_snapshot(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                       (json.dumps(snapshot,sort_keys=True,separators=(',',':')),))


def event(identity, now, kind, message, **payload):
    return dict(id=digest(identity),at=now.isoformat(),kind=kind,message=message,payload=payload)


def status(root=STATE_ROOT,now=None):
    """Read one worker-published snapshot; never computes calendars or opens the wallet."""
    now=now or datetime.now(UTC);path=Path(root)/'monitor.sqlite3'
    base=dict(mode='READ_ONLY',paper_execution_enabled=False,feed='iex',status='STOPPED',quotes=[],daily=[],
              message='Background services have not been activated.',last_market_update=None,last_daily_refresh=None,next_scheduled_refresh=None)
    if not path.exists():return base
    try:
        read_started=time.monotonic_ns()
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.25)) as db:
                db.execute('BEGIN')
                has_snapshot=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='health_snapshot'").fetchone()
                row=db.execute('SELECT payload FROM health_snapshot WHERE id=1').fetchone() if has_snapshot else None
                if row:
                    values=json.loads(row[0]);recent=values.get('recent_events',[])
                    if values.get('_invalidated') is True:raise ValueError('Health snapshot invalidated by state change')
                else:
                    # Pre-snapshot databases are read fail-closed and migrated
                    # by the worker's next atomic save, never by the API.
                    values={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM state')}
                    recent=[dict(id=r[0],at=r[1],kind=r[2],message=r[3]) for r in db.execute('SELECT id,at,kind,message FROM events ORDER BY at DESC,id DESC LIMIT 30')]
        except sqlite3.Error as exc:
            LOG.warning('stage=monitor.status.sqlite_read outcome=ERROR exception_class=%s duration_ms=%.3f',
                        type(exc).__name__,(time.monotonic_ns()-read_started)/1_000_000)
            raise
        read_ms=(time.monotonic_ns()-read_started)/1_000_000
        base.update(values);base['recent_events']=recent
        heartbeat=timestamp(values['heartbeat']) if values.get('heartbeat') else None
        if not values.get('running') or heartbeat is None or now-heartbeat>timedelta(seconds=180):
            base['status']='STOPPED';base['message']='Worker stopped or heartbeat overdue; no execution capability.'
        else:
            base['status']=values.get('health','DEGRADED')
            if base['status']=='RUNNING' and not values.get('last_successful_refresh'):
                base['status']='DEGRADED'
                base['message']='Heartbeat present, but no successful validated refresh recorded.'
        info=values.get('schedule')
        if not isinstance(info,dict) or not info.get('expected_candle'):
            info=None
            if base['status'] not in ('STOPPED','ERROR'):
                base['status']='DEGRADED'
                base['message']='Health snapshot is initializing; signals are suppressed.'
        base['schedule']=info
        LOG.info('stage=monitor.status snapshot_read_ms=%.3f',read_ms)
        for quote in base.get('quotes',[]):
            quote_at=timestamp(quote['timestamp']) if quote.get('timestamp') else None
            age=max(0,int((now-quote_at).total_seconds())) if quote_at and quote_at<=now else None
            quote['age_seconds']=age
            quote['status']='UNAVAILABLE' if quote_at is None else 'STALE' if age is None or age>120 else 'VALIDATED'
        schedule_due=False
        if info:
            try:schedule_due=bool(info.get('next_daily_refresh') and now>=timestamp(info['next_daily_refresh']))
            except (TypeError,ValueError,DataError):schedule_due=True
        for row in base.get('daily',[]):
            if row.get('status')!='VALIDATED':
                row['signals']=[]
            elif (not info or base['status'] in ('STOPPED','ERROR') or schedule_due or row.get('candle')!=info['expected_candle'] or
                  base.get('in_progress') or base.get('phase') not in (None,'idle')):
                row['status']='STALE';row['signals']=[];row['explanation']='Latest expected completed daily candle or current worker validation unavailable; new decisions blocked.'
        if base['status']=='RUNNING' and any(r['status']=='STALE' for r in base.get('daily',[])):base['status']='STALE'
        if base['status']=='RUNNING' and info and info.get('regular_hours') and any(q['status']=='STALE' for q in base.get('quotes',[])):base['status']='STALE'
        if base.get('in_progress') and base['status'] not in ('STOPPED','ERROR'):
            base['status']='DEGRADED';base['message']='Read-only refresh in progress; decisions are temporarily suppressed.'
        base['connection_health']=values.get('connection_health','UNKNOWN')
        return base
    except (OSError,sqlite3.Error,ValueError,KeyError,DataError,RuntimeError):
        base.update(status='ERROR',message='Monitor state unavailable or invalid; no decisions permitted.');return base


def quote_rows(response, now):
    if not isinstance(response.get('quotes'),dict):raise DataError('Invalid quote collection')
    rows=[]
    for ticker in TICKERS:
        try:
            row=response['quotes'][ticker];at=timestamp(row['t']);bid=number(row['bp']);ask=number(row['ap'])
            if at>now or now-at>timedelta(seconds=120) or bid>ask:raise DataError('Stale, future or crossed quote')
            if any(type(row.get(k)) is not int or row[k]<=0 for k in ('bs','as')):raise DataError('Invalid quote size')
            rows.append(dict(ticker=ticker,feed='iex',bid=str(row['bp']),ask=str(row['ap']),timestamp=at.isoformat(),status='VALIDATED',source='Alpaca latest IEX quote; not a fill or daily candle'))
        except (KeyError,TypeError,ValueError,DataError):
            rows.append(dict(ticker=ticker,feed='iex',timestamp=None,status='UNAVAILABLE',explanation='Missing, stale or invalid IEX quote; no price invented.'))
    return rows


class Worker:
    def __init__(self,store,client_factory=None,clock=None,logger=None):
        self.store=store;self.cache=AlpacaCache(store.root/'history')
        self.client_factory=client_factory or (lambda:Client(keychain_credentials(),attempts=2,timeout=10))
        self.clock=clock or (lambda:datetime.now(UTC));self.logger=logger or logging.getLogger('dual_ma_ai.monitor')
        self._active_stage='idle';self._active_ticker=None;self._stage_started=time.monotonic()
        self._last_completed_stage=None;self._last_phase_duration_ms=None
    @contextmanager
    def phase(self,name,seconds=45):
        now=self.clock();started=time.monotonic()
        prior=(self._active_stage,self._active_ticker,self._stage_started)
        self._active_stage=name;self._active_ticker=name.rsplit('-',1)[1] if name.startswith(('history-','validation-')) else None;self._stage_started=started
        self.store.save(dict(running=True,heartbeat=now.isoformat(),phase=name,phase_started_at=now.isoformat()))
        self.logger.info('Stage %s started',name)
        try:
            with deadline(seconds,name):yield
        finally:
            elapsed=time.monotonic()-started
            self._last_completed_stage=name;self._last_phase_duration_ms=max(0,int(elapsed*1000))
            self.logger.info('Stage %s ended; seconds=%.3f',name,elapsed)
            self._active_stage,self._active_ticker,self._stage_started=prior
    def tick(self,force=False):
        """Run one atomic publication cycle; retain prior validated snapshot on failure."""
        now=self.clock();slot=now.replace(second=0,microsecond=0).isoformat()
        try:
            return self._tick(force)
        except Exception as exc:
            # Never persist exception text, request URLs, headers, or response bodies.
            status_code=getattr(exc,'status',None)
            attempt=getattr(exc,'attempts',None)
            duration=getattr(exc,'duration_ms',None)
            exception_class=getattr(exc,'exception_class',type(exc).__name__)
            stage=self._active_stage
            ticker=self._active_ticker
            try:
                persisted=self.store.read();persisted_phase=persisted.get('phase')
                if isinstance(persisted_phase,str) and persisted_phase not in ('idle','startup'):
                    stage=persisted_phase
                    if stage.startswith(('history-','validation-')):ticker=stage.rsplit('-',1)[1]
                    if duration is None and self._last_completed_stage==stage:
                        duration=self._last_phase_duration_ms
                    elif duration is None and persisted.get('phase_started_at'):
                        duration=max(0,int((self.clock()-timestamp(persisted['phase_started_at'])).total_seconds()*1000))
            except Exception:
                pass
            if duration is None:duration=max(0,int((time.monotonic()-self._stage_started)*1000))
            safe=dict(exception_class=str(exception_class)[:80],stage=stage,ticker=ticker,
                      http_status=status_code if isinstance(status_code,int) else None,
                      attempt=attempt if isinstance(attempt,int) else None,duration_ms=duration)
            self.logger.error('Worker failure class=%s stage=%s ticker=%s http_status=%s attempt=%s duration_ms=%d',
                              safe['exception_class'],stage,ticker or '-',safe['http_status'] if safe['http_status'] is not None else '-',
                              safe['attempt'] if safe['attempt'] is not None else '-',duration)
            message='Read-only refresh failed; prior validated data retained and new decisions blocked.'
            try:
                old=self.store.read()
                error=event(('worker-failure',slot),now,'ERROR',message,**safe)
                self.store.save(dict(heartbeat=self.clock().isoformat(),running=True,in_progress=False,
                    last_attempt_slot=slot,last_daily_attempt=now.isoformat(),phase='idle',health='ERROR',
                    connection_health='ERROR',message=message,
                    next_scheduled_refresh=(self.clock()+timedelta(seconds=60)).isoformat()),[error])
                return self.store.read()
            except Exception:
                # State-store failure cannot be repaired in-process; main loop logs a sanitized marker.
                raise
    def _tick(self,force=False):
        # A heartbeat is liveness, not proof of calendar/data readiness.
        now=self.clock();state_recovered=False
        try:old=self.store.read()
        except (ValueError,TypeError):
            old,invalid=self.store.recover_invalid_state(now)
            state_recovered=True
            self.logger.error('Invalid monitor state repaired; undecodable keys discarded=%d',len(invalid))
        slot=now.replace(second=0,microsecond=0).isoformat()
        if old.get('last_attempt_slot')==slot and not force and not old.get('in_progress') and not state_recovered:
            self.store.save(dict(running=True,heartbeat=now.isoformat(),phase='idle'))
            return self.store.read()
        self.store.save(dict(health=old.get('health','DEGRADED'),message=old.get('message','Initializing read-only monitor; data readiness not established.')))
        try:
            with self.phase('calendar',20):info=schedule(now)
        except PhaseTimeout:
            return self.fail_phase('calendar',now)
        need_quotes=info['regular_hours'] or force  # Explicit dry-run also probes quote access outside regular hours.
        daily=old.get('daily',[])
        incomplete=(old.get('daily_expected')!=info['expected_candle'] or len(daily)!=len(TICKERS)
                    or any(r['status']!='VALIDATED' or r.get('candle')!=info['expected_candle'] for r in daily))
        retry_due=not old.get('last_daily_attempt') or now-timestamp(old['last_daily_attempt'])>=timedelta(seconds=300)
        need_daily=force or old.get('in_progress',False) or old.get('daily_expected')!=info['expected_candle'] or (incomplete and retry_due)
        self.store.save(dict(running=True,heartbeat=now.isoformat(),schedule=info,next_scheduled_refresh=(now+timedelta(seconds=60)).isoformat()))
        if not need_quotes and not need_daily:
            self.store.save(dict(phase='idle'))
            return self.store.read()
        start_events=[event(('attempt',slot),now,'REFRESH','Read-only IEX refresh started.')]
        if old.get('in_progress'):start_events.append(event(('restart',slot),now,'RECOVERY','Incomplete refresh recovered after restart; only current data requested.'))
        self.store.save(dict(last_attempt_slot=slot,in_progress=True),start_events)
        updates={};events=[];failures=[];network_failed=False
        try:
            with self.phase('credentials',65):client=self.client_factory()
            if need_quotes:
                with self.phase('quotes',55):
                    updates['quotes']=quote_rows(client.get('/v2/stocks/quotes/latest',dict(symbols=','.join(TICKERS),feed='iex')),self.clock())
                updates['last_market_attempt']=now.isoformat()
                updates['last_market_update']=now.isoformat() if any(q['status']=='VALIDATED' for q in updates['quotes']) else old.get('last_market_update')
                if any(q['status']!='VALIDATED' for q in updates['quotes']):failures.append('Some current quotes are unavailable or stale')
            if need_daily:
                updates['last_daily_attempt']=now.isoformat();updates['daily_expected']=info['expected_candle'];daily=[]
                from datetime import date
                end=date.fromisoformat(info['expected_candle']);start=end-timedelta(days=450)
                with self.phase('history-calendar',20):calendar=USCalendar(start-timedelta(days=7),end+timedelta(days=7))
                for ticker in TICKERS:
                    self.store.save(dict(heartbeat=self.clock().isoformat()))
                    try:
                        with self.phase('history-'+ticker,55):
                            payload=dict(feed='iex',start=str(start),end=str(end),asof='-',raw=client.bars(ticker,start,end,'iex','raw'),split=client.bars(ticker,start,end,'iex','split'))
                        with self.phase('validation-'+ticker,20):ds,validation=parse_pair(ticker,payload,now,calendar)
                        if ds.bars[-1].day!=end or len(ds.bars)<201:raise DataError('Latest completed candle or MA warmup unavailable')
                        try:
                            (prior,_),_=self.cache.latest(ticker,'iex')
                            past={b.day:b for b in prior.bars}
                            if any(b.day in past and (b.open,b.high,b.low,b.close,b.volume,b.split)!=(past[b.day].open,past[b.day].high,past[b.day].low,past[b.day].close,past[b.day].volume,past[b.day].split) for b in ds.bars[1:]):
                                raise DataError('Historical overlap revision requires review')
                        except HistoryUnavailable:
                            prior=None
                        if prior is None or prior.content_hash!=ds.content_hash:self.cache.save(ticker,payload,now,calendar)
                        choices=[]
                        for strategy in PRESETS:
                            latest=next((s for s in reversed(signals(ds,strategy,as_of=now)) if s.day==end),None)
                            side=latest.side if latest else 'NO CROSS'
                            explanation='Confirmed completed-candle crossover; informational only, execution disabled.' if latest else 'No new crossover on the latest completed daily candle; bullish regime alone does not imply entry.'
                            choices.append(dict(strategy=strategy.name,signal=side,candle=str(end),usable_at=ds.bars[-1].usable_at.isoformat(),evaluated_at=now.isoformat(),explanation=explanation))
                            events.append(event(('signal',ticker,strategy.name,str(end),side),now,side,explanation,ticker=ticker,strategy=strategy.name,candle=str(end)))
                        daily.append(dict(ticker=ticker,status='VALIDATED',feed='iex',source=SOURCE,candle=str(end),closed_at=ds.bars[-1].closed_at.isoformat(),usable_at=ds.bars[-1].usable_at.isoformat(),fetched_at=now.isoformat(),payload_sha256=ds.content_hash,validation=validation,signals=choices))
                    except (AlpacaHTTPError,AlpacaTransportError,PhaseTimeout):
                        raise
                    except (DataError,OSError,ValueError) as exc:
                        try:failed_stage=self.store.read().get('phase',self._active_stage)
                        except Exception:failed_stage=self._active_stage
                        blocked_duration=getattr(exc,'duration_ms',None)
                        if blocked_duration is None and self._last_completed_stage==failed_stage:
                            blocked_duration=self._last_phase_duration_ms
                        if blocked_duration is None:
                            blocked_duration=max(0,int((time.monotonic()-self._stage_started)*1000))
                        blocked_status=getattr(exc,'status',None);blocked_attempt=getattr(exc,'attempts',None)
                        self.logger.warning('Ticker data blocked class=%s stage=%s ticker=%s http_status=%s attempt=%s duration_ms=%d',
                            type(exc).__name__,failed_stage,ticker,
                            blocked_status if blocked_status is not None else '-',blocked_attempt if blocked_attempt is not None else '-',blocked_duration)
                        daily.append(dict(ticker=ticker,status='BLOCKED',signals=[],explanation='Daily data missing, stale, inconsistent or unvalidated; signals blocked.'))
                        failures.append(ticker+': daily validation blocked')
                        events.append(event(('blocked',slot,ticker),now,'BLOCKED','Daily data unavailable or failed validation; no decision.',
                            ticker=ticker,exception_class=type(exc).__name__,stage=failed_stage,
                            http_status=blocked_status if isinstance(blocked_status,int) else None,
                            attempt=blocked_attempt if isinstance(blocked_attempt,int) else None,
                            duration_ms=blocked_duration if isinstance(blocked_duration,int) else None))
                updates['daily']=daily;updates['last_daily_refresh']=now.isoformat() if any(r['status']=='VALIDATED' for r in daily) else old.get('last_daily_refresh')
                updates['last_strategy_evaluation']=now.isoformat() if any(r['status']=='VALIDATED' for r in daily) else old.get('last_strategy_evaluation')
        except PhaseTimeout as exc:
            return self.fail_phase(str(exc),now)
        except (DataError,OSError,ValueError,subprocess.SubprocessError) as exc:
            network_failed=True;failures.append('Keychain or market-data transport unavailable; details suppressed to protect credentials')
            phase_state=self.store.read()
            stage=phase_state.get('phase','refresh-cycle')
            ticker=stage.rsplit('-',1)[1] if stage.startswith(('history-','validation-')) else None
            exception_class=getattr(exc,'exception_class',type(exc).__name__)
            status_code=getattr(exc,'status',None);attempt=getattr(exc,'attempts',None)
            duration=getattr(exc,'duration_ms',None)
            self.logger.error('Provider failure class=%s stage=%s ticker=%s http_status=%s attempt=%s duration_ms=%s',
                exception_class,stage,ticker or '-',status_code if status_code is not None else '-',
                attempt if attempt is not None else '-',duration if duration is not None else '-')
            events.append(event(('provider-failure',slot),now,'ERROR',
                'Read-only market-data request failed; prior validated data retained.',
                exception_class=str(exception_class)[:80],stage=stage,ticker=ticker,
                http_status=status_code if isinstance(status_code,int) else None,
                attempt=attempt if isinstance(attempt,int) else None,
                duration_ms=duration if isinstance(duration,int) else None))
        health='ERROR' if network_failed else 'DEGRADED' if failures else 'RUNNING'
        events.append(event(('result',slot),now,'ERROR' if network_failed else 'BLOCKED' if failures else 'REFRESH', '; '.join(failures) if failures else 'Read-only refresh successful.'))
        was_failed=old.get('health')=='ERROR' or (old.get('health')=='DEGRADED' and old.get('last_attempt_slot'))
        if was_failed and health=='RUNNING':events.append(event(('recovered',slot),now,'RECOVERY','Market-data service recovered.'))
        if not network_failed and any(r['status']=='VALIDATED' for r in updates.get('daily',[])+updates.get('quotes',[])):
            updates['last_successful_refresh']=self.clock().isoformat()
        updates.update(heartbeat=self.clock().isoformat(),running=True,in_progress=False,phase='idle',health=health,connection_health='ERROR' if network_failed else 'CONNECTED',message='; '.join(failures) if failures else 'Read-only IEX monitoring; PAPER execution disabled.',next_scheduled_refresh=(self.clock()+timedelta(seconds=60)).isoformat())
        self.store.save(updates,events);self.logger.info('Read-only refresh %s; validation failures=%d',health,len(failures))
        return self.store.read()
    def fail_phase(self,stage,now):
        ticker=stage.rsplit('-',1)[1] if stage.startswith(('history-','validation-')) else None
        if self._last_completed_stage==stage:
            duration=self._last_phase_duration_ms
        else:
            try:
                persisted=self.store.read();started=timestamp(persisted['phase_started_at'])
                duration=max(0,int((now-started).total_seconds()*1000))
            except (KeyError,TypeError,ValueError,DataError):duration=None
        diagnostic=dict(exception_class='PhaseTimeout',stage=stage,ticker=ticker,http_status=None,attempt=None,duration_ms=duration)
        self.store.save(dict(running=True,heartbeat=self.clock().isoformat(),health='ERROR',connection_health='ERROR',phase=stage,in_progress=False,message='Read-only stage deadline exceeded: '+stage+'. No new decisions published.',next_scheduled_refresh=(self.clock()+timedelta(seconds=60)).isoformat()),[event(('timeout',now.isoformat(),stage),now,'ERROR','Read-only stage deadline exceeded: '+stage,**diagnostic)])
        self.logger.error('Worker failure class=PhaseTimeout stage=%s ticker=%s http_status=- attempt=- duration_ms=%s',stage,ticker or '-',duration if duration is not None else '-')
        return self.store.read()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('run','dry-run','status'))
    parser.add_argument('--state-root',type=Path,default=STATE_ROOT,help='Separate monitoring state; never a wallet')
    parser.add_argument('--once',action='store_true',help='One foreground read-only cycle; no scheduler installation')
    args=parser.parse_args()
    if args.command=='status':print(json.dumps(status(args.state_root),indent=2));return
    if args.command=='dry-run':
        with tempfile.TemporaryDirectory(prefix='dual-ma-monitor-dry-') as tmp:
            result=Worker(Store(tmp)).tick(force=True)
            print(json.dumps(dict(mode='READ_ONLY_DRY_RUN',state=result),indent=2));return
    args.state_root.mkdir(parents=True,exist_ok=True)
    logger=logging.getLogger('dual_ma_ai.monitor');logger.setLevel(logging.INFO)
    handler=RotatingFileHandler(args.state_root/'worker.log',maxBytes=1_000_000,backupCount=3);logger.addHandler(handler)
    logger.info('Read-only startup; pid=%d; python=%s; cwd=%s; state=%s',os.getpid(),os.sys.executable,Path.cwd(),args.state_root)
    stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        with exclusive(args.state_root/'worker.lock'):
            with deadline(10,'state-initialization'):store=Store(args.state_root)
            worker=Worker(store,logger=logger)
            logger.info('State initialized; entering read-only cycle')
            now=datetime.now(UTC)
            prior=store.read()
            store.save(dict(running=True,heartbeat=now.isoformat(),health=prior.get('health','DEGRADED'),phase='startup',message=prior.get('message','Read-only startup; validated data readiness pending.')),[event(('startup',now.isoformat(),os.getpid()),now,'STARTUP','Read-only worker initialized; validated data readiness pending.')])
            try:
                while not stopping:
                    try:
                        with deadline(120,'refresh-cycle'):worker.tick()
                    except PhaseTimeout:
                        worker.fail_phase(store.read().get('phase','refresh-cycle'),datetime.now(UTC))
                    except Exception as exc:
                        # Last-resort protection for failures in diagnostics/state publication itself.
                        now=datetime.now(UTC);stage=store.read().get('phase','refresh-cycle')
                        logger.error('Worker loop failure class=%s stage=%s ticker=- http_status=%s attempt=%s duration_ms=unknown',type(exc).__name__,stage,getattr(exc,'status','-'),getattr(exc,'attempts','-'))
                        store.save(dict(heartbeat=now.isoformat(),health='ERROR',running=True,in_progress=False,last_attempt_slot=now.replace(second=0,microsecond=0).isoformat(),phase='idle',message='Read-only worker loop failed; new decisions blocked.'),[event(('unexpected',now.replace(second=0,microsecond=0).isoformat()),now,'ERROR','Read-only worker loop failed; new decisions blocked.',exception_class=type(exc).__name__,stage=stage,ticker=None,http_status=getattr(exc,'status',None),attempt=getattr(exc,'attempts',None),duration_ms=None)])
                    if args.once:break
                    for _ in range(60):
                        if stopping:break
                        time.sleep(1)
            finally:store.save(dict(running=False,heartbeat=datetime.now(UTC).isoformat()))
    except PhaseTimeout:logger.error('State initialization deadline exceeded; no data decisions');raise SystemExit(2)
    except DataError:logger.error('Duplicate worker rejected');raise SystemExit(2)


if __name__=='__main__':main()
