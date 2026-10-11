CREATE TABLE audit_events (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    signal_id TEXT,
    transition TEXT NOT NULL,
    at_utc TEXT NOT NULL,
    source_snapshot_id TEXT,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL
);
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'alert audit events are immutable'); END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'alert audit events are immutable'); END;

CREATE TABLE signals (
    signal_id TEXT PRIMARY KEY,
    logical_key TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    technical_side TEXT NOT NULL CHECK (technical_side IN ('BUY','SELL','NO_CROSS')),
    decision_state TEXT NOT NULL CHECK (decision_state IN ('ENTRY_REVIEW','EXIT_REVIEW','HOLD_REVIEW','NO_ACTION')),
    lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('REVIEW_PENDING','ACKNOWLEDGED','HOLD_REVIEW','NO_ACTION','EXPIRED','SUPERSEDED','SUPPRESSED')),
    validation_state TEXT NOT NULL,
    validation_reason TEXT,
    source TEXT NOT NULL,
    feed TEXT NOT NULL,
    adjustment_policy TEXT NOT NULL,
    candle_date TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    usable_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    expiry_policy_id TEXT NOT NULL,
    calendar_version TEXT NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    actionable INTEGER NOT NULL CHECK (actionable IN (0,1))
);
CREATE INDEX signals_recent ON signals(candle_date DESC, ticker, strategy);
CREATE INDEX signals_filter ON signals(actionable, decision_state, ticker, strategy, candle_date DESC);
CREATE INDEX signals_expiry ON signals(actionable, expires_at);

CREATE TABLE observations (
    observation_id TEXT PRIMARY KEY,
    source_snapshot_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candle_date TEXT,
    payload_hash TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('VALIDATED','SUPPRESSED')),
    reason TEXT,
    signal_id TEXT,
    observed_at TEXT NOT NULL
);
CREATE INDEX observations_candle ON observations(ticker, strategy, candle_date);

CREATE TABLE idempotency (
    key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    event_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE service_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
