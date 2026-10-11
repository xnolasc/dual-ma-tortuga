-- Schema v1. Money is integer USD cents; quantities are whole shares.
CREATE TABLE IF NOT EXISTS wallet (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash INTEGER NOT NULL CHECK (typeof(cash) = 'integer' AND cash >= 0),
    contributions INTEGER NOT NULL CHECK (typeof(contributions) = 'integer'),
    realized INTEGER NOT NULL CHECK (typeof(realized) = 'integer')
);
CREATE TABLE IF NOT EXISTS positions (
    ticker TEXT PRIMARY KEY CHECK (ticker IN ('SPCX','LITE','MRVL','TSM','CIEN','COHR','MU','NVDA','AAOI')),
    quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity > 0),
    cost INTEGER NOT NULL CHECK (typeof(cost) = 'integer' AND cost > 0),
    mark INTEGER NOT NULL CHECK (typeof(mark) = 'integer' AND mark > 0)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('initial','deposit','withdraw','buy','sell','mark')),
    payload TEXT NOT NULL,
    cash_after INTEGER NOT NULL,
    contributions_after INTEGER NOT NULL,
    realized_after INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END;
PRAGMA user_version = 1;
