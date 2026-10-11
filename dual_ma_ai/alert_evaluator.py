"""Fail-closed consumer of confirmed daily Phase 5A monitor outputs."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time

from .alert_policy import PolicySet, expiry_for
from .alert_store import AlertStore, stable_id
from .alpaca_data import POLICY as ALPACA_ADJUSTMENT_POLICY, SOURCE as ALPACA_SOURCE
from .market_data import DataError, timestamp
from .monitor import TICKERS, status as monitor_status
from .strategy import PRESETS

UTC = timezone.utc
LOG = logging.getLogger('dual_ma_ai.alerts')
STRATEGIES = {strategy.name for strategy in PRESETS}
HASH_RE = re.compile(r'^[0-9a-f]{64}$')


class AlertCapacityError(RuntimeError):
    pass


class AlertEvaluator:
    """No broker, wallet or data-fetch capability; state is a separate SQLite file."""
    def __init__(self, alert_store, monitor_root, *, policies=None, source_reader=None,
                 policy_resolver=None, max_backoff_seconds=30,
                 retry_delays=(1.0, 2.0, 4.0, 8.0, 16.0)):
        self.store = alert_store if isinstance(alert_store, AlertStore) else AlertStore(alert_store)
        self.monitor_root = monitor_root
        self.policies = policies or PolicySet.load()
        self.source_reader = source_reader or monitor_status
        self.policy_resolver = policy_resolver
        self.max_backoff_seconds = max_backoff_seconds
        self.retry_delays = tuple(retry_delays[:5])
        self._gate = threading.Lock()
        saved = self.store.service_state().get('worker', {})
        self._failures = int(saved.get('failures', 0)) if isinstance(saved, dict) else 0
        try:
            self._circuit_open_until = datetime.fromisoformat(saved['circuit_open_until']) if isinstance(saved, dict) and saved.get('circuit_open_until') else None
            self._next_retry_at = datetime.fromisoformat(saved['next_retry_at']) if isinstance(saved, dict) and saved.get('next_retry_at') else None
        except (TypeError, ValueError):
            raise DataError('Alert recovery state is invalid; operator review is required') from None

    @staticmethod
    def _stable_failure_source(status, expected, progress, row=None):
        return stable_id(dict(status=status, expected_candle=expected,
                              in_progress=bool(progress), row_status=(row or {}).get('status'),
                              candle=(row or {}).get('candle'),
                              payload_hash=(row or {}).get('payload_sha256')))

    def _suppress_ticker(self, ticker, candle, source_id, code, reason, now):
        results = []
        for strategy in sorted(STRATEGIES):
            results.append(self.store.suppress(ticker=ticker, strategy=strategy, timeframe='1Day',
                candle_date=candle, source_snapshot_id=source_id, reason_code=code,
                reason=reason, at=now, source=ALPACA_SOURCE, feed='iex'))
        return results

    @staticmethod
    def _check_source(snapshot, now):
        if not isinstance(snapshot, dict):
            raise DataError('Monitor snapshot is not an object')
        status = snapshot.get('status')
        if status not in ('RUNNING', 'DEGRADED'):
            return 'MONITOR_' + str(status or 'UNKNOWN'), 'Monitor is not in an eligible healthy state.'
        if snapshot.get('in_progress'):
            return 'REFRESH_IN_PROGRESS', 'Monitor refresh is in progress; its data cannot generate alerts.'
        expected = (snapshot.get('schedule') or {}).get('expected_candle')
        if not isinstance(expected, str):
            return 'EXPECTED_CANDLE_UNAVAILABLE', 'Expected completed exchange session is unavailable.'
        return None, expected

    def _validated_row(self, row, expected_candle, now):
        if not isinstance(row, dict) or row.get('status') != 'VALIDATED':
            raise DataError('Ticker is BLOCKED, STALE, or unavailable in the monitor snapshot')
        if row.get('feed') != 'iex' or row.get('source') != ALPACA_SOURCE:
            raise DataError('Ticker source/feed is not the authorized validated source')
        if row.get('candle') != expected_candle:
            raise DataError('Ticker candle is not the latest expected completed session')
        if not HASH_RE.fullmatch(str(row.get('payload_sha256', ''))):
            raise DataError('Validated source payload hash is missing or malformed')
        validation = row.get('validation')
        if (not isinstance(validation, dict) or validation.get('status') != 'VALIDATED' or
                validation.get('data_kind') != 'REAL' or validation.get('feed') != 'iex' or
                type(validation.get('sessions')) is not int or validation['sessions'] < 201 or
                validation.get('missing_internal_sessions') != 0 or validation.get('excluded_incomplete') != 0):
            raise DataError('Daily coverage, warmup, or validation state is insufficient')
        try:
            candle = timestamp(row['closed_at'])
            usable = timestamp(row['usable_at'])
            fetched = timestamp(row['fetched_at'])
            validation_close = timestamp(validation['latest_completed_candle'])
            validation_usable = timestamp(validation['latest_usable_at'])
        except (KeyError, TypeError, ValueError):
            raise DataError('Validated candle timestamps are missing or invalid') from None
        if candle != validation_close or usable != validation_usable or usable > now or fetched > now:
            raise DataError('Candle is incomplete, future-dated, or internally inconsistent')
        if row.get('adjustment_policy') != ALPACA_ADJUSTMENT_POLICY:
            raise DataError('Daily adjustment policy is inconsistent')
        signals = row.get('signals')
        if not isinstance(signals, list) or len(signals) != len(PRESETS):
            raise DataError('Strategy output is incomplete')
        by_strategy = {}
        for signal in signals:
            if not isinstance(signal, dict) or signal.get('strategy') not in STRATEGIES or signal.get('strategy') in by_strategy:
                raise DataError('Strategy output is invalid or duplicated')
            if signal.get('candle') != expected_candle or signal.get('usable_at') != row['usable_at']:
                raise DataError('Strategy output is not tied to the validated completed candle')
            if signal.get('signal') not in ('BUY', 'SELL', 'NO CROSS'):
                raise DataError('Strategy output side is invalid')
            by_strategy[signal['strategy']] = signal
        if set(by_strategy) != STRATEGIES:
            raise DataError('Strategy output is incomplete')
        return by_strategy

    def _process_snapshot(self, snapshot, now):
        reason = self._check_source(snapshot, now)
        if reason[0]:
            code, message = reason
            expected = (snapshot.get('schedule') or {}).get('expected_candle') if isinstance(snapshot, dict) else None
            sid = self._stable_failure_source(snapshot.get('status') if isinstance(snapshot, dict) else 'INVALID',
                    expected, snapshot.get('in_progress') if isinstance(snapshot, dict) else False)
            result = []
            for ticker in TICKERS:
                result.extend(self._suppress_ticker(ticker, expected, sid, code, message, now))
            return dict(status='SUPPRESSED', reason_code=code, observations=len(result),
                        actionable_created=0)

        expected = reason[1]
        daily = snapshot.get('daily')
        if not isinstance(daily, list):
            daily = []
        by_ticker = {row.get('ticker'): row for row in daily if isinstance(row, dict) and isinstance(row.get('ticker'), str)}
        results, created = [], 0
        for ticker in TICKERS:
            row = by_ticker.get(ticker)
            try:
                if isinstance(row, dict) and 'adjustment_policy' not in row and self.policy_resolver:
                    # The resolver must independently prove policy provenance. Failure
                    # leaves the original row untouched and therefore fail-closed.
                    try:
                        resolved = self.policy_resolver(row)
                    except Exception:
                        resolved = None
                    if resolved:
                        row = dict(row, adjustment_policy=resolved)
                outputs = self._validated_row(row, expected, now)
            except (DataError, KeyError, TypeError, ValueError) as exc:
                row_status = row.get('status') if isinstance(row, dict) else 'MISSING'
                code = 'BLOCKED_DATA' if row_status == 'BLOCKED' else 'STALE_DATA' if row_status == 'STALE' else 'INVALID_DATA'
                reason_text = row.get('explanation') if isinstance(row, dict) else None
                if row_status not in ('BLOCKED', 'STALE') or not reason_text:
                    reason_text = str(exc) or reason_text or 'Ticker output failed validation.'
                candle = (row.get('candle') if isinstance(row, dict) else None) or expected
                sid = self._stable_failure_source(snapshot.get('status'), expected,
                                                  snapshot.get('in_progress'), row)
                results.extend(self._suppress_ticker(ticker, candle, sid, code, reason_text, now))
                continue

            for preset in PRESETS:
                output = outputs[preset.name]
                side = 'NO_CROSS' if output['signal'] == 'NO CROSS' else output['signal']
                policy = self.policies.for_signal(preset.name, '1Day')
                expires_at, calendar_version = expiry_for(expected, policy)
                payload_hash = stable_id(dict(source_payload=row['payload_sha256'],
                    signal=output['signal'], strategy=preset.name, candle=expected,
                    validation=row['validation']))
                source_snapshot_id = stable_id((row['payload_sha256'], ticker, 'iex', expected))
                item = dict(ticker=ticker, strategy=preset.name, strategy_version='phase5a-v1',
                    timeframe='1Day', source=ALPACA_SOURCE, feed='iex',
                    adjustment_policy=ALPACA_ADJUSTMENT_POLICY, candle_date=expected,
                    closed_at=timestamp(row['closed_at']), usable_at=timestamp(row['usable_at']),
                    fetched_at=timestamp(row['fetched_at']), payload_hash=payload_hash,
                    source_snapshot_id=source_snapshot_id, technical_side=side,
                    expiry_policy_id=policy.policy_id, calendar_version=calendar_version,
                    expires_at=expires_at)
                result = self.store.observe(item, at=now)
                results.append(result)
                created += int(bool(result.get('created') and result.get('actionable')))
        return dict(status='OK', observations=len(results), actionable_created=created,
                    blocked_tickers=[row.get('ticker') for row in daily
                                     if isinstance(row, dict) and row.get('status') in ('BLOCKED','STALE')])

    def _failure(self, now, exc):
        self._failures += 1
        base = min(self.max_backoff_seconds, 2 ** min(self._failures - 1, 5))
        jitter = int(stable_id((type(exc).__name__, self._failures))[:8], 16) / 0xffffffff * base * 0.2
        cooldown = min(self.max_backoff_seconds, base + jitter)
        if self._failures >= 5:
            self._circuit_open_until = now + timedelta(seconds=60)
        state = dict(failures=self._failures, circuit_open_until=(self._circuit_open_until.isoformat()
                     if self._circuit_open_until else None), last_error=type(exc).__name__,
                     next_retry_at=(now + timedelta(seconds=cooldown)).isoformat())
        self._next_retry_at = now + timedelta(seconds=cooldown)
        try:
            self.store.save_service_state({'worker': state})
            event_id = stable_id(('evaluator-error', type(exc).__name__,
                                  now.replace(second=0, microsecond=0).isoformat()))
            self.store.runtime_event(event_id, 'ERROR', dict(stage='evaluate',
                exception_class=type(exc).__name__, failure_count=self._failures,
                retry_after_seconds=cooldown, reason='Alert processing failed; new alerts suppressed.'), now)
        except Exception:
            LOG.error('stage=alert.failure_audit result=unavailable exception_class=%s', type(exc).__name__)
        LOG.warning('stage=alert.evaluate result=ERROR exception_class=%s failure_count=%d retry_after_seconds=%d',
                    type(exc).__name__, self._failures, cooldown)
        return dict(status='ERROR', reason='Alert evaluation unavailable; no new actionable alerts published.',
                    exception_class=type(exc).__name__, retry_after_seconds=cooldown)

    def tick(self, *, now=None):
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise DataError('Timezone-aware evaluator clock required')
        if not self._gate.acquire(blocking=False):
            raise AlertCapacityError('Alert evaluator already has one in-flight snapshot')
        try:
            if self._circuit_open_until and now < self._circuit_open_until:
                return dict(status='CIRCUIT_OPEN', retry_at=self._circuit_open_until.isoformat())
            if self._next_retry_at and now < self._next_retry_at:
                return dict(status='BACKOFF', retry_at=self._next_retry_at.isoformat())
            for attempt in range(max(1, min(len(self.retry_delays), 5))):
                try:
                    self.store.expire(now)
                    snapshot = self.source_reader(self.monitor_root, now)
                    result = self._process_snapshot(snapshot, now)
                    self._failures = 0
                    self._circuit_open_until = self._next_retry_at = None
                    self.store.save_service_state({'worker': dict(status='OK', failures=0,
                        last_evaluation_at=now.isoformat(), source_status=snapshot.get('status')
                        if isinstance(snapshot,dict) else 'INVALID')})
                    return result
                except sqlite3.OperationalError as exc:
                    transient = any(token in str(exc).lower() for token in ('locked', 'busy', 'temporarily unavailable'))
                    if not transient or attempt + 1 >= len(self.retry_delays):
                        return self._failure(now, exc)
                    base_delay=max(0.0,min(float(self.retry_delays[attempt]),self.max_backoff_seconds))
                    jitter=int(stable_id((type(exc).__name__,attempt,self._failures))[:8],16)/0xffffffff
                    time.sleep(min(self.max_backoff_seconds,base_delay*(1+0.2*jitter)))
                except Exception as exc:
                    return self._failure(now, exc)
            return self._failure(now, RuntimeError('Bounded alert database retry exhausted'))
        finally:
            self._gate.release()
