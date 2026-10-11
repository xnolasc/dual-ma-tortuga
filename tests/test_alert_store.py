import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dual_ma_ai.alert_store import AlertStore
from dual_ma_ai.market_data import DataError
from dual_ma_ai.alert_policy import ExpiryPolicy, expiry_for
from dual_ma_ai.strategy import PRESETS
from alert_helpers import AS_OF


class AlertStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'alerts.sqlite3'
        self.store=AlertStore(self.path)

    def tearDown(self):self.temp.cleanup()

    def signal(self, side='BUY', payload='a'*64):
        expiry,calendar=expiry_for('2024-01-02',ExpiryPolicy(PRESETS[0].name,'1Day',1))
        return dict(ticker='MU',strategy=PRESETS[0].name,strategy_version='phase5a-v1',timeframe='1Day',
            source='Alpaca Market Data daily bars',feed='iex',adjustment_policy='split-v1',
            candle_date='2024-01-02',closed_at=datetime(2024,1,2,21,tzinfo=timezone.utc),
            usable_at=datetime(2024,1,3,5,16,tzinfo=timezone.utc),
            fetched_at=datetime(2024,1,3,5,20,tzinfo=timezone.utc),payload_hash=payload,
            source_snapshot_id='b'*64,technical_side=side,expiry_policy_id='one-session-v1',
            calendar_version=calendar,expires_at=expiry)

    def test_schema_isolated_and_append_only_audit_chain(self):
        self.assertEqual(self.store.path,self.path.resolve())
        result=self.store.observe(self.signal(),at=AS_OF)
        self.assertTrue(result['actionable'])
        self.assertTrue(self.store.verify_chain())
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM audit_events')
            with self.assertRaises(sqlite3.IntegrityError):db.execute("UPDATE audit_events SET transition='X'")

    def test_hash_chain_detects_manual_tampering_after_trigger_bypass(self):
        self.store.observe(self.signal(),at=AS_OF)
        db=sqlite3.connect(self.path)
        db.execute('DROP TRIGGER audit_events_no_update')
        db.execute("UPDATE audit_events SET payload_json='{}' WHERE seq=1")
        db.commit();db.close()
        with self.assertRaisesRegex(DataError,'hash chain'):
            AlertStore(self.path)

    def test_replay_is_durable_idempotent_across_store_restart(self):
        signal=self.signal()
        first=self.store.observe(signal,at=AS_OF)
        count=len(self.store.recent_events())
        restarted=AlertStore(self.path)
        second=restarted.observe(signal,at=AS_OF)
        self.assertEqual(first['signal_id'],second['signal_id'])
        self.assertFalse(second['created'])
        self.assertEqual(len(restarted.recent_events()),count)
        self.assertEqual(len(restarted.list_signals(actionable=True)),1)

    def test_acknowledgment_is_idempotent_and_hold_is_human_selected(self):
        result=self.store.observe(self.signal(),at=AS_OF)
        ack=self.store.acknowledge(result['signal_id'],choice='hold',key='acknowledgment-key-0001',
                                   actor='reviewer',now=AS_OF)
        replay=self.store.acknowledge(result['signal_id'],choice='hold',key='acknowledgment-key-0001',
                                      actor='reviewer',now=AS_OF)
        self.assertEqual(ack,replay)
        self.assertEqual(ack['decision_state'],'HOLD_REVIEW')
        row=self.store.list_signals()[0]
        self.assertEqual(row['lifecycle_state'],'HOLD_REVIEW')
        self.assertEqual(row['acknowledged_by'],'reviewer')
        with self.assertRaises(DataError):self.store.acknowledge(result['signal_id'],choice='acknowledge',
            key='acknowledgment-key-0001',actor='reviewer',now=AS_OF)

    def test_expiration_creates_audit_and_no_action_projection(self):
        result=self.store.observe(self.signal(),at=AS_OF)
        later=datetime(2024,1,3,21,tzinfo=timezone.utc)
        expired=self.store.expire(later)
        self.assertEqual(expired,[result['signal_id']])
        row=self.store.list_signals()[0]
        self.assertEqual(row['decision_state'],'NO_ACTION')
        self.assertEqual(row['lifecycle_state'],'EXPIRED')
        self.assertFalse(row['actionable'])
        self.assertIn('EXPIRED',[e['transition'] for e in self.store.recent_events()])

    def test_payload_revision_is_audited_and_side_change_supersedes_old_review(self):
        first=self.store.observe(self.signal('BUY','a'*64),at=AS_OF)
        revised=self.store.observe(self.signal('SELL','c'*64),at=AS_OF)
        self.assertNotEqual(first['signal_id'],revised['signal_id'])
        rows=self.store.list_signals(actionable=True)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['technical_side'],'SELL')
        self.assertIn('SUPERSEDED',[e['transition'] for e in self.store.recent_events()])

    def test_suppression_reason_is_durable_and_disables_prior_alert(self):
        active=self.store.observe(self.signal(),at=AS_OF)
        suppressed=self.store.suppress(ticker='MU',strategy=PRESETS[0].name,timeframe='1Day',
            candle_date='2024-01-03',source_snapshot_id='d'*64,reason='Expected completed candle is stale.',
            reason_code='STALE_DATA',at=AS_OF)
        self.assertTrue(suppressed['created'])
        self.assertEqual(self.store.list_signals(actionable=True),[])
        events=self.store.recent_events()
        self.assertTrue(any(e['transition']=='SUPPRESSED' and 'stale' in e['payload_json'].lower() for e in events))
        self.assertTrue(self.store.verify_chain())


if __name__=='__main__':unittest.main()
