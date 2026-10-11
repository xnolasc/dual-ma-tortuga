import io
import json
import sqlite3
import tempfile
import unittest
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stdout
from fractions import Fraction

from dual_ma_ai.monitor import Store,Worker,status,schedule,exclusive,keychain_credentials,quote_rows,TICKERS
from dual_ma_ai.alpaca_data import Credentials
from dual_ma_ai.market_data import USCalendar,DataError
from dual_ma_ai.launch_agents import configurations
from dual_ma_ai.dashboard import Dashboard,make_server
from dual_ma_ai.wallet import Wallet
from http.client import HTTPConnection
import threading

UTC=timezone.utc
NOW=datetime(2026,10,9,15,tzinfo=UTC)


class FakeClient:
    def __init__(self,now=NOW):self.now=now;self.calls=[];self.fail=False;self.missing=False;self.revision=False;self.templates={}
    def get(self,path,query):
        self.calls.append((path,query))
        if self.fail:raise DataError('private-secret-must-not-leak')
        return {'quotes':{t:dict(t=self.now.isoformat(),bp='100.00',ap='100.01',bs=100,**{'as':100}) for t in TICKERS}}
    def bars(self,ticker,start,end,feed,adjustment):
        self.calls.append((ticker,adjustment,start,end))
        from zoneinfo import ZoneInfo
        if (start,end) not in self.templates:
            cal=USCalendar(start-timedelta(days=7),end+timedelta(days=7));days=cal.sessions(start,end)
            self.templates[start,end]=[dict(t=datetime.combine(d,datetime.min.time(),ZoneInfo('America/New_York')).isoformat(),o='100',h='100',l='100',c='100',v=10000) for d in days]
        rows=[dict(r) for r in self.templates[start,end]]
        if self.missing:rows.pop(-3)
        if self.revision:
            for key in ('o','h','l','c'):rows[-3][key]='101'
        return rows


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root)
        self.client=FakeClient();self.now=NOW
        self.worker=Worker(self.store,lambda:self.client,lambda:self.now)
    def tearDown(self):self.tmp.cleanup()
    def test_calendar_holiday_early_close_and_dst(self):
        self.assertFalse(schedule(datetime(2026,11,26,16,tzinfo=UTC))['regular_hours'])
        early=schedule(datetime(2026,11,27,17,59,tzinfo=UTC))
        self.assertEqual(early['market_close'],'2026-11-27T18:00:00+00:00')
        self.assertFalse(schedule(datetime(2026,11,27,18,tzinfo=UTC))['regular_hours'])
        self.assertEqual(schedule(datetime(2026,3,6,15,tzinfo=UTC))['market_open'],'2026-03-06T14:30:00+00:00')
        self.assertEqual(schedule(datetime(2026,3,9,15,tzinfo=UTC))['market_open'],'2026-03-09T13:30:00+00:00')
    def test_daily_finalization_not_regular_close(self):
        before=schedule(datetime(2026,10,10,4,15,tzinfo=UTC))
        after=schedule(datetime(2026,10,10,4,16,tzinfo=UTC))
        self.assertEqual(before['expected_candle'],'2026-10-08')
        self.assertEqual(after['expected_candle'],'2026-10-09')
    def test_read_only_tick_records_signals_and_quote_provenance(self):
        result=self.worker.tick()
        self.assertEqual(result['health'],'RUNNING')
        self.assertEqual(len(result['daily']),9)
        self.assertTrue(all(len(r['signals'])==3 for r in result['daily']))
        self.assertTrue(all(s['signal']=='NO CROSS' for r in result['daily'] for s in r['signals']))
        self.assertEqual(status(self.root,NOW)['status'],'RUNNING')
        with self.store.connect() as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables,{'state','events','health_snapshot'})
    def test_duplicate_slot_and_restart_do_not_duplicate_writes(self):
        self.worker.tick();n=len(self.client.calls);files=list((self.root/'history').glob('*.json'))
        Worker(self.store,lambda:self.client,lambda:self.now).tick()
        self.assertEqual(len(self.client.calls),n)
        self.now+=timedelta(minutes=5);self.client.now=self.now
        self.worker.tick(force=True)
        self.assertEqual(len(list((self.root/'history').glob('*.json'))),len(files))
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='NO CROSS'").fetchone()[0],27)
    def test_quotes_every_minute_daily_batch_not_every_minute(self):
        self.worker.tick();n=len(self.client.calls);self.now+=timedelta(minutes=1);self.client.now=self.now
        self.worker.tick();self.assertEqual(len(self.client.calls)-n,1)
    def test_invalid_quotes_do_not_claim_a_successful_market_refresh(self):
        self.worker.tick();first=self.store.read()['last_market_update'];self.now+=timedelta(minutes=3)
        result=self.worker.tick()
        self.assertEqual(result['health'],'DEGRADED');self.assertEqual(result['last_market_update'],first)
    def test_new_completed_candle_does_not_wait_for_previous_retry_backoff(self):
        self.worker.tick();self.now=datetime(2026,10,10,4,16,tzinfo=UTC)
        self.store.save(dict(last_daily_attempt=(self.now-timedelta(seconds=30)).isoformat()))
        result=self.worker.tick();self.assertEqual(result['daily'][0]['candle'],'2026-10-09')
    def test_network_failure_is_sanitized_and_recovery_logged(self):
        self.client.fail=True;result=self.worker.tick()
        self.assertEqual(result['health'],'ERROR')
        self.assertNotIn('private-secret',json.dumps(result))
        self.client.fail=False;self.now+=timedelta(minutes=1);self.client.now=self.now
        self.worker.tick()
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='RECOVERY'").fetchone()[0],1)
    def test_missing_sessions_block_all_new_daily_decisions(self):
        import time
        self.client.missing=True;self.worker._stage_started=time.monotonic()-400;result=self.worker.tick()
        self.assertEqual(result['health'],'DEGRADED')
        self.assertTrue(all(not r['signals'] and r['status']=='BLOCKED' for r in result['daily']))
        with self.store.connect() as db:
            payloads=[json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE kind='BLOCKED' AND payload LIKE '%exception_class%'")]
        self.assertEqual(len(payloads),len(TICKERS))
        self.assertTrue(all(0<=p['duration_ms']<10000 for p in payloads),payloads)
    def test_overlapping_revision_is_not_silently_accepted(self):
        self.worker.tick();self.client.revision=True;self.now+=timedelta(minutes=5);self.client.now=self.now
        result=self.worker.tick(force=True)
        self.assertTrue(all(r['status']=='BLOCKED' for r in result['daily']))
    def test_holiday_heartbeat_does_not_fetch_live_prices(self):
        self.now=datetime(2026,11,26,16,tzinfo=UTC)
        self.worker.tick()
        self.assertFalse(any(c[0]=='/v2/stocks/quotes/latest' for c in self.client.calls))
        n=len(self.client.calls);self.now+=timedelta(minutes=1);self.worker.tick();self.assertEqual(n,len(self.client.calls))
    def test_sleep_wake_recovery_requests_only_current_quotes(self):
        self.worker.tick();self.now=datetime(2026,10,12,15,tzinfo=UTC);self.client.now=self.now
        result=self.worker.tick()
        self.assertEqual(result['quotes'][0]['timestamp'],self.now.isoformat())
        self.assertEqual(result['daily'][0]['candle'],'2026-10-09')
    def test_stopped_heartbeat_and_stale_quotes(self):
        self.worker.tick();self.store.save(dict(heartbeat=(NOW+timedelta(minutes=3)).isoformat()))
        self.assertEqual(status(self.root,NOW+timedelta(minutes=3))['status'],'STALE')
        self.assertEqual(status(self.root,NOW+timedelta(minutes=10))['status'],'STOPPED')
    def test_new_expected_candle_blocks_old_signal_display(self):
        self.worker.tick();self.store.save(dict(heartbeat='2026-10-10T05:00:00+00:00'))
        result=status(self.root,datetime(2026,10,10,5,tzinfo=UTC))
        self.assertEqual(result['status'],'STALE');self.assertTrue(all(not r['signals'] for r in result['daily']))
    def test_health_snapshot_read_never_constructs_calendar(self):
        self.worker.tick()
        with patch('dual_ma_ai.monitor.schedule',side_effect=AssertionError('health path computed calendar')):
            result=status(self.root,NOW)
        self.assertEqual(result['status'],'RUNNING')
        self.assertEqual(result['schedule']['expected_candle'],'2026-10-08')
    def test_health_snapshot_suppresses_signals_during_refresh_and_for_blocked_rows(self):
        self.worker.tick()
        rows=self.store.read()['daily']
        rows[0]=dict(ticker='SPCX',status='BLOCKED',signals=[dict(signal='BUY')])
        self.store.save(dict(daily=rows,phase='history-NVDA',in_progress=True))
        result=status(self.root,NOW)
        self.assertEqual(result['status'],'DEGRADED')
        self.assertEqual(result['daily'][0]['signals'],[])
        self.assertTrue(all(not row['signals'] for row in result['daily']))
    def test_snapshot_due_transition_marks_data_stale_without_calendar_work(self):
        self.worker.tick()
        self.store.save(dict(schedule=dict(expected_candle='2026-10-08',regular_hours=False,
                                            next_daily_refresh=NOW.isoformat())))
        with patch('dual_ma_ai.monitor.schedule',side_effect=AssertionError('health path computed calendar')):
            result=status(self.root,NOW)
        self.assertEqual(result['status'],'STALE')
        self.assertTrue(all(row['status']=='STALE' and not row['signals'] for row in result['daily']))
    def test_stopped_worker_suppresses_previously_validated_signals(self):
        self.worker.tick()
        self.store.save(dict(running=False,heartbeat=NOW.isoformat(),phase='idle',in_progress=False))
        result=status(self.root,NOW)
        self.assertEqual(result['status'],'STOPPED')
        self.assertTrue(all(row['status']=='STALE' and not row['signals'] for row in result['daily']))
    def test_missing_state_is_stopped_without_creating_database(self):
        root=self.root/'missing';self.assertEqual(status(root)['status'],'STOPPED');self.assertFalse(root.exists())
    def test_lock_rejects_duplicate_and_releases_after_exception(self):
        path=self.root/'worker.lock'
        with exclusive(path):
            with self.assertRaises(DataError):
                with exclusive(path):pass
        with exclusive(path):pass
    def test_restart_recovers_incomplete_atomic_refresh(self):
        self.store.save(dict(in_progress=True,last_attempt_slot=NOW.replace(second=0,microsecond=0).isoformat()))
        result=self.worker.tick();self.assertFalse(result['in_progress'])
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='RECOVERY'").fetchone()[0],1)
    def test_event_ledger_cannot_be_rewritten(self):
        self.worker.tick()
        with self.store.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):db.execute("UPDATE events SET kind='BUY'")
    def test_quote_validation_rejects_future_crossed_and_stale(self):
        data=self.client.get('/v2/stocks/quotes/latest',{})
        for key,value in [('t',(NOW+timedelta(seconds=1)).isoformat()),('bp','101'),('t',(NOW-timedelta(minutes=3)).isoformat())]:
            original=dict(data['quotes']['MU']);data['quotes']['MU'][key]=value
            self.assertEqual(next(r for r in quote_rows(data,NOW) if r['ticker']=='MU')['status'],'UNAVAILABLE')
            data['quotes']['MU']=original
    def test_keychain_only_capture_exact_items_no_shell_or_secret_repr(self):
        calls=[]
        def security(args,**kw):
            calls.append((args,kw));return SimpleNamespace(returncode=0,stdout=b'mock-only-secret',stderr=b'')
        with patch('dual_ma_ai.monitor.subprocess.run',side_effect=security):c=keychain_credentials()
        self.assertNotIn('mock-only-secret',repr(c))
        self.assertEqual([a[0][-2] for a in calls],['dual-ma-alpaca-api-key','dual-ma-alpaca-secret-key'])
        self.assertTrue(all(a[1]['capture_output'] and not a[1].get('shell') for a in calls))
    def test_keychain_error_does_not_leak_stderr(self):
        with patch('dual_ma_ai.monitor.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout=b'',stderr=b'secret')):
            with self.assertRaises(DataError) as exc:keychain_credentials()
        self.assertNotIn('secret',str(exc.exception))
    def test_launch_agents_disabled_read_only_and_no_install_actions(self):
        configs=configurations(self.root)
        self.assertEqual(len(configs),2)
        for c in configs.values():self.assertTrue(c['Disabled'] and c['RunAtLoad'] and c['KeepAlive'])
        self.assertIn('--read-only',configs['local.dual-ma-ai.dashboard']['ProgramArguments'])
        self.assertNotIn('secret',json.dumps(configs))
    def test_dashboard_read_only_rejects_funding_and_exposes_status(self):
        wallet=Wallet(self.root/'test-wallet.sqlite3');before=Path(wallet.path).read_bytes()
        app=Dashboard(wallet,reports=self.root/'reports',cache=self.root/'alpha',alpaca_cache=self.root/'alpaca',monitor_root=self.root,read_only=True)
        with make_server(app,0) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            client=HTTPConnection('127.0.0.1',server.server_port)
            client.request('POST','/api/funding/confirm',body='{}');r=client.getresponse();self.assertEqual(r.status,403);r.read()
            client.request('GET','/api/monitor');r=client.getresponse();self.assertEqual(json.loads(r.read())['mode'],'READ_ONLY');client.close()
            server.shutdown();thread.join()
        self.assertEqual(Path(wallet.path).read_bytes(),before)

    def test_dashboard_monitor_missing_invalid_recovery_and_two_cycles_are_isolated(self):
        monitor_root=self.root/'isolated-monitor';wallet=Wallet(self.root/'isolated-wallet.sqlite3')
        wallet_before=Path(wallet.path).read_bytes()
        cycle_now=datetime.now(UTC).replace(second=0,microsecond=0);client=FakeClient(cycle_now)
        app=Dashboard(wallet,reports=self.root/'reports',cache=self.root/'alpha',alpaca_cache=self.root/'alpaca',monitor_root=monitor_root,read_only=True)
        with make_server(app,0) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def get_monitor():
                client=HTTPConnection('127.0.0.1',server.server_port,timeout=10)
                client.request('GET','/api/monitor');response=client.getresponse();data=json.loads(response.read());client.close()
                self.assertEqual(response.status,200);return data
            missing=get_monitor()
            self.assertEqual(missing['status'],'STOPPED');self.assertEqual(missing['mode'],'READ_ONLY')
            self.assertFalse((monitor_root/'monitor.sqlite3').exists())
            store=Store(monitor_root)
            store.save(dict(last_attempt_slot=cycle_now.isoformat(),in_progress=False))
            with store.connect() as db:db.execute("INSERT INTO state(key,value) VALUES('broken','{')")
            self.assertEqual(get_monitor()['status'],'ERROR')
            self.assertEqual(Worker(store,lambda:client,lambda:cycle_now).tick(force=True)['health'],'RUNNING')
            cycle_now+=timedelta(minutes=1);client.now=cycle_now
            self.assertEqual(Worker(store,lambda:client,lambda:cycle_now).tick(force=True)['health'],'RUNNING')
            self.assertEqual(get_monitor()['status'],'RUNNING')
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='REFRESH' AND message='Read-only refresh successful.'").fetchone()[0],2)
            Worker(store,lambda:client,lambda:cycle_now).tick(force=True)
            with store.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='REFRESH' AND message='Read-only refresh successful.'").fetchone()[0],2)
            server.shutdown();thread.join()
        self.assertEqual(Path(wallet.path).read_bytes(),wallet_before)

class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root)
        self.client=FakeClient();self.now=NOW;self.worker=Worker(self.store,lambda:self.client,lambda:self.now)
    def tearDown(self):self.tmp.cleanup()

    def test_unexpected_nvda_failure_is_atomic_clears_marker_and_recovers(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);now=NOW;client=FakeClient(now)
            worker=Worker(store,lambda:client,lambda:now)
            first=worker.tick(force=True)
            prior=first['daily']
            self.assertEqual(first['health'],'RUNNING')
            original=client.bars
            def fail_nvda(ticker,*args):
                if ticker=='NVDA':raise RuntimeError('private credential-like provider detail')
                return original(ticker,*args)
            client.bars=fail_nvda;now+=timedelta(minutes=1);client.now=now
            failed=worker.tick(force=True)
            self.assertEqual(failed['health'],'ERROR');self.assertFalse(failed['in_progress'])
            self.assertEqual(failed['daily'],prior)
            self.assertNotIn('private credential-like',json.dumps(failed))
            with store.connect() as db:
                rows=db.execute("SELECT payload FROM events WHERE kind='ERROR'").fetchall()
                payloads=[json.loads(r[0]) for r in rows]
                diag=next(p for p in payloads if p.get('exception_class')=='RuntimeError')
                self.assertEqual((diag['stage'],diag['ticker']),('history-NVDA','NVDA'))
                self.assertIsNone(diag['http_status'])
                self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='BUY' OR kind='SELL' OR kind='NO CROSS'").fetchone()[0],27)
            client.bars=original;now+=timedelta(minutes=1);client.now=now
            recovered=worker.tick(force=True)
            self.assertEqual(recovered['health'],'RUNNING');self.assertFalse(recovered['in_progress'])
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='RECOVERY'").fetchone()[0],1)
                self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='REFRESH' AND message='Read-only refresh successful.'").fetchone()[0],2)
                before=db.execute('SELECT count(*) FROM events').fetchone()[0]
            worker.tick(force=True)
            with store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],before)

    def test_http_failure_persists_status_and_attempt_without_partial_snapshot(self):
        from dual_ma_ai.alpaca_data import AlpacaHTTPError
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);now=NOW;client=FakeClient(now);worker=Worker(store,lambda:client,lambda:now)
            first=worker.tick(force=True);prior=first['daily'];original=client.bars
            def fail_nvda(ticker,*args):
                if ticker=='NVDA':raise AlpacaHTTPError(403,1,17)
                return original(ticker,*args)
            client.bars=fail_nvda;now+=timedelta(minutes=1);client.now=now
            result=worker.tick(force=True)
            self.assertEqual(result['health'],'ERROR');self.assertFalse(result['in_progress']);self.assertEqual(result['daily'],prior)
            with store.connect() as db:
                payloads=[json.loads(row[0]) for row in db.execute("SELECT payload FROM events WHERE kind='ERROR'")]
            diag=next(p for p in payloads if p.get('http_status')==403)
            self.assertEqual((diag['exception_class'],diag['stage'],diag['ticker'],diag['attempt'],diag['duration_ms']),('AlpacaHTTPError','history-NVDA','NVDA',1,17))

    def test_heartbeat_before_calendar_is_degraded_not_running(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);client=FakeClient()
            original=schedule
            def inspect(now):
                current=store.read()
                self.assertEqual(current['phase'],'calendar')
                self.assertEqual(current['health'],'DEGRADED')
                self.assertNotIn('last_successful_refresh',current)
                return original(now)
            with patch('dual_ma_ai.monitor.schedule',side_effect=inspect):
                result=Worker(store,lambda:client,lambda:NOW).tick()
            self.assertEqual(result['health'],'RUNNING')
            self.assertEqual(result['phase'],'idle')
            self.assertEqual(result['last_successful_refresh'],NOW.isoformat())

    def test_calendar_timeout_is_visible_and_no_credentials_are_read(self):
        from dual_ma_ai.monitor import PhaseTimeout
        with tempfile.TemporaryDirectory() as root:
            store=Store(root)
            with patch('dual_ma_ai.monitor.schedule',side_effect=PhaseTimeout('calendar')),patch('dual_ma_ai.monitor.keychain_credentials') as keychain:
                result=Worker(store,clock=lambda:NOW).tick()
            keychain.assert_not_called()
            self.assertEqual(result['health'],'ERROR')
            self.assertEqual(result['phase'],'calendar')
            self.assertNotIn('last_successful_refresh',result)
            self.assertEqual(status(root,NOW)['status'],'ERROR')
            with store.connect() as db:
                self.assertEqual(db.execute("select count(*) from events where kind='ERROR'").fetchone()[0],1)
                payload=json.loads(db.execute("select payload from events where kind='ERROR'").fetchone()[0])
                self.assertEqual(payload['exception_class'],'PhaseTimeout');self.assertEqual(payload['stage'],'calendar')

    def test_calendar_timeout_preserves_last_candle_without_false_stale_projection(self):
        from dual_ma_ai.monitor import PhaseTimeout
        self.worker.tick();published=self.store.read()['daily'];self.now+=timedelta(minutes=5)
        interim=[]
        def timeout_after_observation(now):
            interim.append(status(self.root,now))
            raise PhaseTimeout('calendar')
        with patch('dual_ma_ai.monitor.schedule',side_effect=timeout_after_observation):
            result=self.worker.tick(force=True)
        self.assertEqual(interim[0]['status'],'DEGRADED')
        self.assertTrue(all(r['status']=='CHECKING' and not r['signals'] for r in interim[0]['daily']))
        self.assertEqual(result['daily'],published)
        projected=status(self.root,self.now)
        self.assertEqual(projected['status'],'ERROR')
        self.assertTrue(all(r['status']=='UNAVAILABLE' and not r['signals'] for r in projected['daily']))

    def test_calendar_recovery_publishes_schedule_and_idle_together(self):
        from dual_ma_ai.monitor import PhaseTimeout
        self.worker.tick();self.now+=timedelta(minutes=5)
        with patch('dual_ma_ai.monitor.schedule',side_effect=PhaseTimeout('calendar')):
            failed=self.worker.tick(force=True)
        self.assertEqual(failed['phase'],'calendar')
        self.client.now=self.now
        recovered=Worker(self.store,lambda:self.client,lambda:self.now).tick(force=True)
        self.assertEqual(recovered['health'],'RUNNING')
        self.assertEqual(recovered['phase'],'idle')
        self.assertIsNone(recovered.get('phase_started_at'))
        view=status(self.root,self.now)
        self.assertEqual(view['status'],'RUNNING')
        self.assertTrue(all(r['status']=='VALIDATED' for r in view['daily']))

    def test_calendar_phase_concurrent_reads_are_consistently_suppressed(self):
        self.worker.tick()
        self.store.save(dict(phase='calendar',phase_started_at=NOW.isoformat(),in_progress=False))
        failures=[];results=[]
        def read_many():
            for _ in range(30):
                view=status(self.root,NOW)
                if view['status']!='DEGRADED' or any(r['signals'] or r['status']!='CHECKING' for r in view['daily']):
                    failures.append(view)
                results.append(view)
        threads=[threading.Thread(target=read_many) for _ in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(failures,[])
        self.assertEqual(len(results),120)

    def test_calendar_projection_during_weekends_and_exchange_holiday(self):
        from dual_ma_ai.monitor import schedule
        for at in (datetime(2026,10,10,16,tzinfo=UTC),
                   datetime(2026,10,11,16,tzinfo=UTC),
                   datetime(2026,11,26,17,tzinfo=UTC)):
            info=schedule(at)
            rows=[dict(ticker=t,status='VALIDATED',feed='iex',source='Alpaca Market Data daily bars',
                       candle=info['expected_candle'],signals=[dict(signal='BUY')]) for t in TICKERS]
            self.store.save(dict(running=True,heartbeat=at.isoformat(),health='RUNNING',phase='calendar',
                                 in_progress=False,schedule=info,daily=rows,last_successful_refresh=at.isoformat()))
            projected=status(self.root,at)
            self.assertEqual(projected['schedule']['expected_candle'],info['expected_candle'])
            self.assertEqual(projected['status'],'DEGRADED')
            self.assertTrue(all(r['status']=='CHECKING' and not r['signals'] for r in projected['daily']))

    def test_credentials_deadline_fails_closed_and_recovers(self):
        from dual_ma_ai.monitor import PhaseTimeout
        with tempfile.TemporaryDirectory() as root:
            store=Store(root)
            worker=Worker(store,lambda:(_ for _ in ()).throw(PhaseTimeout('credentials')),lambda:NOW)
            result=worker.tick()
            self.assertEqual(result['health'],'ERROR');self.assertFalse(result['in_progress'])
            self.assertNotIn('daily',result)
            self.assertNotIn('last_successful_refresh',result)
            later=NOW+timedelta(minutes=1)
            result=Worker(store,lambda:FakeClient(later),lambda:later).tick()
            self.assertEqual(result['health'],'RUNNING')
            with store.connect() as db:self.assertEqual(db.execute("select count(*) from events where kind='RECOVERY'").fetchone()[0],1)

    def test_partial_history_timeout_never_publishes_partial_decisions(self):
        from dual_ma_ai.monitor import PhaseTimeout
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);client=FakeClient();original=client.bars
            def bars(ticker,*args):
                if ticker=='MRVL':raise PhaseTimeout('history-MRVL')
                return original(ticker,*args)
            client.bars=bars
            result=Worker(store,lambda:client,lambda:NOW).tick()
            self.assertEqual(result['health'],'ERROR')
            self.assertNotIn('daily',result)
            with store.connect() as db:self.assertEqual(db.execute("select count(*) from events where kind in ('BUY','SELL','NO CROSS')").fetchone()[0],0)

    def test_running_without_successful_refresh_is_not_healthy(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);store.save(dict(running=True,heartbeat=NOW.isoformat(),health='RUNNING'))
            self.assertEqual(status(root,NOW)['status'],'DEGRADED')
            store.save(dict(heartbeat=(NOW-timedelta(minutes=4)).isoformat()))
            self.assertEqual(status(root,NOW)['status'],'STOPPED')

    def test_actual_deadline_interrupts_blocking_work_and_restores_handler(self):
        import signal,time
        from dual_ma_ai.monitor import deadline,PhaseTimeout
        handler=signal.getsignal(signal.SIGALRM)
        with self.assertRaises(PhaseTimeout):
            with deadline(.02,'calendar'):time.sleep(1)
        self.assertEqual(signal.getsignal(signal.SIGALRM),handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))

    def test_nested_deadline_preserves_outer_budget(self):
        import time,signal
        from dual_ma_ai.monitor import deadline,PhaseTimeout
        started=time.monotonic()
        with self.assertRaises(PhaseTimeout):
            with deadline(.04,'refresh-cycle'):
                with deadline(1,'history-MU'):time.sleep(1)
        self.assertLess(time.monotonic()-started,.5)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))

    def test_isolated_cli_once_and_restart_do_not_touch_production_state(self):
        import subprocess,sys
        with tempfile.TemporaryDirectory() as root:
            script="""import sys
from unittest.mock import patch
import dual_ma_ai.monitor as m
from tests.test_monitor import FakeClient
sys.argv=['monitor','run','--once','--state-root',sys.argv[1]]
with patch.object(m,'keychain_credentials',return_value=m.load_credentials({'ALPACA_API_KEY':'mock','ALPACA_SECRET_KEY':'mock'})),patch.object(m,'Client',return_value=FakeClient()):m.main()
"""
            for _ in range(2):
                result=subprocess.run([sys.executable,'-c',script,root],capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)
            store=Store(root);state=store.read()
            self.assertFalse(state['running'])
            self.assertIn('last_successful_refresh',state)
            log=(Path(root)/'worker.log').read_text()
            self.assertIn('Read-only startup',log);self.assertIn('Stage calendar started',log)
            with store.connect() as db:self.assertEqual(db.execute("select count(*) from events where kind='NO CROSS'").fetchone()[0],27)

    def test_actual_duplicate_process_and_crash_restart_keep_state_isolated(self):
        import subprocess,sys,time
        script="""import sys
from unittest.mock import patch
import dual_ma_ai.monitor as m
from tests.test_monitor import FakeClient
sys.argv=['monitor','run','--state-root',sys.argv[1]]+sys.argv[2:]
with patch.object(m,'keychain_credentials',return_value=m.load_credentials({'ALPACA_API_KEY':'mock','ALPACA_SECRET_KEY':'mock'})),patch.object(m,'Client',return_value=FakeClient()):m.main()
"""
        with tempfile.TemporaryDirectory() as root:
            process=subprocess.Popen([sys.executable,'-c',script,root],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                until=time.monotonic()+20
                while time.monotonic()<until:
                    path=Path(root)/'monitor.sqlite3'
                    if path.exists():
                        try:
                            state=Store(root).read()
                            if state.get('last_successful_refresh') and not state.get('in_progress'):break
                        except sqlite3.Error:pass
                    if process.poll() is not None:self.fail('Isolated worker exited before readiness')
                    time.sleep(.05)
                else:self.fail('Isolated worker did not become ready')
                store=Store(root)
                with store.connect() as db:before=db.execute('select count(*) from events').fetchone()[0]
                duplicate=subprocess.run([sys.executable,'-c',script,root,'--once'],capture_output=True,text=True,timeout=10)
                self.assertEqual(duplicate.returncode,2)
                with store.connect() as db:self.assertEqual(db.execute('select count(*) from events').fetchone()[0],before)
                store.save(dict(in_progress=True))
                process.kill();process.communicate(timeout=5)
                restarted=subprocess.run([sys.executable,'-c',script,root,'--once'],capture_output=True,text=True,timeout=20)
                self.assertEqual(restarted.returncode,0,restarted.stderr)
                self.assertFalse(store.read()['in_progress'])
                self.assertFalse(store.read()['running'])
                with store.connect() as db:
                    self.assertEqual(db.execute("select count(*) from events where kind='RECOVERY'").fetchone()[0],1)
                    self.assertEqual(db.execute("select count(*) from events where kind='NO CROSS'").fetchone()[0],27)
            finally:
                if process.poll() is None:process.kill()
                process.communicate(timeout=5)

    def test_empty_daily_snapshot_retries_off_hours_after_transport_failure(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);now=datetime(2026,10,10,5,tzinfo=UTC)
            store.save(dict(daily_expected='2026-10-09',last_daily_attempt=(now-timedelta(minutes=6)).isoformat(),health='ERROR'))
            client=FakeClient(now)
            result=Worker(store,lambda:client,lambda:now).tick()
            self.assertEqual(result['health'],'RUNNING')
            self.assertEqual(len(result['daily']),9)
            self.assertFalse(any(call[0]=='/v2/stocks/quotes/latest' for call in client.calls))

    def test_new_daily_expected_does_not_make_old_snapshot_ready(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);client=FakeClient()
            Worker(store,lambda:client,lambda:NOW).tick()
            now=datetime(2026,10,10,5,tzinfo=UTC)
            store.save(dict(daily_expected='2026-10-09',last_daily_attempt=(now-timedelta(minutes=6)).isoformat()))
            result=Worker(store,lambda:FakeClient(now),lambda:now).tick()
            self.assertTrue(all(r['candle']=='2026-10-09' for r in result['daily']))
