"""Synthetic Phase 5A snapshot constructors for isolated Phase 5B tests."""
from datetime import date, datetime, timezone

from dual_ma_ai.alpaca_data import POLICY, SOURCE
from dual_ma_ai.wallet import TICKERS
from dual_ma_ai.strategy import PRESETS

UTC = timezone.utc
DAY = date(2024, 1, 2)
CLOSED = '2024-01-02T21:00:00+00:00'
USABLE = '2024-01-03T05:16:00+00:00'
FETCHED = '2024-01-03T05:20:00+00:00'
AS_OF = datetime(2024, 1, 3, 6, 0, tzinfo=UTC)


def row_for(ticker, *, day='2024-01-02', sides=None, status='VALIDATED', payload_hash=None):
    sides = sides or {}
    return dict(ticker=ticker,status=status,feed='iex',source=SOURCE,candle=day,closed_at=CLOSED,
        usable_at=USABLE,fetched_at=FETCHED,payload_sha256=payload_hash or ('a'*64),
        adjustment_policy=POLICY,
        validation=dict(status='VALIDATED',feed='iex',data_kind='REAL',sessions=250,
            last_session=day,latest_completed_candle=CLOSED,latest_usable_at=USABLE,
            missing_internal_sessions=0,excluded_incomplete=0,warnings=[]),
        signals=[dict(strategy=s.name,signal=sides.get(s.name,'NO CROSS'),candle=day,
                      usable_at=USABLE,evaluated_at=FETCHED,explanation='Confirmed synthetic test decision.')
                 for s in PRESETS],
        explanation='Synthetic monitor row for isolated tests.')


def snapshot(*, sides=None, blocked=(), stale=(), incomplete=(), status='RUNNING', day='2024-01-02'):
    rows=[]
    sides=sides or {}
    for ticker in TICKERS:
        if ticker in blocked:
            rows.append(dict(ticker=ticker,status='BLOCKED',signals=[],explanation='Synthetic missing history; blocked by validation.'))
        elif ticker in stale:
            rows.append(dict(ticker=ticker,status='STALE',signals=[],explanation='Synthetic stale completed candle; decisions blocked.'))
        else:
            per_ticker=sides.get(ticker,{})
            row=row_for(ticker,day=day,sides=per_ticker)
            if ticker in incomplete:row['signals']=row['signals'][:-1]
            rows.append(row)
    return dict(status=status,mode='READ_ONLY',paper_execution_enabled=False,in_progress=False,
        schedule=dict(expected_candle=day,calendar='XNYS'),daily=rows,heartbeat=FETCHED,
        phase='idle',last_successful_refresh=FETCHED)
