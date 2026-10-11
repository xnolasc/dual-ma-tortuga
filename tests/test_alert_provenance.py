import json
import tempfile
import unittest
from pathlib import Path

from dual_ma_ai.alert_provenance import CachedPolicyResolver
from dual_ma_ai.alpaca_data import POLICY, SOURCE
from dual_ma_ai.market_data import canonical, digest
from alert_helpers import row_for


class CachedPolicyResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.row = row_for('CIEN')
        self.payload = {'feed': 'iex', 'ticker': 'CIEN', 'bars': [{'day': '2024-01-02'}]}
        self.row['payload_sha256'] = digest(self.payload)
        self.row.pop('adjustment_policy')
        self._write_envelope()
        self.resolve = CachedPolicyResolver(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def _write_envelope(self, *, policy=POLICY, ticker='CIEN', payload=None):
        value = dict(format_version=1, source=SOURCE, policy=policy, ticker=ticker,
                     fetched_at='2024-01-03T05:20:00+00:00', payload=payload or self.payload,
                     validation=self.row['validation'])
        path = self.root / f"{ticker}-iex-{digest(value)}.json"
        path.write_bytes(canonical(value))
        return path

    def test_resolves_only_when_hash_source_validation_and_policy_match(self):
        self.assertEqual(self.resolve(self.row), POLICY)

    def test_suppresses_when_payload_hash_does_not_match_cache(self):
        self.row['payload_sha256'] = 'b' * 64
        self.assertIsNone(self.resolve(self.row))

    def test_suppresses_wrong_source_or_feed(self):
        self.row['source'] = 'untrusted source'
        self.assertIsNone(self.resolve(self.row))
        self.row['source'] = SOURCE
        self.row['feed'] = 'sip'
        self.assertIsNone(self.resolve(self.row))

    def test_suppresses_universe_escape_and_wrong_policy(self):
        self.row['ticker'] = '../outside'
        self.assertIsNone(self.resolve(self.row))
        self.row['ticker'] = 'CIEN'
        source = next(self.root.glob('*.json'))
        envelope = json.loads(source.read_text())
        envelope['policy'] = 'unrecognized adjustment policy'
        source.unlink()
        (self.root / f"CIEN-iex-{digest(envelope)}.json").write_bytes(canonical(envelope))
        self.assertIsNone(self.resolve(self.row))

    def test_suppresses_wrong_validation_metadata_or_future_cache(self):
        self.row['validation']['sessions'] += 1
        self.assertIsNone(self.resolve(self.row))
        self.row['validation']['sessions'] -= 1
        envelope = json.loads(next(self.root.glob('*.json')).read_text())
        envelope['fetched_at'] = '2024-01-03T05:30:00+00:00'
        next(self.root.glob('*.json')).unlink()
        (self.root / f"CIEN-iex-{digest(envelope)}.json").write_bytes(canonical(envelope))
        self.assertIsNone(self.resolve(self.row))

    def test_suppresses_symlinked_envelope(self):
        source = next(self.root.glob('*.json'))
        link = self.root / 'CIEN-iex-linked.json'
        link.symlink_to(source)
        source.unlink()
        self.assertIsNone(self.resolve(self.row))


if __name__ == '__main__':
    unittest.main()
