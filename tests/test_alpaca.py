"""Mock-only Alpaca integration. No environment secrets, network, or persistent funds."""
import copy
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo

from dual_ma_ai.alpaca_data import (Credentials, Client, NoRedirect, load_credentials,
    parse_pair, AlpacaCache, SOURCE, POLICY, fetch, live_probe, AlpacaHTTPError, AlpacaTransportError)
from dual_ma_ai.market_data import DataError, HistoryUnavailable, causal_adjusted
from dual_ma_ai.backtest import Execution, run_backtest, compare
from dual_ma_ai.strategy import PRESETS, signals, Strategy
from dual_ma_ai.alpaca_legacy import replay_history
from dual_ma_ai.dashboard import Dashboard, data_kind
from dual_ma_ai.wallet import Wallet
from test_research import CAL, NOW


def fixture(prices=None, start=date(2020,1,2), split_index=None, split_factor=2):
    prices = prices or [100] * 10
    days = CAL.sessions(start, start+timedelta(days=len(prices)*2))[:len(prices)]
    raw, adjusted = [], []
    for i,(day,price) in enumerate(zip(days,prices)):
        pre_split = split_index is not None and i < split_index
        instant = datetime.combine(day,datetime.min.time(),ZoneInfo('America/New_York')).astimezone(timezone.utc).isoformat()
        factor = split_factor if pre_split else 1
        raw.append(dict(t=instant,o=str(price*factor),h=str(price*factor),l=str(price*factor),c=str(price*factor),v=10000))
        adjusted.append(dict(t=instant,o=str(price),h=str(price),l=str(price),c=str(price),v=10000*factor))
    return dict(raw=raw,split=adjusted,start=str(days[0]),end=str(days[-1]),feed='iex',asof='-')


class Response:
    def __init__(self,payload):
        self.raw = json.dumps(payload).encode()
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,size):return self.raw[:size]


class SplitQuantizationTests(unittest.TestCase):
    def test_provider_cent_rounding_preserves_exact_causal_split(self):
        p=fixture([Decimal('12.512')]*3,split_index=2,split_factor=10)
        for row in p['split'][:2]:
            for field in ('o','h','l','c'): row[field]='12.51'
        ds,_=parse_pair('NVDA',p,NOW,CAL)
        self.assertEqual(ds.bars[2].split,Fraction(10))
        self.assertEqual(ds.bars[0].close,Fraction('125.12'))
        self.assertEqual(next(causal_adjusted(ds.bars))[3],ds.bars[0].close)

    def test_rounding_tolerance_does_not_allow_wrong_adjustment(self):
        p=fixture([Decimal('12.512')]*3,split_index=2,split_factor=10)
        p['split'][0]['c']='12.50'
        with self.assertRaises(DataError): parse_pair('NVDA',p,NOW,CAL)


class CredentialsTests(unittest.TestCase):
    def test_only_authorized_environment_variables_no_file_access(self):
        with patch.object(Path,'read_text',side_effect=AssertionError('No credential files')):
            c=load_credentials({'ALPACA_API_KEY':'mock-key','ALPACA_SECRET_KEY':'mock-secret'})
        self.assertNotIn('mock-key',repr(c));self.assertNotIn('mock-secret',repr(c))
        self.assertEqual(c.feed,'iex')
        with self.assertRaises(DataError):
            load_credentials({'ALPACA_API_KEY_ID':'other','APCA_API_SECRET_KEY':'other'})

    def test_invalid_header_encoding_is_rejected_without_echo(self):
        value='mock-secret\nInjected: value'
        with self.assertRaises(DataError) as exc:
            load_credentials({'ALPACA_API_KEY':'mock-key','ALPACA_SECRET_KEY':value})
        self.assertNotIn(value,str(exc.exception))

    def test_missing_credentials_never_send_requests(self):
        def forbidden(*a,**k):raise AssertionError('Network not allowed without credentials')
        with patch.object(Path,'read_text',side_effect=AssertionError('File fallback forbidden')):
            result=live_probe({},opener=forbidden)
        self.assertEqual(result['authentication'],'NOT_ATTEMPTED')
        self.assertIsNone(result['feeds']['iex']['historical']['http_status'])
        for env in ({'ALPACA_API_KEY':'mock-key'},{'ALPACA_SECRET_KEY':'mock-secret'}):
            with self.assertRaises(DataError):load_credentials(env)


class TransportTests(unittest.TestCase):
    def client(self,opener,**kwargs):return Client(Credentials('mock-key','mock-secret'),opener=opener,sleep=lambda _:None,jitter=lambda:0,**kwargs)

    def test_get_allowlist_and_credential_destination(self):
        requests=[]
        def opener(req,timeout):
            requests.append(req);self.assertEqual(timeout,20);return Response({'bars':{}})
        client=self.client(opener)
        client.get('/v2/stocks/bars',{'feed':'iex'})
        req=requests[0]
        self.assertEqual(req.get_method(),'GET')
        self.assertEqual(urlsplit(req.full_url).netloc,'data.alpaca.markets')
        self.assertNotIn('mock-key',req.full_url);self.assertNotIn('mock-secret',req.full_url)
        for path in ('/v2/orders','/v2/account','https://evil.example','/v2/stocks/bars/../../orders'):
            with self.assertRaises(DataError):client.get(path,{'feed':'iex'})
        self.assertEqual(len(requests),1)

    def test_redirects_refused(self):
        with self.assertRaises(DataError):NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example')

    def test_pagination_explicit_feed_and_adjustment(self):
        rows=fixture()['raw'];requests=[]
        def opener(req,timeout):
            q=parse_qs(urlsplit(req.full_url).query);requests.append(q)
            return Response({'bars':{'MU':rows[5:] if q.get('page_token') else rows[:5]},'next_page_token':None if q.get('page_token') else 'page2'})
        result=self.client(opener).bars('MU',date(2020,1,2),date(2020,2,1),'sip','raw')
        self.assertEqual(result,rows)
        self.assertEqual(requests[1]['page_token'],['page2'])
        self.assertTrue(all(q['feed']==['sip'] and q['asof']==['-'] and q['adjustment']==['raw'] for q in requests))

    def test_repeated_token_and_wrong_symbol_rejected(self):
        for payload in ({'bars':{},'next_page_token':'same'},{'bars':{'OTHER':[]}}, {'bars':[]}):
            with self.assertRaises(DataError):self.client(lambda *a,**k:Response(payload)).bars('MU',date(2020,1,1),date(2020,2,1),'iex','raw')

    def test_retry_429_and_500_bounded(self):
        calls=[];delays=[]
        def opener(*a,**k):
            calls.append(1)
            if len(calls)<3:raise HTTPError('https://data.alpaca.markets',429 if len(calls)==1 else 500,'error',{'Retry-After':'999'},None)
            return Response({'bars':{}})
        client=self.client(opener);client.sleep=delays.append
        client.get('/v2/stocks/bars',{'feed':'iex'})
        self.assertEqual(len(calls),3);self.assertEqual(delays,[30,30])

    def test_retry_exhaustion_and_no_auth_retry(self):
        for status,count in ((429,3),(503,3),(401,1),(403,1)):
            calls=[]
            def opener(*a,**k):
                calls.append(1);raise HTTPError('https://data.alpaca.markets',status,'credential-body-must-not-leak',{},None)
            with self.assertRaises(AlpacaHTTPError) as exc:self.client(opener).get('/v2/stocks/bars',{'feed':'sip'})
            self.assertEqual(len(calls),count);self.assertNotIn('credential-body',str(exc.exception))

    def test_network_errors_sanitized(self):
        def opener(*a,**k):raise URLError('mock-secret')
        with self.assertRaises(DataError) as exc:self.client(opener).get('/v2/stocks/bars',{'feed':'iex'})
        self.assertNotIn('mock-secret',str(exc.exception))

    def test_transient_transport_retries_are_bounded_and_metadata_is_safe(self):
        calls=[];delays=[];ticks=iter([0,0.1,0.3])
        def opener(*a,**k):
            calls.append(1);raise URLError('credential-looking-message')
        client=Client(Credentials('mock-key','mock-secret'),opener=opener,sleep=delays.append,
                      jitter=lambda:0.2,clock=lambda:next(ticks))
        with self.assertRaises(AlpacaTransportError) as exc:client.get('/v2/stocks/bars',{'feed':'iex'})
        self.assertEqual(len(calls),3);self.assertEqual(delays,[1.2,2.2])
        self.assertEqual(exc.exception.exception_class,'URLError')
        self.assertEqual(exc.exception.attempts,3);self.assertEqual(exc.exception.duration_ms,100)
        self.assertNotIn('credential-looking',str(exc.exception))

    def test_timeout_and_connection_errors_are_both_retried_with_a_bound(self):
        for failure in (TimeoutError('private-timeout-detail'),ConnectionError('private-connection-detail')):
            with self.subTest(exception=type(failure).__name__):
                calls=[]
                def opener(*a,**k):calls.append(1);raise failure
                client=self.client(opener,attempts=2)
                with self.assertRaises(AlpacaTransportError) as exc:client.get('/v2/stocks/bars',{'feed':'iex'})
                self.assertEqual(len(calls),2);self.assertEqual(exc.exception.attempts,2)
                self.assertNotIn('private-',str(exc.exception))

    def test_authorization_failure_is_not_retried_and_has_safe_metadata(self):
        calls=[]
        def opener(*a,**k):
            calls.append(1);raise HTTPError('https://data.alpaca.markets',403,'secret-body',{},None)
        with self.assertRaises(AlpacaHTTPError) as exc:self.client(opener).get('/v2/stocks/bars',{'feed':'iex'})
        self.assertEqual(len(calls),1);self.assertEqual(exc.exception.status,403)
        self.assertEqual(exc.exception.attempts,1);self.assertNotIn('secret-body',str(exc.exception))

    def test_bad_json_and_nonfinite_rejected(self):
        for raw in (b'bad',b'{"x":NaN}',b'{"x":1,"x":2}'):
            response=Response({});response.raw=raw
            with self.assertRaises((DataError,ValueError)):self.client(lambda *a,**k:response).get('/v2/stocks/bars',{'feed':'iex'})

    def test_separate_historical_and_recent_entitlements_no_fallback(self):
        calls=[]
        def opener(req,timeout):
            q=parse_qs(urlsplit(req.full_url).query);feed=q['feed'][0];calls.append((feed,urlsplit(req.full_url).path))
            if feed=='sip' and 'quotes' in req.full_url:raise HTTPError(req.full_url,403,'DENIED',{},None)
            return Response({'quotes':{'MU':{'t':'2020-01-02T05:00:00Z'}}} if 'quotes' in req.full_url else {'bars':{'MU':[{'t':'2020-01-02T05:00:00Z'}]}})
        result=self.client(opener).entitlements(NOW)
        self.assertEqual([f for f,p in calls],['iex','iex','sip','sip'])
        self.assertEqual(result['feeds']['sip']['historical']['http_status'],200)
        self.assertEqual(result['feeds']['sip']['recent']['http_status'],403)
        self.assertEqual(result['feeds']['iex']['historical']['status'],'PERMITTED')


class ValidationTests(unittest.TestCase):
    def test_exact_split_normalization_and_causal_signals(self):
        p=fixture([100]*6,split_index=3)
        ds,v=parse_pair('MU',p,NOW,CAL)
        self.assertEqual(ds.bars[3].split,Fraction(2))
        self.assertEqual([row[3] for row in causal_adjusted(ds.bars)],[Fraction(200)]*6)
        self.assertEqual(signals(ds,Strategy('SMA',1,2),as_of=NOW),())
        self.assertEqual(v['data_kind'],'REAL');self.assertEqual(ds.feed,'iex')
        self.assertEqual(ds.bars[0].open,Fraction(200))
        self.assertIn('PRICE RETURN ONLY',v['warnings'][0])

    def test_future_split_does_not_rewrite_past_signal_inputs(self):
        old,_=parse_pair('MU',fixture([100]*5+[110]*5),NOW,CAL)
        future,_=parse_pair('MU',fixture([50]*5+[55]*5+[52]*5,split_index=10),NOW,CAL)
        self.assertEqual(list(causal_adjusted(old.bars)),list(causal_adjusted(future.bars))[:10])
        asof=old.bars[-1].closed_at
        self.assertEqual(signals(old,Strategy('SMA',1,2),as_of=asof),signals(future,Strategy('SMA',1,2),as_of=asof))

    def test_timezone_and_session_validation(self):
        for stamp in ('2020-01-02T05:00:00','2020-01-02T00:00:00Z','2020-01-04T05:00:00Z'):
            p=fixture();p['raw'][0]['t']=p['split'][0]['t']=stamp
            with self.assertRaises((DataError,ValueError)):parse_pair('MU',p,NOW,CAL)

    def test_calendar_dst_and_holiday(self):
        from dual_ma_ai.market_data import USCalendar
        cal=USCalendar(date(2020,3,1),date(2020,3,20))
        p=fixture([100]*5,start=date(2020,3,6))
        ds,_=parse_pair('MU',p,NOW,cal)
        self.assertEqual(ds.bars[0].opened_at.hour,14)
        self.assertEqual(ds.bars[1].opened_at.hour,13)
        p=fixture([100],start=date(2020,7,3))
        with self.assertRaises(DataError):parse_pair('MU',p,NOW,USCalendar(date(2020,7,1),date(2020,7,10)))

    def test_calendar_weekend_endpoint_does_not_invalidate_freshness(self):
        from dual_ma_ai.market_data import USCalendar
        cal=USCalendar(date(2026,9,30),date(2026,10,11))
        self.assertEqual(cal.sessions(date(2026,9,30),date(2026,10,10))[-1],date(2026,10,9))
        with self.assertRaises(DataError):cal.bounds(date(2026,10,10))

    def test_completed_candles_only(self):
        p=fixture();asof=CAL.bounds(date.fromisoformat(p['raw'][-1]['t'][:10]))[1]-timedelta(seconds=1)
        ds,v=parse_pair('MU',p,asof,CAL)
        self.assertEqual(len(ds.bars),9);self.assertEqual(v['excluded_incomplete'],1)
        self.assertTrue(all(b.closed_at<=asof for b in ds.bars))

    def test_regular_close_is_not_full_daily_aggregate_availability(self):
        p=fixture([100]*9+[110])
        asof=CAL.bounds(date.fromisoformat(p['raw'][-1]['t'][:10]))[1]+timedelta(hours=1)
        ds,_=parse_pair('MU',p,NOW,CAL)
        crosses=signals(ds,Strategy('SMA',1,2),as_of=asof)
        self.assertFalse(any(s.day==ds.bars[-1].day for s in crosses))
        usable,_=parse_pair('MU',p,asof,CAL)
        self.assertEqual(len(usable.bars),9)
        self.assertTrue(all(b.usable_at<=asof for b in usable.bars))

    def test_missing_duplicate_unsorted_sessions(self):
        for mutate in ('missing','duplicate','unsorted'):
            p=fixture()
            for name in ('raw','split'):
                if mutate=='missing':p[name].pop(4)
                elif mutate=='duplicate':p[name].insert(4,copy.deepcopy(p[name][4]))
                else:p[name][4],p[name][5]=p[name][5],p[name][4]
            with self.assertRaises(DataError):parse_pair('MU',p,NOW,CAL)

    def test_outliers_ohlc_and_adjustment_mismatch(self):
        for mutate in ('outlier','ohlc','adjustment','volume'):
            p=fixture()
            if mutate=='outlier':
                for name in ('raw','split'):
                    for k in ('o','h','l','c'):p[name][5][k]='300'
            elif mutate=='ohlc':p['raw'][0]['h']='1'
            elif mutate=='adjustment':p['split'][0]['c']='99'
            else:p['split'][0]['v']=15000
            with self.assertRaises(DataError):parse_pair('MU',p,NOW,CAL)

    def test_invalid_numbers_and_volume(self):
        for value in ('NaN','-1','0',100.0):
            p=fixture();p['raw'][0]['c']=value
            with self.assertRaises(DataError):parse_pair('MU',p,NOW,CAL)
        p=fixture();p['raw'][0]['v']='1.2'
        with self.assertRaises(DataError):parse_pair('MU',p,NOW,CAL)

    def test_no_empty_or_mismatched_pairs(self):
        for p in (dict(fixture(),raw=[],split=[]),dict(fixture(),split=[]),dict(fixture(),asof='2020-01-01')):
            with self.assertRaises(DataError):parse_pair('MU',p,NOW,CAL)

    def test_stale_tail_warning_and_no_extrapolation(self):
        p=fixture();p['end']='2020-02-01'
        ds,v=parse_pair('MU',p,NOW,CAL)
        self.assertTrue(any('STALE' in w for w in v['warnings']))
        self.assertEqual(len(ds.bars),10)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.cache=AlpacaCache(self.root/'alpaca')
    def tearDown(self):self.temp.cleanup()

    def test_atomic_cache_deterministic_and_persistent(self):
        p=fixture();first=self.cache.save('MU',p,NOW,CAL);second=self.cache.save('MU',p,NOW,CAL)
        self.assertEqual(first,second)
        self.assertEqual(len(list(first.parent.glob('*.json'))),1)
        ds,v=AlpacaCache(self.cache.root).load(first,CAL)
        self.assertEqual(ds.content_hash,parse_pair('MU',p,NOW,CAL)[0].content_hash)
        self.assertEqual(self.cache.latest('MU','iex',CAL)[1],first)
        with self.assertRaises(HistoryUnavailable):self.cache.latest('MU','sip',CAL)

    def test_cache_tamper_rejected(self):
        path=self.cache.save('MU',fixture(),NOW,CAL)
        self.cache.load(path,CAL)  # warm the fingerprint-validated decoded-history cache
        e=json.loads(path.read_text());e['payload']['raw'][0]['c']='999'
        path.write_text(json.dumps(e))
        with self.assertRaises(DataError):self.cache.load(path,CAL)

    def test_invalid_data_not_published(self):
        p=fixture();p['raw'].pop()
        with self.assertRaises(DataError):self.cache.save('MU',p,NOW,CAL)
        self.assertFalse(self.cache.root.exists())

    def test_fetch_through_mock_transport(self):
        p=fixture()
        def opener(req,timeout):
            q=parse_qs(urlsplit(req.full_url).query)
            return Response({'bars':{'MU':p[q['adjustment'][0]]},'next_page_token':None})
        c=Client(Credentials('mock-key','mock-secret'),opener=opener)
        path=fetch(c,self.cache,'MU',date.fromisoformat(p['start']),date.fromisoformat(p['end']),'iex',now=NOW,calendar=CAL)
        self.assertEqual(self.cache.load(path,CAL)[1]['status'],'VALIDATED')
        self.assertNotIn('mock-secret',path.read_text())

    def test_modern_next_bar_and_legacy_separation(self):
        prices=[100]*205+[90]*10+[120]*30+[90]*30
        p=fixture(prices);ds,v=parse_pair('MU',p,NOW,CAL)
        run=run_backtest({'MU':ds},PRESETS[1],ds.bars[205].day,ds.bars[-1].day,Execution(warmup=200),calendar=CAL,as_of=NOW)
        self.assertTrue(run['fills'])
        self.assertTrue(all(f['signal_day']<f['fill_day'] for f in run['fills']))
        self.assertEqual(run['provenance']['MU']['feed'],'iex')
        replay=replay_history({'MU':ds})
        self.assertEqual(replay['mode'],'LEGACY_OFFLINE_REPLAY')
        self.assertEqual(replay['input_data_kind'],'REAL')
        self.assertEqual(set(replay['ledgers']),{'todo','porcentaje','atr'})
        self.assertEqual(replay['unavailable_legacy_symbols'],['BE','SNDK','WDC'])
        self.assertIn('ALPACA EQUITY ADAPTATION',replay['warnings'][-1])

    def test_mixed_feeds_and_nonlegacy_symbols_rejected(self):
        ds,_=parse_pair('MU',fixture([100]*240),NOW,CAL)
        other=replace(ds,ticker='LITE',feed='sip')
        with self.assertRaises(DataError):run_backtest({'MU':ds,'LITE':other},PRESETS[0],ds.bars[210].day,ds.bars[-1].day,Execution(),calendar=CAL,as_of=NOW)
        with self.assertRaises(DataError):replay_history({'LITE':other})

    def test_unavailable_horizons_never_extrapolated(self):
        ds,_=parse_pair('MU',fixture(),NOW,CAL)
        result=compare({'MU':ds},ds.bars[-1].day,Execution(),calendar=CAL,as_of=NOW)
        self.assertEqual([c['status'] for c in result['comparisons']],['unavailable']*4)
        self.assertTrue(all(c['runs']==[] for c in result['comparisons']))

    def test_dashboard_alpaca_provenance_wallet_isolation(self):
        p=fixture([100]*220);ds,v=parse_pair('MU',p,NOW,CAL)
        w=Wallet(self.root/'wallet.sqlite3');before=Path(w.path).read_bytes()
        app=Dashboard(w,reports=self.root/'reports',cache=self.root/'alpha',alpaca_cache=self.cache.root)
        with patch.object(app.alpaca,'latest',return_value=((ds,v),Path('fixture'))),patch('dual_ma_ai.dashboard.USCalendar',return_value=CAL):
            rows=app.signal_panel('alpaca','iex')['rows']
        self.assertTrue(all(r['status']=='REAL' and r['feed']=='iex' and r['validation_status']=='VALIDATED' for r in rows))
        self.assertEqual(Path(w.path).read_bytes(),before)
        report={'comparisons':[{'runs':[{'provenance':{'MU':dict(source=SOURCE,feed='iex',validation_status='VALIDATED')}}]}]}
        self.assertEqual(data_kind(report),'REAL')
        report['synthetic']=True;self.assertEqual(data_kind(report),'SYNTHETIC')

    def test_original_legacy_equities_are_data_only(self):
        path=self.cache.save('WDC',fixture(),NOW,CAL)
        ds,_=self.cache.latest('WDC','iex',CAL)[0]
        self.assertEqual(ds.ticker,'WDC')
        self.assertEqual(replay_history({'WDC':ds})['input_universe'],['WDC'])
        with self.assertRaises(DataError):
            run_backtest({'WDC':ds},PRESETS[0],ds.bars[0].day,ds.bars[-1].day,Execution(),calendar=CAL,as_of=NOW)

    def test_coverage_no_data_all_tickers_unavailable(self):
        report=self.cache.coverage('sip')
        self.assertEqual(len(report['rows']),9)
        self.assertTrue(all(r['data_kind']=='UNAVAILABLE' and all(h['status']=='unavailable' for h in r['horizons']) for r in report['rows']))

    def test_real_calendar_coverage_keeps_available_shorter_horizons(self):
        from dual_ma_ai.market_data import USCalendar
        cal=USCalendar(date(2024,1,1),date(2026,10,20))
        days=cal.sessions(date(2024,1,2),date(2026,10,9))
        p=fixture([100]*len(days))
        for name in ('raw','split'):
            for row,day in zip(p[name],days):
                row['t']=datetime.combine(day,datetime.min.time(),ZoneInfo('America/New_York')).astimezone(timezone.utc).isoformat()
        p.update(start=str(days[0]),end=str(days[-1]))
        self.cache.save('MU',p,datetime(2026,10,10,12,tzinfo=timezone.utc),cal)
        row=self.cache.coverage('iex')['rows'][6]
        self.assertEqual(row['status'],'VALIDATED')
        self.assertEqual([h['status'] for h in row['horizons']],['available','unavailable','unavailable','unavailable'])
