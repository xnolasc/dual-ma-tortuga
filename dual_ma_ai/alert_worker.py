"""Explicitly invoked, local advisory-alert evaluator; never starts automatically."""
import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import sys
import time

from .alert_evaluator import AlertEvaluator
from .alert_provenance import CachedPolicyResolver
from .alert_policy import PolicySet
from .alert_store import AlertStore
from .monitor import STATE_ROOT
from .research import ROOT

ALERT_ROOT = ROOT / 'data/dual_ma_ai/alerts'
DEFAULT_ALERT_DB = ALERT_ROOT / 'alerts.sqlite3'


def run(monitor_root=STATE_ROOT, alert_db=DEFAULT_ALERT_DB, policy=None,
        *, once=False, interval=60, stop=None):
    store = AlertStore(alert_db)
    evaluator = AlertEvaluator(store, monitor_root, policies=PolicySet.load(policy),
        policy_resolver=CachedPolicyResolver(Path(monitor_root) / 'history'))
    lock_path = Path(alert_db).with_suffix('.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another Phase 5B alert worker owns the process lock') from None
        while True:
            print(json.dumps(evaluator.tick()), flush=True)
            if once or (stop is not None and stop()):
                return
            time.sleep(max(1, min(int(interval), 3600)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--monitor-root', type=Path, default=STATE_ROOT)
    parser.add_argument('--alert-db', type=Path, default=DEFAULT_ALERT_DB)
    parser.add_argument('--policy', type=Path, default=os.environ.get('DUAL_MA_ALERT_POLICY'))
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    try:
        run(args.monitor_root, args.alert_db, args.policy, once=args.once, interval=args.interval)
    except (OSError, ValueError, RuntimeError) as exc:
        # Exception text is restricted to local state/config errors; do not log file contents.
        parser.exit(2, f'Alert worker stopped safely: {type(exc).__name__}: {exc}\n')


if __name__ == '__main__':
    main()
