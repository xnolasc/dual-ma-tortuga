"""Local HTTP integration tests, exclusively against temporary SQLite state."""
import concurrent.futures
import hashlib
import http.client
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from dual_ma_ai.dashboard import Dashboard, Handler, make_server, wallet_view, data_kind
from dual_ma_ai.wallet import Wallet
from dual_ma_ai.market_data import digest


def _cpu_bound_catalog(_reports_path):
    value=0
    for i in range(12_000_000):value=((value<<5)^i^(value>>3))&0xFFFFFFFF
    return {'reports':[{'worker_pid':os.getpid(),'checksum':value}],'warnings':[]}


def _cpu_bound_signal(_cache_path,_alpaca_path,provider,feed):
    value=1
    for i in range(12_000_000):value=((value*33)^i)&0xFFFFFFFF
    return {'provider':provider,'feed':feed,'rows':[{'checksum':value}],'execution_enabled':False}


def _cpu_bound_coverage(path,feed):
    value=1
    for i in range(12_000_000):value=((value*33)^i)&0xFFFFFFFF
    return {'feed':feed,'rows':[{'path':path,'checksum':value}]}


def _slow_research_value(value):
    time.sleep(.15)
    return {'value':value,'worker_pid':os.getpid()}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.wallet = Wallet(self.root / 'wallet.sqlite3')
        self.app = Dashboard(self.wallet, self.root / 'reports', self.root / 'cache', self.root / 'alpaca',
                             monitor_root=self.root / 'monitor')
        self.server = make_server(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f'127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None, method=None, timeout=5):
        client_started=time.monotonic_ns()
        c = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=timeout)
        h = {'Host': self.host,'X-Request-ID':f'test-{threading.get_ident()}-{client_started}',
             'X-Client-Start-Mono-Ns':str(client_started)}
        if body is not None:
            h.update({'Content-Type': 'application/json', 'Origin': 'http://' + self.host,
                      'X-CSRF-Token': self.app.csrf})
        h.update(headers or {})
        c.request(method or ('POST' if body is not None else 'GET'), path,
                  json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        status, response_headers, raw = r.status, dict(r.getheaders()), r.read()
        c.close()
        return status, response_headers, json.loads(raw) if response_headers.get('Content-Type') == 'application/json' else raw

    def prepared(self, amount='10.00', operation='deposit', key='test-idempotency-key-001'):
        status, _, result = self.request('/api/funding/prepare', dict(amount=amount, operation=operation, key=key))
        self.assertEqual(status, 200)
        return dict(**result, confirmed=True)

    def test_rendering_and_local_assets(self):
        for path, marker in [('/', b'Portfolio overview'), ('/app.js', b'crypto.randomUUID'), ('/style.css', b'@media')]:
            status, h, raw = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(marker, raw)
            self.assertIn("frame-ancestors 'none'", h['Content-Security-Policy'])
            self.assertEqual(h['Cache-Control'], 'no-store')
            self.assertNotIn('Access-Control-Allow-Origin', h)
        html = self.request('/')[2].decode()
        for name in ('SMA 50 / 200', 'SMA 20 / 50', 'EMA 20 / 50', 'SMA 10 / 30', 'All-in', '15% initial capital', 'ATR-based sizing', 'READ ONLY'):
            self.assertIn(name, html)
        self.assertNotIn('https://', html)

    def test_actual_wallet_accounting_and_positions(self):
        self.wallet.deposit('200.00', 'initial-deposit')
        self.wallet.buy('MU', 2, '100.00', 'buy')
        self.wallet.mark('MU', '110.00', 'mark')
        status, _, view = self.request('/api/wallet')
        self.assertEqual(status, 200)
        self.assertEqual(view['equity'], '2220.00')
        self.assertEqual(view['cash'], '2000.00')
        self.assertEqual(view['net_contributions'], '2200.00')
        self.assertEqual(view['net_deposits'], '200.00')
        self.assertEqual(view['trading_pnl'], '20.00')
        self.assertEqual(view['positions'][0]['mark'], '110.00')
        self.assertEqual(view['revision'], view['events'][0]['id'])
        self.assertEqual(view['equity'], self.wallet.snapshot()['equity'])

    def test_confirmation_duplicate_and_restart(self):
        body = self.prepared()
        first = self.request('/api/funding/confirm', body)
        second = self.request('/api/funding/confirm', body)
        self.assertEqual(first[0], 200)
        self.assertEqual(first[2]['event']['id'], second[2]['event']['id'])
        self.assertEqual(first[2]['wallet']['cash'], '2010.00')
        self.assertEqual(first[2]['wallet']['trading_pnl'], '0.00')
        self.assertEqual(Wallet(self.wallet.path).snapshot()['cash'], '2010.00')

    def test_concurrent_duplicate_deposits(self):
        body = self.prepared()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.request('/api/funding/confirm', body), range(8)))
        self.assertTrue(all(r[0] == 200 for r in results))
        self.assertEqual(len({r[2]['event']['id'] for r in results}), 1)
        self.assertEqual(self.wallet.snapshot()['cash'], '2010.00')

    def test_concurrent_withdrawals_cannot_overdraw(self):
        bodies = [self.prepared('1500.00', 'withdraw', 'withdrawal-request-'+str(i)) for i in range(2)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda b: self.request('/api/funding/confirm', b), bodies))
        self.assertEqual(sorted(r[0] for r in results), [200, 400])
        self.assertEqual(self.wallet.snapshot()['cash'], '500.00')
        self.assertEqual(self.wallet.snapshot()['trading_pnl'], '0.00')

    def test_conflicting_duplicate_rejected(self):
        body = self.prepared()
        self.request('/api/funding/confirm', body)
        conflict = self.prepared('11.00')
        self.assertEqual(self.request('/api/funding/confirm', conflict)[0], 400)
        self.assertEqual(self.wallet.snapshot()['cash'], '2010.00')

    def test_confirmation_tamper_missing_and_expired(self):
        body = self.prepared()
        for changes in ({'confirmed': False}, {'confirmation': 'bad'}, {'amount': '100.00'}, {'expires': 0}, {'operation': 'withdraw'}, {'key': 'another-request-key-001'}):
            self.assertEqual(self.request('/api/funding/confirm', dict(body, **changes))[0], 400)
        self.assertEqual(self.wallet.snapshot()['cash'], '2000.00')

    def test_invalid_amounts(self):
        for amount in ('0', '-1', 'NaN', 'Infinity', '1.001', '1e3', '', 10.0, True, '1000000000001.00'):
            self.assertEqual(self.request('/api/funding/prepare', dict(amount=amount, operation='deposit', key='test-invalid-key-001'))[0], 400)
        self.assertEqual(len(self.wallet.history()), 1)

    def test_origin_csrf_host_protection(self):
        body = self.prepared()
        for h in ({'Origin': 'https://evil.example'}, {'X-CSRF-Token': ''}, {'Host': 'evil.example'}, {'Origin': 'null'}):
            self.assertEqual(self.request('/api/funding/confirm', body, h)[0], 403)
        self.assertEqual(self.request('/api/session', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(len(self.wallet.history()), 1)

    def test_no_execution_or_arbitrary_file_endpoints(self):
        before = self.wallet.snapshot()
        for path in ('/api/buy', '/api/sell', '/api/execute', '/api/schedule'):
            self.assertEqual(self.request(path, {})[0], 404)
        self.assertNotEqual(self.request('/api/reports/../../wallet.sqlite3')[0], 200)
        self.assertEqual(self.request('/../dualma_trader.py')[0], 404)
        self.assertEqual(before, self.wallet.snapshot())

    def test_unavailable_signals_are_not_fabricated(self):
        status, _, result = self.request('/api/signals')
        self.assertEqual(status, 200)
        self.assertFalse(result['execution_enabled'])
        self.assertEqual(len(result['rows']), 9)
        self.assertTrue(all(r['status'] == 'UNAVAILABLE' for r in result['rows']))

    def test_client_disconnect_during_response_is_absorbed(self):
        class BrokenPipeWriter:
            def write(self, _body):
                raise BrokenPipeError('simulated browser disconnect')
        class DisconnectedHandler(Handler):
            def send_response(self, *_args): pass
            def send_header(self, *_args): pass
            def end_headers(self): pass
        handler=object.__new__(DisconnectedHandler)
        handler.wfile=BrokenPipeWriter()
        # A canceled browser request must not escape the request handler or
        # trigger a second error response on the already-closed socket.
        handler.send(200,{'status':'DEGRADED'})

    def test_identical_expensive_reads_are_single_flight_cached(self):
        entered=threading.Event()
        def slow_catalog():
            entered.set();time.sleep(.08)
            return {'reports':[],'warnings':[]}
        self.app.catalog=slow_catalog
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.app.cached_catalog(),range(8)))
        self.assertTrue(all(result=={'reports':[],'warnings':[]} for result in results))
        self.assertTrue(entered.is_set())

    def test_duplicate_coverage_http_requests_share_one_slow_cold_computation(self):
        calls=[];guard=threading.Lock();original=self.app._isolated
        def slow_isolated(fn,*args,stage=None):
            with guard:calls.append(stage)
            time.sleep(.15)
            return {'feed':'iex','rows':[]}
        self.app._isolated=slow_isolated
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results=list(pool.map(lambda _:self.request('/api/coverage?feed=iex'),range(8)))
        finally:self.app._isolated=original
        self.assertEqual([status for status,_,_ in results],[200]*8)
        self.assertTrue(all(body==results[0][2] for _,_,body in results))
        self.assertEqual(calls,['alpaca.coverage'])

    def test_shared_research_flight_outlives_old_five_second_lock_window(self):
        # Coverage can take materially longer than five seconds on a cold cache.
        # Identical callers wait on the same Future within the normal 20s budget.
        calls=[];entered=threading.Event()
        def slow():
            calls.append(1);entered.set();time.sleep(5.1);return {'rows':['same']}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            first=pool.submit(self.app._memoized,'test.coverage','iex',(),slow,30)
            self.assertTrue(entered.wait(2))
            rest=[pool.submit(self.app._memoized,'test.coverage','iex',(),slow,30) for _ in range(5)]
            self.assertEqual(first.result(timeout=7),{'rows':['same']})
            self.assertEqual([f.result(timeout=2) for f in rest],[{'rows':['same']}]*5)
        self.assertEqual(calls,[1])

    def test_failed_shared_research_flight_is_cleared_and_retryable(self):
        from dual_ma_ai.dashboard import DataError
        calls=[];entered=threading.Event();release=threading.Event()
        def failing():
            calls.append('fail');entered.set();release.wait(3);raise DataError('injected worker failure')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            owner=pool.submit(self.app._memoized,'test.coverage','iex',(),failing)
            self.assertTrue(entered.wait(2))
            duplicate=pool.submit(self.app._memoized,'test.coverage','iex',(),failing)
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                with self.app._memo_guard:
                    if next(iter(self.app._memo_flights.values()))['waiters']==1:break
                time.sleep(.005)
            self.assertEqual(next(iter(self.app._memo_flights.values()))['waiters'],1)
            release.set()
            for future in (owner,duplicate):
                with self.assertRaises(DataError):future.result(timeout=3)
        self.assertFalse(self.app._memo_flights)
        value=self.app._memoized('test.coverage','iex',(),lambda:(calls.append('retry') or {'ok':True}))
        self.assertEqual(value,{'ok':True})
        self.assertEqual(calls,['fail','retry'])

    def test_shared_flight_waiter_deadline_and_capacity_are_bounded(self):
        from concurrent.futures import Future
        from dual_ma_ai.dashboard import DashboardBusy,MAX_MEMO_FLIGHTS,MAX_MEMO_WAITERS
        calls=[];entered=threading.Event();release=threading.Event()
        def slow():
            calls.append(1);entered.set();release.wait(3);return {'ready':True}
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            owner=pool.submit(self.app._memoized,'deadline','iex',(),slow)
            self.assertTrue(entered.wait(2))
            with patch('dual_ma_ai.dashboard.RESEARCH_REQUEST_TIMEOUT_SECONDS',.02):
                with self.assertRaises(DashboardBusy):
                    self.app._memoized('deadline','iex',(),slow)
            release.set()
            self.assertEqual(owner.result(timeout=2),{'ready':True})
        self.assertEqual(calls,[1])
        with self.app._memo_guard:
            self.app._memo_flights.clear()
            for index in range(MAX_MEMO_FLIGHTS):
                self.app._memo_flights[(('occupied',index),())]={'future':Future(),'waiters':0}
        with self.assertRaises(DashboardBusy):self.app._memoized('overflow','key',(),lambda:None)
        with self.app._memo_guard:
            self.app._memo_flights.clear()
            self.app._memo_flights[(('waiters','key'),())]={'future':Future(),'waiters':MAX_MEMO_WAITERS}
        with self.assertRaises(DashboardBusy):self.app._memoized('waiters','key',(),lambda:None)
        with self.app._memo_guard:self.app._memo_flights.clear()

    def test_concurrent_signal_calculation_is_coalesced_and_cache_invalidates(self):
        calls=[];guard=threading.Lock()
        def slow_panel(provider,feed):
            with guard:calls.append((provider,feed))
            time.sleep(.08)
            return {'provider':provider,'feed':feed,'rows':[]}
        self.app.signal_panel=slow_panel
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.app.cached_signal_panel('alpaca','iex'),range(8)))
        self.assertEqual(len(calls),1)
        self.assertTrue(all(result==results[0] for result in results))
        self.app.alpaca.root.mkdir(parents=True)
        (self.app.alpaca.root/'NVDA-iex-fixture.json').write_text('{}')
        self.app.cached_signal_panel('alpaca','iex')
        self.assertEqual(len(calls),2)

    def test_monitor_endpoint_stays_responsive_during_slow_wallet_and_reports(self):
        # Warm calendar construction so this measures request isolation rather
        # than the exchange-calendar library's one-time import cost.
        self.assertEqual(self.request('/api/monitor')[0],200)
        release=threading.Event();wallet_started=threading.Event();reports_started=threading.Event();signals_started=threading.Event()
        def slow_wallet(_wallet):
            wallet_started.set();release.wait(5);return {'cash':'2000.00'}
        original_wallet_view=__import__('dual_ma_ai.dashboard',fromlist=['wallet_view']).wallet_view
        original_catalog=self.app.catalog
        def slow_catalog():
            reports_started.set();release.wait(5);return {'reports':[],'warnings':[]}
        def slow_signals(provider='alpaca',feed='iex'):
            signals_started.set();release.wait(5)
            return {'provider':provider,'feed':feed,'rows':[],'execution_enabled':False}
        import dual_ma_ai.dashboard as dashboard_module
        dashboard_module.wallet_view=slow_wallet
        self.app.catalog=slow_catalog
        self.app.signal_panel=slow_signals
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                wallet=pool.submit(self.request,'/api/wallet')
                reports=pool.submit(self.request,'/api/reports')
                signals=pool.submit(self.request,'/api/signals')
                self.assertTrue(wallet_started.wait(2))
                self.assertTrue(reports_started.wait(2))
                self.assertTrue(signals_started.wait(2))
                # Both independent slow handlers are in-flight; monitor still
                # reads its own temporary DB and completes on another worker.
                before=time.monotonic();response=self.request('/api/monitor');elapsed=time.monotonic()-before
                self.assertEqual(response[0],200)
                self.assertLess(elapsed,2.0)
                self.assertFalse(wallet.done())
                self.assertFalse(reports.done())
                self.assertFalse(signals.done())
                release.set()
                self.assertEqual(wallet.result(timeout=5)[0],200)
                self.assertEqual(reports.result(timeout=5)[0],200)
                self.assertEqual(signals.result(timeout=5)[0],200)
        finally:
            release.set();dashboard_module.wallet_view=original_wallet_view;self.app.catalog=original_catalog

    def test_monitor_p95_and_peak_under_cpu_bound_research_load(self):
        from datetime import datetime,timezone,timedelta
        from dual_ma_ai.monitor import Store,event
        now=datetime.now(timezone.utc)
        Store(self.root/'monitor').save(dict(running=True,heartbeat=now.isoformat(),health='DEGRADED',
            connection_health='CONNECTED',phase='idle',in_progress=False,last_successful_refresh=now.isoformat(),
            schedule={'expected_candle':now.date().isoformat(),'regular_hours':False,
                      'next_daily_refresh':(now+timedelta(days=1)).isoformat()},
            daily=[dict(ticker=t,status='BLOCKED' if t in ('SPCX','AAOI') else 'VALIDATED',
                        candle=now.date().isoformat(),signals=[dict(signal='BUY')] if t in ('SPCX','AAOI') else [])
                   for t in ('SPCX','LITE','MRVL','TSM','CIEN','COHR','MU','NVDA','AAOI')]),
            [event(('load-test',now.isoformat()),now,'REFRESH','isolated cpu-load fixture')])
        # Match the failed production shape exactly: 48 simultaneous monitor
        # GETs plus 4 each reports, IEX signals, and coverage. Empty temporary
        # research stores make this deterministic while retaining real routing,
        # process-pool admission, socket acceptance and response serialization.
        paths=(['/api/monitor']*48+['/api/reports']*4+
               ['/api/signals?provider=alpaca&feed=iex']*4+['/api/coverage?feed=iex']*4)
        def batch(port):
            gate=threading.Barrier(len(paths)+1)
            def timed(path):
                gate.wait(timeout=10);start=time.monotonic_ns()
                c=http.client.HTTPConnection('127.0.0.1',port,timeout=25)
                h={'Host':f'127.0.0.1:{port}','X-Request-ID':f'load-{threading.get_ident()}-{start}',
                   'X-Client-Start-Mono-Ns':str(start)}
                try:
                    c.request('GET',path,headers=h);r=c.getresponse();raw=r.read()
                    body=json.loads(raw) if 'application/json' in r.getheader('Content-Type','') else raw
                    return path,r.status,(time.monotonic_ns()-start)/1_000_000,body
                except Exception as exc:return path,None,(time.monotonic_ns()-start)/1_000_000,type(exc).__name__
                finally:c.close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(paths)) as clients:
                pending=[clients.submit(timed,path) for path in paths]
                gate.wait(timeout=10)
                return [f.result(timeout=35) for f in pending]

        # A/B the old five-connection listener backlog against the corrected
        # 128-entry bounded kernel backlog with identical request shapes.
        import socketserver
        from dual_ma_ai.dashboard import DashboardHTTPServer,Handler
        class FiveConnectionBacklog(DashboardHTTPServer):request_queue_size=5
        legacy=FiveConnectionBacklog(('127.0.0.1',0),Handler);legacy.app=self.app
        legacy.daemon_threads=True
        legacy_thread=threading.Thread(target=socketserver.BaseServer.serve_forever,args=(legacy,),daemon=True)
        legacy_thread.start()
        try:
            old_results=batch(legacy.server_port)
            legacy_samples=list(legacy._request_samples)
        finally:
            legacy.shutdown();socketserver.TCPServer.server_close(legacy);legacy_thread.join(timeout=5)
        with self.app._memo_guard:self.app._memo.clear()
        with self.app._compute_pool_lock:self.app._compute_samples.clear()
        results=batch(self.server.server_port)
        monitor=[r for r in results if r[0]=='/api/monitor']
        heavy=[r for r in results if r[0]!='/api/monitor']
        samples=[r[2] for r in monitor]
        p95=sorted(samples)[int(len(samples)*.95)-1]
        print(f'FAILED_SHAPE_REPRO samples={len(samples)} p95_ms={p95:.2f} max_ms={max(samples):.2f} heavy_statuses={[x[1] for x in heavy]}')
        old_monitor=sorted(x[2] for x in old_results if x[0]=='/api/monitor')
        new_monitor=sorted(x[2] for x in monitor)
        old_accept=sorted(x['client_to_accept_ms'] for x in legacy_samples if x['route']=='/api/monitor' and x['client_to_accept_ms'] is not None)
        old_p95=old_monitor[int(len(old_monitor)*.95)-1];new_p95=new_monitor[int(len(new_monitor)*.95)-1]
        print(f'BACKLOG_AB old_queue=5 monitor_p95_ms={old_p95:.2f} max_ms={max(old_monitor):.2f} client_to_accept_p95_ms={old_accept[int(len(old_accept)*.95)-1]:.2f} new_queue=128 monitor_p95_ms={new_p95:.2f} max_ms={max(new_monitor):.2f}')
        self.assertGreater(old_p95,new_p95)
        self.assertLess(p95,2000.0,{'p95_ms':p95,'samples':samples})
        self.assertLess(max(samples),5000.0,{'max_ms':max(samples),'samples':samples})
        self.assertEqual(len(monitor),48)
        self.assertTrue(all(code==200 and body['status']=='DEGRADED' for _,code,_,body in monitor))
        self.assertTrue(all(code in (200,503) for _,code,_,_ in heavy))
        self.assertTrue(all(elapsed<5_000 for _,_,elapsed,_ in heavy))
        blocked=[r for _,_,_,view in monitor for r in view['daily'] if r.get('ticker') in ('SPCX','AAOI')]
        self.assertTrue(blocked)
        self.assertTrue(all(r['status']=='BLOCKED' and not r['signals'] for r in blocked))
        with sqlite3.connect(self.root/'monitor'/'monitor.sqlite3') as db:
            count,unique=db.execute('select count(*),count(distinct id) from events').fetchone()
        self.assertEqual((count,unique),(1,1))
        timings=self.server._request_samples
        monitor_timings=[m for m in timings if m['route']=='/api/monitor']
        self.assertGreaterEqual(len(monitor_timings),48)
        self.assertTrue(all(m['client_to_accept_ms'] is None or m['client_to_accept_ms']<2000 for m in monitor_timings))
        accept_ms=sorted(m['client_to_accept_ms'] for m in monitor_timings if m['client_to_accept_ms'] is not None)
        dispatch_ms=sorted(m['thread_dispatch_ms'] for m in monitor_timings)
        handler_ms=sorted(m['handler_ms'] for m in monitor_timings)
        print(f'ACCEPT_STAGE samples={len(accept_ms)} client_to_accept_p95_ms={accept_ms[int(len(accept_ms)*.95)-1]:.2f} max_ms={max(accept_ms):.2f} thread_dispatch_p95_ms={dispatch_ms[int(len(dispatch_ms)*.95)-1]:.2f} handler_p95_ms={handler_ms[int(len(handler_ms)*.95)-1]:.2f}')
        pool=self.app._compute_samples
        if pool:
            waits=sorted(x['pool_wait_ms'] for x in pool);computes=sorted(x['compute_ms'] for x in pool)
            print(f'PROCESS_POOL_STAGE samples={len(pool)} queue_wait_p95_ms={waits[int(len(waits)*.95)-1]:.2f} queue_wait_max_ms={max(waits):.2f} compute_p95_ms={computes[int(len(computes)*.95)-1]:.2f} compute_max_ms={max(computes):.2f} routes={[x["stage"] for x in pool]}')

    def test_process_pool_is_bounded_and_records_wait_and_compute(self):
        from datetime import datetime,timezone
        from dual_ma_ai.monitor import Store
        now=datetime.now(timezone.utc).isoformat()
        Store(self.root/'monitor').save(dict(running=True,heartbeat=now,health='DEGRADED',
            connection_health='CONNECTED',phase='idle',in_progress=False,daily=[]))
        with self.assertLogs('dual_ma_ai.dashboard',level='INFO') as captured:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                first=pool.submit(self.app._isolated,_cpu_bound_catalog,str(self.root/'reports'),stage='test.catalog')
                second=pool.submit(self.app._isolated,_cpu_bound_signal,str(self.root/'cache'),str(self.root/'alpaca'),'alpaca','iex',stage='test.signals')
                third=pool.submit(self.app._isolated,_cpu_bound_coverage,str(self.root/'alpaca'),'iex',stage='test.coverage')
                fourth=pool.submit(self.app._isolated,_cpu_bound_signal,str(self.root/'cache'),str(self.root/'alpaca'),'alpaca','sip',stage='test.signals.sip')
                deadline=time.monotonic()+5
                while self.app._compute_slots._value != 0 and time.monotonic()<deadline:time.sleep(.01)
                before=time.monotonic();status,_,view=self.request('/api/monitor');elapsed=time.monotonic()-before
                self.assertEqual(status,200);self.assertEqual(view['status'],'DEGRADED');self.assertLess(elapsed,2)
                from dual_ma_ai.dashboard import DashboardBusy
                with self.assertRaises(DashboardBusy) as raised:
                    self.app._isolated(_cpu_bound_catalog,str(self.root/'reports')+'-overflow',stage='test.overflow')
                self.assertEqual(first.result(timeout=25)['reports'][0]['worker_pid']!=os.getpid(),True)
                self.assertEqual(second.result(timeout=25)['execution_enabled'],False)
                self.assertEqual(third.result(timeout=25)['feed'],'iex')
                self.assertEqual(fourth.result(timeout=25)['feed'],'sip')
        output='\n'.join(captured.output)
        self.assertIn('process_pool_wait_ms=',output);self.assertIn('compute_ms=',output)

    def test_process_pool_same_key_requests_share_one_future(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.app._isolated,_cpu_bound_catalog,str(self.root/'reports'),stage='test.singleflight')
            deadline=time.monotonic()+5
            while not self.app._compute_jobs and time.monotonic()<deadline:time.sleep(.01)
            second=pool.submit(self.app._isolated,_cpu_bound_catalog,str(self.root/'reports'),stage='test.singleflight')
            a=first.result(timeout=25);b=second.result(timeout=25)
        self.assertEqual(a,b)
        self.assertNotEqual(a['reports'][0]['worker_pid'],os.getpid())

    def test_research_timeout_returns_503_and_keeps_job_reusable(self):
        import dual_ma_ai.dashboard as dashboard_module
        def slow_catalog():return self.app._isolated(_slow_research_value,'catalog',stage='reports.catalog')
        self.app._cached_catalog_compute=slow_catalog
        with patch.object(dashboard_module,'RESEARCH_REQUEST_TIMEOUT_SECONDS',0.001):
            started=time.monotonic();status,_,body=self.request('/api/reports',timeout=3);elapsed=time.monotonic()-started
        self.assertEqual(status,503);self.assertIn('busy',body['error'].lower());self.assertLess(elapsed,2)
        key=('_slow_research_value',('catalog',))
        future=self.app._compute_jobs[key][0]
        self.assertEqual(future.result(timeout=25)[1]['value'],'catalog')
        with patch.object(dashboard_module,'RESEARCH_REQUEST_TIMEOUT_SECONDS',2):
            status,_,result=self.request('/api/reports',timeout=3)
        self.assertEqual(status,200);self.assertEqual(result['worker_pid']!=os.getpid(),True)

    def _timed_monitor(self):
        started=time.monotonic();status,_,view=self.request('/api/monitor',timeout=5)
        return (time.monotonic()-started)*1000,(status,view)

    def test_request_logs_processing_serialization_and_socket_stages(self):
        with self.assertLogs('dual_ma_ai.dashboard',level='INFO') as captured:
            self.assertEqual(self.request('/api/session')[0],200)
            self.assertEqual(self.request('/api/funding/prepare',dict(operation='deposit',amount='1.00',key='timing-test-key-0001'))[0],200)
        line='\n'.join(captured.output)
        for field in ('method=GET route=/api/session','method=POST route=/api/funding/prepare',
                      'client_to_accept_ms=','thread_dispatch_ms=','accept_to_handler_ms=',
                      'handler_ms=','serialization_ms=','socket_write_ms='):
            self.assertIn(field,line)

    def test_monitor_sqlite_lock_wait_is_bounded_and_fails_closed(self):
        import sqlite3
        from dual_ma_ai.monitor import Store
        Store(self.root/'monitor')
        lock=sqlite3.connect(self.root/'monitor'/'monitor.sqlite3',timeout=1,isolation_level=None)
        lock.execute('BEGIN EXCLUSIVE')
        lock.execute("INSERT INTO state(key,value) VALUES('pending','true')")
        try:
            before=time.monotonic()
            status,_,view=self.request('/api/monitor',timeout=9)
            elapsed=time.monotonic()-before
            self.assertEqual(status,200)
            self.assertEqual(view['status'],'ERROR')
            self.assertLess(elapsed,1.0)
        finally:
            lock.rollback();lock.close()

    def test_reports_provenance_and_wallet_isolation(self):
        before = Path(self.wallet.path).read_bytes()
        self.app.reports.mkdir()
        report = dict(mode='PAPER_BACKTEST_COMPARISON', synthetic=True, evaluation_end='2026-10-09', warnings=[], comparisons=[dict(years=1, status='available', runs=[dict(provenance={'MU': {'source': 'SYNTHETIC fixture'}})])])
        identity = digest(report)
        (self.app.reports / (identity+'.json')).write_text(json.dumps(report))
        result = self.request('/api/reports')[2]
        self.assertEqual(result['reports'][0]['data_kind'], 'SYNTHETIC')
        self.assertEqual(self.request('/api/reports/'+identity)[2]['data_kind'], 'SYNTHETIC')
        report['synthetic'] = False
        self.assertEqual(data_kind(report), 'SYNTHETIC')
        report['comparisons'][0]['runs'][0]['provenance']['MU']['source'] = 'unknown'
        self.assertEqual(data_kind(report), 'UNAVAILABLE / UNVERIFIED')
        (self.app.reports / (identity+'.json')).write_text(json.dumps(report))
        self.assertEqual(self.request('/api/reports/'+identity)[0], 400)
        self.assertEqual(self.request('/api/reports')[2]['reports'], [])
        self.assertEqual(Path(self.wallet.path).read_bytes(), before)

    def test_consistent_snapshot_during_writes(self):
        def deposits():
            for i in range(30):
                self.wallet.deposit('1.00', 'snapshot-deposit-'+str(i))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(deposits)
            for _ in range(30):
                v = wallet_view(self.wallet)
                self.assertEqual(v['cash'], format(v['events'][0]['cash_after']/100, '.2f'))
                self.assertEqual(v['net_contributions'], v['cash'])
                self.assertEqual(v['trading_pnl'], '0.00')
            writer.result()

    def test_attach_existing_does_not_write_database(self):
        before = Path(self.wallet.path).read_bytes()
        attached = Wallet.open_existing(self.wallet.path)
        self.assertEqual(wallet_view(attached)['equity'], '2000.00')
        self.assertEqual(Path(self.wallet.path).read_bytes(), before)
        with self.assertRaises(ValueError):
            Wallet.open_existing(self.root / 'missing.sqlite3')

    def test_signal_panel_uses_completed_candles_and_reports_freshness(self):
        from dataclasses import replace
        from datetime import timedelta
        from test_research import dataset, CAL
        from dual_ma_ai.market_data import SOURCE
        from dual_ma_ai.wallet import TICKERS
        import dual_ma_ai.dashboard as dashboard_module
        ds = replace(dataset([10] * 200 + [9] * 10 + [12] * 10), source=SOURCE)
        as_of = ds.bars[-2].closed_at
        batch_calls=[];original=dashboard_module.signals_many
        def counted_batch(dataset_value,strategies,*,as_of):
            batch_calls.append(dataset_value.ticker)
            return original(dataset_value,strategies,as_of=as_of)
        class Clock:
            @staticmethod
            def now(tz):
                return as_of
        with patch.object(self.app.cache, 'latest', return_value=(ds, Path('fixture'))), patch('dual_ma_ai.dashboard.datetime', Clock), patch('dual_ma_ai.dashboard.USCalendar', return_value=CAL), patch('dual_ma_ai.dashboard.signals_many', counted_batch):
            view = self.app.signal_panel()
        self.assertEqual(len(batch_calls),len(TICKERS))
        row = view['rows'][0]
        self.assertEqual(row['latest_completed_candle'], as_of.isoformat())
        self.assertEqual(row['source'], SOURCE)
        self.assertFalse(row['stale'])
        self.assertEqual([s['name'] for s in row['strategies']], ['SMA_50_200', 'SMA_20_50', 'EMA_20_50'])
        self.assertTrue(all(s['last_crossover'] is None or s['last_crossover']['day'] <= str(ds.bars[-2].day) for s in row['strategies']))
        with patch.object(self.app.cache, 'latest', return_value=(ds, Path('fixture'))), patch('dual_ma_ai.dashboard.USCalendar', return_value=CAL):
            self.assertTrue(self.app.signal_panel()['rows'][0]['stale'])
