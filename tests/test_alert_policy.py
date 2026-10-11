import json
from pathlib import Path
import tempfile
import unittest
from datetime import date, datetime, timezone

from dual_ma_ai.alert_policy import ExpiryPolicy, PolicySet, expiry_for
from dual_ma_ai.market_data import DataError
from dual_ma_ai.strategy import PRESETS


class AlertPolicyTests(unittest.TestCase):
    def test_checked_in_daily_policy_is_explicit_per_strategy_and_timeframe(self):
        policies=PolicySet.load()
        for strategy in PRESETS:
            policy=policies.for_signal(strategy.name,'1Day')
            self.assertEqual(policy.session_ttl,1)
            self.assertIn('1Day',policy.policy_id)
        with self.assertRaises(DataError):policies.for_signal(PRESETS[0].name,'5Min')

    def test_custom_strategy_expiry_counts_exchange_sessions_across_holiday(self):
        friday=date(2024,1,12)
        one=ExpiryPolicy(PRESETS[0].name,'1Day',1)
        two=ExpiryPolicy(PRESETS[0].name,'1Day',2)
        expires_one,calendar=expiry_for(friday,one)
        expires_two,_=expiry_for(friday,two)
        self.assertEqual(expires_one,datetime(2024,1,16,21,tzinfo=timezone.utc)) # MLK Monday closure
        self.assertEqual(expires_two,datetime(2024,1,17,21,tzinfo=timezone.utc))
        self.assertIn('XNYS',calendar)

    def test_early_close_and_dst_are_calendar_aware(self):
        early,_=expiry_for(date(2024,11,27),ExpiryPolicy(PRESETS[0].name,'1Day',1))
        # Friday after Thanksgiving is an XNYS early close (13:00 EST).
        self.assertEqual(early,datetime(2024,11,29,18,tzinfo=timezone.utc))
        winter,_=expiry_for(date(2024,3,8),ExpiryPolicy(PRESETS[0].name,'1Day',1))
        self.assertEqual(winter,datetime(2024,3,11,20,tzinfo=timezone.utc)) # EDT close

    def test_policy_rejects_unsupported_intraday_and_bad_ttl(self):
        doc=json.loads(Path('dual_ma_ai/alert_policy_defaults.json').read_text())
        doc['policies'][0]['timeframe']='5Min'
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'policy.json';path.write_text(json.dumps(doc))
            with self.assertRaises(DataError):PolicySet.load(path)
        with self.assertRaises(DataError):expiry_for(date(2024,1,2),ExpiryPolicy(PRESETS[0].name,'5Min',1))
        with self.assertRaises(DataError):expiry_for(date(2024,1,2),ExpiryPolicy(PRESETS[0].name,'1Day',0))

    def test_custom_daily_policy_can_differ_by_strategy(self):
        doc=json.loads(Path('dual_ma_ai/alert_policy_defaults.json').read_text())
        doc['policies'][1]['session_ttl']=3
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'policy.json';path.write_text(json.dumps(doc))
            policies=PolicySet.load(path)
        self.assertEqual(policies.for_signal(PRESETS[1].name,'1Day').session_ttl,3)
        self.assertEqual(policies.for_signal(PRESETS[0].name,'1Day').session_ttl,1)


if __name__=='__main__':unittest.main()
