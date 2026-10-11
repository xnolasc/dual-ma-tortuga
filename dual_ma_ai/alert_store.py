"""Isolated transactional SQLite store for advisory signal alerts."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .market_data import DataError

UTC = timezone.utc
ZERO_HASH = '0' * 64
SCHEMA_VERSION = 1


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def stable_id(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def utc_iso(value):
    if value.tzinfo is None:
        raise DataError('Timezone-aware UTC time required')
    return value.astimezone(UTC).isoformat()


class AlertStore:
    """Alert-only DB. It never receives a wallet or monitor connection."""
    def __init__(self, path, *, busy_timeout_ms=250):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = max(1, min(int(busy_timeout_ms), 2000))
        self._migrate()
        self.verify_chain()

    @contextmanager
    def connect(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000,
                             isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute(f'PRAGMA busy_timeout={self.busy_timeout_ms}')
        db.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def _migrate(self):
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        try:
            db.execute('PRAGMA busy_timeout=2000')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version != 0:
                raise DataError('Unsupported alert database schema version')
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            if tables:
                raise DataError('Refusing to initialize a nonempty unversioned alert database')
            schema = Path(__file__).with_name('alert_schema.sql').read_text()
            try:
                db.executescript('BEGIN EXCLUSIVE;\n' + schema + f'\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;')
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                raise
        finally:
            db.close()

    @staticmethod
    def _append(db, *, event_id, signal_id, transition, at, source_snapshot_id, payload):
        if db.execute('SELECT 1 FROM audit_events WHERE event_id=?', (event_id,)).fetchone():
            return False
        previous = db.execute('SELECT event_hash FROM audit_events ORDER BY seq DESC LIMIT 1').fetchone()
        previous_hash = previous[0] if previous else ZERO_HASH
        payload_raw = canonical(payload).decode('utf-8')
        payload_hash = hashlib.sha256(payload_raw.encode()).hexdigest()
        at_text = utc_iso(at)
        body = canonical(dict(event_id=event_id, signal_id=signal_id, transition=transition,
                              at_utc=at_text, source_snapshot_id=source_snapshot_id,
                              payload_hash=payload_hash, payload=payload_raw)).decode()
        event_hash = hashlib.sha256((previous_hash + body).encode()).hexdigest()
        db.execute('''INSERT INTO audit_events(event_id,signal_id,transition,at_utc,source_snapshot_id,
                      payload_hash,payload_json,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?)''',
                   (event_id, signal_id, transition, at_text, source_snapshot_id,
                    payload_hash, payload_raw, previous_hash, event_hash))
        return True

    def verify_chain(self):
        previous = ZERO_HASH
        with self.connect() as db:
            for row in db.execute('SELECT * FROM audit_events ORDER BY seq'):
                body = canonical(dict(event_id=row['event_id'], signal_id=row['signal_id'],
                                      transition=row['transition'], at_utc=row['at_utc'],
                                      source_snapshot_id=row['source_snapshot_id'],
                                      payload_hash=row['payload_hash'], payload=row['payload_json'])).decode()
                expected = hashlib.sha256((previous + body).encode()).hexdigest()
                if row['previous_hash'] != previous or row['event_hash'] != expected:
                    raise DataError('Alert audit hash chain is invalid')
                previous = row['event_hash']
        return True

    @staticmethod
    def logical_signal_id(signal):
        fields = (signal['ticker'], signal['strategy'], signal['strategy_version'],
                  signal['timeframe'], signal['source'], signal['feed'],
                  signal['adjustment_policy'], signal['candle_date'], signal['technical_side'])
        return stable_id(fields), canonical(fields).decode()

    def _transition(self, db, signal_id, transition, source_snapshot_id, payload, at, discriminator=None):
        event_id = stable_id((signal_id, transition, source_snapshot_id, discriminator or ''))
        self._append(db, event_id=event_id, signal_id=signal_id, transition=transition,
                     at=at, source_snapshot_id=source_snapshot_id, payload=payload)
        return event_id

    def suppress(self, *, ticker, strategy, timeframe, candle_date, source_snapshot_id,
                 reason, reason_code, at, source='Alpaca Market Data daily bars', feed='iex'):
        logical = (ticker, strategy, timeframe, candle_date or 'UNKNOWN', 'SUPPRESSED')
        signal_id = stable_id(logical)
        payload_hash = stable_id((reason_code, reason, source_snapshot_id))
        obs_id = stable_id((source_snapshot_id, ticker, strategy, timeframe, candle_date,
                            payload_hash, 'SUPPRESSED'))
        with self.connect(write=True) as db:
            inserted = db.execute('''INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (obs_id, source_snapshot_id, ticker, strategy, timeframe, candle_date,
                 payload_hash, 'SUPPRESSED', reason, signal_id, utc_iso(at))).rowcount
            if not inserted:
                return dict(created=False, signal_id=signal_id, outcome='SUPPRESSED')
            payload = dict(reason_code=reason_code, reason=reason, ticker=ticker,
                           strategy=strategy, timeframe=timeframe, candle_date=candle_date)
            self._transition(db, signal_id, 'OBSERVED', source_snapshot_id, payload, at)
            self._transition(db, signal_id, 'SUPPRESSED', source_snapshot_id, payload, at, payload_hash)
            active = db.execute('''SELECT signal_id,source_snapshot_id FROM signals
                                   WHERE ticker=? AND strategy=? AND actionable=1''',
                                 (ticker, strategy)).fetchall()
            for prior in active:
                self._transition(db, prior['signal_id'], 'SUPPRESSED', source_snapshot_id,
                                 dict(reason_code=reason_code, reason=reason), at, payload_hash)
            db.execute('''UPDATE signals SET decision_state='NO_ACTION',lifecycle_state='SUPPRESSED',
                       validation_state='SUPPRESSED',validation_reason=?,updated_at=?,actionable=0
                       WHERE ticker=? AND strategy=? AND actionable=1''',
                       (reason, utc_iso(at), ticker, strategy))
            return dict(created=True, signal_id=signal_id, outcome='SUPPRESSED')

    def observe(self, signal, *, at):
        signal_id, logical_key = self.logical_signal_id(signal)
        source_snapshot_id = signal['source_snapshot_id']
        observation_id = stable_id((source_snapshot_id, signal_id, signal['payload_hash'],
                                    signal['technical_side']))
        side = signal['technical_side']
        decision = 'ENTRY_REVIEW' if side == 'BUY' else 'EXIT_REVIEW' if side == 'SELL' else 'NO_ACTION'
        lifecycle = 'REVIEW_PENDING' if side in ('BUY', 'SELL') else 'NO_ACTION'
        actionable = int(side in ('BUY', 'SELL') and at < signal['expires_at'])
        if not actionable and side in ('BUY', 'SELL'):
            decision, lifecycle = 'NO_ACTION', 'EXPIRED'
        payload = dict(ticker=signal['ticker'], strategy=signal['strategy'], timeframe=signal['timeframe'],
                       candle_date=signal['candle_date'], technical_side=side,
                       decision_state=decision, validation_state='VALIDATED',
                       payload_hash=signal['payload_hash'], expiry_policy_id=signal['expiry_policy_id'],
                       expires_at=utc_iso(signal['expires_at']))
        with self.connect(write=True) as db:
            inserted = db.execute('''INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (observation_id, source_snapshot_id, signal['ticker'], signal['strategy'],
                 signal['timeframe'], signal['candle_date'], signal['payload_hash'],
                 'VALIDATED', None, signal_id, utc_iso(at))).rowcount
            if not inserted:
                return dict(created=False, signal_id=signal_id, decision_state=decision)

            # A later validated bar closes prior review actionability for this profile.
            older = db.execute('''SELECT signal_id,source_snapshot_id,candle_date FROM signals
                                  WHERE ticker=? AND strategy=? AND timeframe=? AND actionable=1
                                  AND candle_date<=? AND signal_id<>?''', (signal['ticker'], signal['strategy'],
                                    signal['timeframe'], signal['candle_date'], signal_id)).fetchall()
            for prior in older:
                self._transition(db, prior['signal_id'], 'SUPERSEDED', source_snapshot_id,
                                 dict(by_candle=signal['candle_date']), at, signal_id)
                db.execute('''UPDATE signals SET decision_state='NO_ACTION',lifecycle_state='SUPERSEDED',
                           actionable=0,updated_at=? WHERE signal_id=?''',
                           (utc_iso(at), prior['signal_id']))

            old = db.execute('SELECT payload_hash FROM signals WHERE signal_id=?', (signal_id,)).fetchone()
            self._transition(db, signal_id, 'OBSERVED', source_snapshot_id, payload, at,
                             signal['payload_hash'])
            self._transition(db, signal_id, 'VALIDATED', source_snapshot_id, payload, at,
                             signal['payload_hash'])
            self._transition(db, signal_id, 'REVISED' if old and old['payload_hash'] != signal['payload_hash'] else
                             ('CREATED' if side in ('BUY','SELL') else 'NO_ACTION'),
                             source_snapshot_id, payload, at, signal['payload_hash'])
            values = (logical_key, signal['ticker'], signal['strategy'], signal['timeframe'], side,
                      decision, lifecycle, 'VALIDATED', None, signal['source'], signal['feed'],
                      signal['adjustment_policy'], signal['candle_date'], utc_iso(signal['closed_at']),
                      utc_iso(signal['usable_at']), utc_iso(signal['fetched_at']), signal['payload_hash'],
                      source_snapshot_id, signal['expiry_policy_id'], signal['calendar_version'],
                      utc_iso(signal['expires_at']) if side in ('BUY','SELL') else None,
                      utc_iso(at), utc_iso(at), actionable)
            db.execute('''INSERT INTO signals(signal_id,logical_key,ticker,strategy,timeframe,technical_side,
                       decision_state,lifecycle_state,validation_state,validation_reason,source,feed,
                       adjustment_policy,candle_date,closed_at,usable_at,fetched_at,payload_hash,
                       source_snapshot_id,expiry_policy_id,calendar_version,expires_at,created_at,updated_at,
                       actionable) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(signal_id) DO UPDATE SET decision_state=excluded.decision_state,
                       lifecycle_state=excluded.lifecycle_state,validation_state=excluded.validation_state,
                       validation_reason=excluded.validation_reason,source=excluded.source,feed=excluded.feed,
                       adjustment_policy=excluded.adjustment_policy,candle_date=excluded.candle_date,
                       closed_at=excluded.closed_at,usable_at=excluded.usable_at,fetched_at=excluded.fetched_at,
                       payload_hash=excluded.payload_hash,source_snapshot_id=excluded.source_snapshot_id,
                       expiry_policy_id=excluded.expiry_policy_id,calendar_version=excluded.calendar_version,
                       expires_at=excluded.expires_at,updated_at=excluded.updated_at,
                       acknowledged_at=NULL,acknowledged_by=NULL,actionable=excluded.actionable''',
                       (signal_id, *values))
            if not actionable and side in ('BUY','SELL'):
                self._transition(db, signal_id, 'EXPIRED', source_snapshot_id,
                                 dict(reason='Signal observed after its configured expiry'), at,
                                 signal['expiry_policy_id'])
            return dict(created=True, signal_id=signal_id, decision_state=decision,
                        actionable=bool(actionable))

    def expire(self, now, *, limit=100):
        now_text = utc_iso(now)
        expired = []
        with self.connect(write=True) as db:
            rows = db.execute('''SELECT signal_id,source_snapshot_id,expires_at FROM signals
                                 WHERE actionable=1 AND expires_at<=? ORDER BY expires_at LIMIT ?''',
                              (now_text, min(max(int(limit), 1), 500))).fetchall()
            for row in rows:
                payload = dict(reason='Configured exchange-session review expiry reached', expires_at=row['expires_at'])
                self._transition(db, row['signal_id'], 'EXPIRED', row['source_snapshot_id'],
                                 payload, now, row['expires_at'])
                db.execute('''UPDATE signals SET decision_state='NO_ACTION',lifecycle_state='EXPIRED',
                           actionable=0,updated_at=? WHERE signal_id=?''', (now_text, row['signal_id']))
                expired.append(row['signal_id'])
        return expired

    def acknowledge(self, signal_id, *, choice, key, actor, now):
        if choice not in ('acknowledge', 'hold'):
            raise DataError('Unsupported advisory acknowledgment')
        if not isinstance(key, str) or not 16 <= len(key) <= 100 or not key.isascii():
            raise DataError('A valid acknowledgment idempotency key is required')
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 80:
            raise DataError('A local actor label is required')
        request = dict(signal_id=signal_id, choice=choice, actor=actor.strip())
        request_hash = stable_id(request)
        at_text = utc_iso(now)
        event_id = stable_id(('ack', key))
        with self.connect(write=True) as db:
            previous = db.execute('SELECT * FROM idempotency WHERE key=?', (key,)).fetchone()
            if previous:
                if previous['operation'] != 'ack' or previous['request_hash'] != request_hash:
                    raise DataError('Idempotency key was used for a different acknowledgment')
                return json.loads(previous['response_json'])
            row = db.execute('SELECT * FROM signals WHERE signal_id=?', (signal_id,)).fetchone()
            if row is None or not row['actionable'] or row['expires_at'] <= at_text:
                raise DataError('Alert is no longer actionable for review')
            if choice == 'hold' and row['lifecycle_state'] not in ('ACKNOWLEDGED','HOLD_REVIEW'):
                # HOLD_REVIEW is explicitly human-selected after acknowledgment.
                if row['lifecycle_state'] != 'REVIEW_PENDING':
                    raise DataError('Alert cannot enter HOLD_REVIEW from its current state')
            state = 'HOLD_REVIEW' if choice == 'hold' else row['decision_state']
            lifecycle = 'HOLD_REVIEW' if choice == 'hold' else 'ACKNOWLEDGED'
            payload = dict(choice=choice, actor=actor.strip(), decision_state=state, at=at_text)
            self._append(db, event_id=event_id, signal_id=signal_id, transition='ACKNOWLEDGED',
                         at=now, source_snapshot_id=row['source_snapshot_id'], payload=payload)
            if choice == 'hold':
                self._transition(db, signal_id, 'HOLD_REVIEW', row['source_snapshot_id'], payload, now, key)
            db.execute('''UPDATE signals SET decision_state=?,lifecycle_state=?,acknowledged_at=?,
                       acknowledged_by=?,updated_at=? WHERE signal_id=?''',
                       (state, lifecycle, at_text, actor.strip(), at_text, signal_id))
            response = dict(signal_id=signal_id, decision_state=state, lifecycle_state=lifecycle,
                            acknowledged_at=at_text)
            db.execute('INSERT INTO idempotency VALUES(?,?,?,?,?,?)',
                       (key, 'ack', request_hash, canonical(response).decode(), event_id, at_text))
            return response

    def list_signals(self, *, ticker=None, strategy=None, decision_state=None, actionable=None, limit=100):
        limit = min(max(int(limit), 1), 200)
        where, args = [], []
        for field, value in (('ticker', ticker), ('strategy', strategy), ('decision_state', decision_state)):
            if value is not None:
                where.append(field + '=?'); args.append(value)
        if actionable is not None:
            where.append('actionable=?'); args.append(int(bool(actionable)))
        query = 'SELECT * FROM signals' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY candle_date DESC,created_at DESC LIMIT ?'
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, (*args, limit))]

    def recent_events(self, *, limit=100):
        limit = min(max(int(limit), 1), 200)
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM audit_events ORDER BY seq DESC LIMIT ?', (limit,))]

    def list_suppressions(self, *, ticker=None, strategy=None, limit=100):
        limit = min(max(int(limit), 1), 200)
        where, args = ["outcome='SUPPRESSED'"], []
        if ticker is not None:
            where.append('ticker=?');args.append(ticker)
        if strategy is not None:
            where.append('strategy=?');args.append(strategy)
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM observations WHERE '+
                ' AND '.join(where)+' ORDER BY observed_at DESC,observation_id LIMIT ?',(*args,limit))]

    def service_state(self):
        with self.connect() as db:
            return {row['key']: json.loads(row['value']) for row in db.execute('SELECT key,value FROM service_state')}

    def save_service_state(self, values):
        with self.connect(write=True) as db:
            for key, value in values.items():
                db.execute('''INSERT INTO service_state VALUES(?,?) ON CONFLICT(key)
                              DO UPDATE SET value=excluded.value''',
                           (key, canonical(value).decode()))

    def runtime_event(self, event_id, transition, payload, at):
        with self.connect(write=True) as db:
            return self._append(db, event_id=event_id, signal_id=None,
                                transition=transition, at=at, source_snapshot_id=None,
                                payload=payload)
