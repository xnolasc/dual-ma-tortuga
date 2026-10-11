import tempfile
import sqlite3
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dual_ma_ai.alert_evaluator import AlertCapacityError, AlertEvaluator
from dual_ma_ai.alert_store import AlertStore
from dual_ma_ai.alert_provenance import CachedPolicyResolver
from dual_ma_ai.market_data import canonical, digest
from dual_ma_ai.alpaca_data import POLICY, SOURCE
from dual_ma_ai.strategy import PRESETS
from alert_helpers import AS_OF, snapshot

UTC=timezone.utc


class AlertEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Path(self.temp.name)/'alerts.sqlite3'
        self.store=AlertStore(self.db)

    def tearDown(self):self.temp.cleanup()

    def evaluator(self, data, source_reader=None, **kwargs):
        return AlertEvaluator(self.store,Path(self.temp.name)/'monitor',
            source_reader=source_reader or (lambda _root,_now:data),**kwargs)

    def test_validated_confirmed_daily_crossover_creates_entry_review_once(self):
        data=snapshot(sides={'MU':{PRESETS[1].name:'BUY'}})
        evaluator=self.evaluator(data)
        first=evaluator.tick(now=AS_OF)
        second=evaluator.tick(now=AS_OF+timedelta(minutes=1))
        rows=self.store.list_signals(ticker='MU',strategy=PRESETS[1].name)
        self.assertEqual(first['status'],'OK')
        self.assertEqual(first['actionable_created'],1)
        self.assertEqual(second['actionable_created'],0)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['decision_state'],'ENTRY_REVIEW')
        self.assertEqual(len([e for e in self.store.recent_events(limit=200) if e['signal_id']==rows[0]['signal_id'] and e['transition']=='CREATED']),1)

    def test_no_cross_is_no_action_not_a_review_alert(self):
        result=self.evaluator(snapshot()).tick(now=AS_OF)
        rows=self.store.list_signals(actionable=True)
        self.assertEqual(result['actionable_created'],0)
        self.assertEqual(rows,[])
        no_action=self.store.list_signals(decision_state='NO_ACTION')
        self.assertEqual(len(no_action),9*3)

    def test_blocked_stale_and_insufficient_ticker_outputs_are_suppressed_with_reason(self):
        data=snapshot(sides={'MU':{PRESETS[0].name:'BUY'},'LITE':{PRESETS[1].name:'SELL'}},
                      blocked=('MU',),stale=('LITE',),incomplete=('NVDA',),status='DEGRADED')
        result=self.evaluator(data).tick(now=AS_OF)
        self.assertEqual(result['status'],'OK')
        self.assertEqual(self.store.list_signals(actionable=True),[])
        events=self.store.recent_events(limit=200)
        self.assertTrue(any(e['transition']=='SUPPRESSED' and e['payload_json'].find('BLOCKED_DATA')>=0 for e in events))
        self.assertTrue(any(e['transition']=='SUPPRESSED' and e['payload_json'].find('STALE_DATA')>=0 for e in events))
        self.assertTrue(any(e['transition']=='SUPPRESSED' and e['payload_json'].find('INVALID_DATA')>=0 for e in events))

    def test_global_incomplete_refresh_suppresses_even_previously_valid_rows(self):
        data=snapshot(sides={'MU':{PRESETS[0].name:'BUY'}});data['in_progress']=True
        result=self.evaluator(data).tick(now=AS_OF)
        self.assertEqual(result['reason_code'],'REFRESH_IN_PROGRESS')
        self.assertEqual(self.store.list_signals(actionable=True),[])

    def test_calendar_projection_suppresses_alerts_without_relabeling_data_stale(self):
        data=snapshot(sides={'MU':{PRESETS[0].name:'BUY'}})
        data['phase']='calendar';data['in_progress']=False
        result=self.evaluator(data).tick(now=AS_OF)
        self.assertEqual(result['reason_code'],'MONITOR_PHASE_ACTIVE')
        self.assertEqual(self.store.list_signals(actionable=True),[])
        self.assertTrue(any(e['transition']=='SUPPRESSED' and 'MONITOR_PHASE_ACTIVE' in e['payload_json']
                            for e in self.store.recent_events(limit=200)))

    def test_duplicate_strategy_output_is_suppressed(self):
        data=snapshot(sides={'MU':{PRESETS[0].name:'BUY'}})
        data['daily'][6]['signals'].append(dict(data['daily'][6]['signals'][0]))
        result=self.evaluator(data).tick(now=AS_OF)
        self.assertEqual(result['actionable_created'],0)
        self.assertEqual(self.store.list_signals(ticker='MU',actionable=True),[])

    def test_missing_adjustment_policy_is_suppressed(self):
        data=snapshot(sides={'MU':{PRESETS[0].name:'BUY'}})
        data['daily'][6].pop('adjustment_policy')
        result=self.evaluator(data).tick(now=AS_OF)
        self.assertEqual(result['actionable_created'],0)
        self.assertEqual(self.store.list_signals(ticker='MU',actionable=True),[])
        self.assertTrue(any(e['transition']=='SUPPRESSED' and 'adjustment policy' in e['payload_json'].lower()
                            for e in self.store.recent_events(limit=200)))

    def test_missing_policy_is_accepted_only_with_matching_immutable_cache_proof(self):
        data=snapshot(sides={'CIEN':{PRESETS[2].name:'BUY'}})
        row=next(row for row in data['daily'] if row['ticker']=='CIEN')
        row.pop('adjustment_policy')
        payload={'feed':'iex','ticker':'CIEN','bars':[{'day':row['candle']}]}
        row['payload_sha256']=digest(payload)
        history=Path(self.temp.name)/'history';history.mkdir()
        envelope=dict(format_version=1,source=SOURCE,policy=POLICY,ticker='CIEN',
            fetched_at='2024-01-03T05:19:00+00:00',payload=payload,validation=row['validation'])
        (history/f"CIEN-iex-{digest(envelope)}.json").write_bytes(canonical(envelope))
        evaluator=AlertEvaluator(self.store,Path(self.temp.name)/'monitor',
            source_reader=lambda _r,_n:data,policy_resolver=CachedPolicyResolver(history))
        result=evaluator.tick(now=AS_OF)
        self.assertEqual(result['actionable_created'],1)
        rows=self.store.list_signals(ticker='CIEN',strategy=PRESETS[2].name,actionable=True)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['decision_state'],'ENTRY_REVIEW')

    def test_missing_policy_without_matching_cache_proof_remains_suppressed(self):
        data=snapshot(sides={'CIEN':{PRESETS[2].name:'BUY'}})
        row=next(row for row in data['daily'] if row['ticker']=='CIEN')
        row.pop('adjustment_policy')
        resolver=CachedPolicyResolver(Path(self.temp.name)/'missing-history')
        result=AlertEvaluator(self.store,Path(self.temp.name)/'monitor',
            source_reader=lambda _r,_n:data,policy_resolver=resolver).tick(now=AS_OF)
        self.assertEqual(result['actionable_created'],0)
        self.assertEqual(self.store.list_signals(ticker='CIEN',actionable=True),[])

    def test_policy_resolver_failure_is_audited_as_suppression(self):
        data=snapshot(sides={'CIEN':{PRESETS[2].name:'BUY'}})
        next(row for row in data['daily'] if row['ticker']=='CIEN').pop('adjustment_policy')
        def fail(_row):raise OSError('local cache unavailable')
        result=AlertEvaluator(self.store,Path(self.temp.name)/'monitor',
            source_reader=lambda _r,_n:data,policy_resolver=fail).tick(now=AS_OF)
        self.assertEqual(result['actionable_created'],0)
        self.assertTrue(any(event['transition']=='SUPPRESSED' and
            'adjustment policy' in event['payload_json'].lower()
            for event in self.store.recent_events(limit=200)))

    def test_restart_replay_keeps_event_ids_and_count(self):
        data=snapshot(sides={'MU':{PRESETS[2].name:'SELL'}})
        one=self.evaluator(data);one.tick(now=AS_OF)
        event_ids={e['event_id'] for e in self.store.recent_events(limit=200)}
        restarted=AlertEvaluator(AlertStore(self.db),Path(self.temp.name)/'monitor',source_reader=lambda _r,_n:data)
        restarted.tick(now=AS_OF+timedelta(minutes=1))
        self.assertEqual(event_ids,{e['event_id'] for e in self.store.recent_events(limit=200)})
        rows=self.store.list_signals(ticker='MU',actionable=True)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['decision_state'],'EXIT_REVIEW')

    def test_exchange_session_expiry_is_audited(self):
        self.evaluator(snapshot(sides={'MU':{PRESETS[0].name:'BUY'}})).tick(now=AS_OF)
        later=datetime(2024,1,3,21,tzinfo=UTC)
        evaluator=self.evaluator(snapshot(day='2024-01-03'),source_reader=lambda _r,_n:snapshot(day='2024-01-02'))
        evaluator.tick(now=later)
        row=next(r for r in self.store.list_signals(ticker='MU',strategy=PRESETS[0].name)
                 if r['technical_side']=='BUY')
        self.assertEqual(row['decision_state'],'NO_ACTION')
        self.assertEqual(row['lifecycle_state'],'EXPIRED')
        self.assertTrue(any(e['transition']=='EXPIRED' for e in self.store.recent_events()))

    def test_only_one_snapshot_can_be_in_flight(self):
        entered=threading.Event();release=threading.Event()
        def slow(_root,_now):entered.set();release.wait(2);return snapshot()
        evaluator=AlertEvaluator(self.store,Path(self.temp.name)/'monitor',source_reader=slow)
        thread=threading.Thread(target=lambda:evaluator.tick(now=AS_OF));thread.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(AlertCapacityError):evaluator.tick(now=AS_OF)
        release.set();thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_bounded_backoff_and_circuit_breaker_survive_restart(self):
        def fail(_root,_now):raise OSError('temporary local source read failure')
        evaluator=AlertEvaluator(self.store,Path(self.temp.name)/'monitor',source_reader=fail,
                                  retry_delays=(0,0,0,0,0))
        now=AS_OF
        for _ in range(5):
            result=evaluator.tick(now=now)
            self.assertEqual(result['status'],'ERROR')
            now+=timedelta(seconds=result['retry_after_seconds'])
        self.assertEqual(evaluator.tick(now=now)['status'],'CIRCUIT_OPEN')
        restarted=AlertEvaluator(AlertStore(self.db),Path(self.temp.name)/'monitor',source_reader=fail)
        self.assertEqual(restarted.tick(now=now)['status'],'CIRCUIT_OPEN')
        self.assertTrue(self.store.verify_chain())

    def test_transient_database_contention_uses_bounded_retry_and_recovers(self):
        original=self.store.expire;calls={'n':0}
        def flaky(now,*,limit=100):
            calls['n']+=1
            if calls['n']<3:raise sqlite3.OperationalError('database is locked')
            return original(now,limit=limit)
        self.store.expire=flaky
        evaluator=AlertEvaluator(self.store,Path(self.temp.name)/'monitor',
            source_reader=lambda _r,_n:snapshot(sides={'MU':{PRESETS[0].name:'BUY'}}),
            retry_delays=(0.001,0.001,0.001))
        result=evaluator.tick(now=AS_OF)
        self.assertEqual(result['status'],'OK')
        self.assertEqual(calls['n'],3)
        self.assertEqual(len(self.store.list_signals(ticker='MU',actionable=True)),1)


if __name__=='__main__':unittest.main()
