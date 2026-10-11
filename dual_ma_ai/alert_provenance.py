"""Read-only proof of Phase 5A policy provenance for alert eligibility.

The accepted Phase 5A monitor schema predates the adjustment_policy field in
its daily rows.  Its immutable cache envelope carries that policy.  This
adapter fills the schema gap only when the cache cryptographically binds the
same validated payload and validation metadata to the allowlisted source/feed.
"""
from pathlib import Path

from .alpaca_data import POLICY, SOURCE
from .market_data import digest, strict_json, timestamp
from .wallet import TICKERS


class CachedPolicyResolver:
    """Resolve omitted policy from at most ``max_candidates`` immutable cache files."""

    def __init__(self, history_root, *, max_candidates=64):
        if type(max_candidates) is not int or not 1 <= max_candidates <= 256:
            raise ValueError('Invalid bounded cache candidate count')
        self.history_root = Path(history_root)
        self.max_candidates = max_candidates

    def __call__(self, row):
        ticker = row.get('ticker') if isinstance(row, dict) else None
        if (ticker not in TICKERS or row.get('source') != SOURCE or
                row.get('feed') != 'iex' or row.get('status') != 'VALIDATED' or
                not isinstance(row.get('payload_sha256'), str) or
                not isinstance(row.get('validation'), dict)):
            return None
        try:
            row_fetched = timestamp(row['fetched_at'])
        except (KeyError, TypeError, ValueError):
            return None
        if not self.history_root.is_dir() or self.history_root.is_symlink():
            return None

        # Deterministic bounded read. If the exact envelope is outside this
        # lexical window, resolution fails closed instead of doing unbounded IO.
        try:
            paths = sorted(self.history_root.glob(f'{ticker}-iex-*.json'),
                           key=lambda p: p.name)[:self.max_candidates]
        except OSError:
            return None
        for path in paths:
            try:
                if path.is_symlink():
                    continue
                before = path.stat()
                envelope = strict_json(path.read_bytes())
                after = path.stat()
                if (path.is_symlink() or (before.st_dev, before.st_ino, before.st_size,
                        before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size,
                                                after.st_mtime_ns)):
                    continue
                identity = path.stem.rsplit('-', 1)[-1]
                payload = envelope.get('payload')
                fetched = timestamp(envelope['fetched_at'])
                if (digest(envelope) != identity or envelope.get('format_version') != 1 or
                        envelope.get('source') != SOURCE or envelope.get('policy') != POLICY or
                        envelope.get('ticker') != ticker or not isinstance(payload, dict) or
                        payload.get('feed') != 'iex' or digest(payload) != row['payload_sha256'] or
                        envelope.get('validation') != row['validation'] or fetched > row_fetched):
                    continue
                return POLICY
            except (OSError, KeyError, TypeError, ValueError, RuntimeError):
                continue
        return None
