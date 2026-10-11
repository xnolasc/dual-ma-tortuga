import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from dual_ma_ai.alert_policy import PolicySet
from dual_ma_ai.alert_evaluator import AlertEvaluator
from dual_ma_ai.alert_store import AlertStore
from dual_ma_ai.dashboard import Dashboard, make_server
from dual_ma_ai.market_data import USCalendar
from dual_ma_ai.alpaca_data import POLICY, SOURCE, daily_ready_at
from dual_ma_ai.strategy import PRESETS
from dual_ma_ai.wallet import Wallet, TICKERS

UTC=timezone.utc


def current_snapshot(now):
    calendar=USCalendar(now.date()-timedelta(days=21),now.date()+timedelta(days=21))
    completed=[d for d in calendar.sessions(now.date()-timedelta(days=21),now.date()) if daily_ready_at(d)<=now]
    candle=max(completed);closed=calendar.bounds(candle)[1].astimezone(UTC).isoformat()
    usable=daily_ready_at(candle).isoformat();fetched=now.isoformat();day=candle.isoformat();daily=[]
    for ticker in TICKERS:
        daily.append(dict(ticker=ticker,status='VALIDATED',feed='iex',source=SOURCE,candle=day,
            closed_at=closed,usable_at=usable,fetched_at=fetched,payload_sha256=(ticker.encode().hex().ljust(64,'a')[:64]),
            adjustment_policy=POLICY,validation=dict(status='VALIDATED',feed='iex',data_kind='REAL',
                sessions=260,latest_completed_candle=closed,latest_usable_at=usable,
                missing_internal_sessions=0,excluded_incomplete=0,warnings=[]),
            signals=[dict(strategy=s.name,signal=('BUY' if ticker=='MU' and s.name==PRESETS[0].name else 'NO CROSS'),
                candle=day,usable_at=usable,evaluated_at=fetched,explanation='Browser fixture only.') for s in PRESETS]))
    return dict(status='RUNNING',mode='READ_ONLY',paper_execution_enabled=False,in_progress=False,
        schedule=dict(expected_candle=day,calendar='XNYS'),daily=daily,heartbeat=fetched,phase='idle')


class AlertDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.wallet=Wallet(self.root/'wallet.sqlite3');self.wallet_before=(self.root/'wallet.sqlite3').read_bytes()
        self.store=AlertStore(self.root/'alerts.sqlite3');self.now=datetime.now(UTC);self.snapshot=current_snapshot(self.now)
        self.monitor_patch=patch('dual_ma_ai.dashboard.monitor_status',side_effect=lambda _root,_now=None:self.snapshot)
        self.monitor_patch.start()
        evaluator=AlertEvaluator(self.store,self.root/'monitor',policies=PolicySet.load(),
            source_reader=lambda _root,_now:self.snapshot)
        evaluator.tick(now=self.now)
        self.server=make_server(Dashboard(self.wallet,reports=self.root/'reports',cache=self.root/'cache',
            alpaca_cache=self.root/'alpaca',monitor_root=self.root/'monitor',read_only=True,alert_store=self.store),0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3);self.monitor_patch.stop();self.temp.cleanup()

    def get(self,path):
        try:
            with urlopen(self.base+path,timeout=3) as response:return response.status,json.loads(response.read())
        except HTTPError as exc:
            try:return exc.code,json.loads(exc.read())
            finally:exc.close()

    def post(self,path,body,token):
        request=Request(self.base+path,data=json.dumps(body).encode(),method='POST',headers={
            'Content-Type':'application/json','Origin':self.base,'X-CSRF-Token':token})
        try:
            with urlopen(request,timeout=3) as response:return response.status,json.loads(response.read())
        except HTTPError as exc:
            try:return exc.code,json.loads(exc.read())
            finally:exc.close()

    def test_alert_rendering_api_filter_and_dashboard_only_acknowledgment(self):
        status,session=self.get('/api/session');self.assertEqual(status,200)
        status,body=self.get('/api/alerts?state=ENTRY_REVIEW&ticker=MU')
        self.assertEqual(status,200);self.assertTrue(body['read_only'])
        self.assertEqual(len(body['alerts']),1)
        row=body['alerts'][0];self.assertEqual(row['decision_state'],'ENTRY_REVIEW')
        self.assertTrue(row['effective_actionable'])
        ack=dict(signal_id=row['signal_id'],choice='hold',key='http-ack-idempotency-001',actor='local reviewer')
        first=self.post('/api/alerts/ack',ack,session['csrf'])
        second=self.post('/api/alerts/ack',ack,session['csrf'])
        self.assertEqual(first[0],200);self.assertEqual(first,second)
        updated=self.store.list_signals(ticker='MU',strategy=PRESETS[0].name)[0]
        self.assertEqual(updated['decision_state'],'HOLD_REVIEW')
        self.assertEqual((self.root/'wallet.sqlite3').read_bytes(),self.wallet_before)
        self.assertEqual(self.post('/api/funding/confirm',{'confirmed':True},session['csrf'])[0],403)
        self.assertEqual(self.post('/api/orders',{},session['csrf'])[0],403)

    def test_alert_endpoint_fails_closed_when_current_monitor_row_is_stale(self):
        stale=dict(self.snapshot);stale['daily']=[dict(r) for r in self.snapshot['daily']]
        stale['daily'][6]['status']='STALE';stale['daily'][6]['signals']=[]
        with patch('dual_ma_ai.dashboard.monitor_status',return_value=stale):
            _,body=self.get('/api/alerts?state=ENTRY_REVIEW&ticker=MU')
        self.assertFalse(body['alerts'][0]['effective_actionable'])
        self.assertEqual(body['alerts'][0]['effective_decision_state'],'NO_ACTION')
        self.assertIn('stale',body['alerts'][0]['effective_suppression_reason'].lower())

    def test_dashboard_exposes_durable_suppression_reason_as_no_action(self):
        self.store.suppress(ticker='MU',strategy=PRESETS[1].name,timeframe='1Day',
            candle_date=self.snapshot['schedule']['expected_candle'],source_snapshot_id='e'*64,
            reason='Insufficient validated warmup coverage.',reason_code='INSUFFICIENT_COVERAGE',at=self.now)
        status,body=self.get('/api/alerts?state=NO_ACTION&ticker=MU&strategy='+PRESETS[1].name)
        self.assertEqual(status,200)
        blocked=[row for row in body['alerts'] if row['lifecycle_state']=='SUPPRESSED']
        self.assertEqual(len(blocked),1)
        self.assertFalse(blocked[0]['effective_actionable'])
        self.assertIn('Insufficient validated warmup coverage',blocked[0]['effective_suppression_reason'])

    def test_invalid_csrf_and_invalid_filters_do_not_acknowledge_or_trade(self):
        status,body=self.get('/api/alerts?state=BUY')
        self.assertEqual(status,400)
        self.assertIn('error',body)
        _,session=self.get('/api/session');row=self.store.list_signals(ticker='MU',actionable=True)[0]
        req=dict(signal_id=row['signal_id'],choice='acknowledge',key='invalid-csrf-idempotency-01',actor='tester')
        self.assertEqual(self.post('/api/alerts/ack',req,'invalid-token')[0],403)
        self.assertEqual(row['lifecycle_state'],'REVIEW_PENDING')
        self.assertEqual((self.root/'wallet.sqlite3').read_bytes(),self.wallet_before)

    def test_concurrent_monitor_reads_remain_fast_with_alert_reads(self):
        paths=['/api/monitor']*48+['/api/alerts?state=ENTRY_REVIEW']*4
        def request(path):
            start=time.monotonic()
            with urlopen(self.base+path,timeout=5) as response:
                response.read();return response.status,(time.monotonic()-start)*1000
        with ThreadPoolExecutor(max_workers=52) as pool:
            responses=list(pool.map(request,paths))
        self.assertTrue(all(status==200 for status,_ in responses))
        samples=sorted(ms for path,(_,ms) in zip(paths,responses) if path=='/api/monitor')
        p95=samples[int(.95*(len(samples)-1))]
        print(f"ALERT_LOAD monitor_requests={len(samples)} p95_ms={p95:.2f} max_ms={max(samples):.2f}")
        self.assertLess(p95,2000);self.assertLess(max(samples),5000)
        self.assertEqual(len({e['event_id'] for e in self.store.recent_events(limit=200)}),
                         len(self.store.recent_events(limit=200)))


if __name__=='__main__':unittest.main()
