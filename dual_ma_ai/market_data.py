"""Read-only historical data adapter and immutable, hash-verified local cache."""
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .wallet import TICKERS

UTC = timezone.utc
SOURCE = 'Alpha Vantage TIME_SERIES_DAILY_ADJUSTED'
POLICY = 'raw-as-traded; provider-adjclose-ratio-OHLC; split-only-volume; causal-forward-signals-v1'


class DataError(ValueError):
    pass


class HistoryUnavailable(DataError):
    pass


def number(value, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise DataError('Market values must be exact decimal strings or integers')
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise DataError('Invalid market number') from exc
    if not result.is_finite() or result < 0 or (not zero and result == 0) or result > Decimal('1e12'):
        raise DataError('Market number outside valid range')
    return Fraction(result)


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise DataError('UTC/offset timestamp required')
    return result.astimezone(UTC)


def canonical(value):
    def exact_decimal(item):
        if isinstance(item, Decimal) and item.is_finite():
            return str(item)
        raise DataError('Unsupported/nonfinite cache value')
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=exact_decimal).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DataError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_float=Decimal,
                      parse_constant=lambda _: (_ for _ in ()).throw(DataError('Nonfinite JSON value')))


class USCalendar:
    """XNYS common US equity sessions, including holidays and early closes."""
    name = 'XNYS (US equity session proxy)'

    def __init__(self, start, end):
        try:
            import exchange_calendars as xcals
        except ImportError as exc:
            raise DataError('Install requirements-backtest.txt in the repository virtual environment') from exc
        # Include adjacent sessions so weekend/holiday query endpoints remain in bounds.
        # This expands only the calendar schedule, never the market-data history.
        self.start, self.end = start, end
        self.calendar = xcals.get_calendar('XNYS', start=(start-timedelta(days=7)).isoformat(),
                                           end=(end+timedelta(days=7)).isoformat())

    @lru_cache(maxsize=128)
    def sessions(self, start, end):
        return tuple(t.date() for t in self.calendar.sessions_in_range(start.isoformat(), end.isoformat()))

    @lru_cache(maxsize=8192)
    def bounds(self, day):
        if not self.calendar.is_session(day.isoformat()):
            raise DataError(f'{day}: not a market session')
        return (self.calendar.session_open(day.isoformat()).to_pydatetime(),
                self.calendar.session_close(day.isoformat()).to_pydatetime())


@dataclass(frozen=True)
class Bar:
    day: date
    opened_at: datetime
    closed_at: datetime
    open: Fraction
    high: Fraction
    low: Fraction
    close: Fraction
    volume: int
    adjusted_close: Fraction
    dividend: Fraction = Fraction(0)
    split: Fraction = Fraction(1)
    available_at: datetime | None = None

    def validate(self):
        if self.opened_at.tzinfo is None or self.closed_at.tzinfo is None or self.opened_at >= self.closed_at:
            raise DataError('Invalid session timestamps')
        if self.available_at is not None and (self.available_at.tzinfo is None or self.available_at < self.closed_at):
            raise DataError('Invalid daily availability timestamp')
        if any(not isinstance(x, Fraction) or x <= 0 for x in
               (self.open, self.high, self.low, self.close, self.adjusted_close, self.split)):
            raise DataError('Nonpositive or inexact prices/split')
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise DataError(f'{self.day}: inconsistent OHLC')
        if type(self.volume) is not int or self.volume < 0 or self.dividend < 0:
            raise DataError('Invalid volume/dividend')

    @property
    def usable_at(self):
        return self.available_at or self.closed_at

    @property
    def adjusted_ohlc(self):
        factor = self.adjusted_close / self.close
        return tuple(value * factor for value in (self.open, self.high, self.low, self.close))


@dataclass(frozen=True)
class Dataset:
    ticker: str
    bars: tuple
    source: str
    fetched_at: datetime
    source_refreshed_at: str
    content_hash: str
    calendar: str
    adjustment_policy: str = POLICY
    feed: str | None = None
    validation_status: str | None = None
    warnings: tuple = ()

    def adjusted_ohlcv(self):
        """Descriptive provider-adjusted prices and split-adjusted volume, not fill prices."""
        remaining_splits = Fraction(1)
        rows = []
        for bar in reversed(self.bars):
            rows.append({'day': bar.day.isoformat(), 'opened_at': bar.opened_at.isoformat(),
                         'closed_at': bar.closed_at.isoformat(),
                         **{key: str(value) for key, value in zip(
                             ('open', 'high', 'low', 'close'), bar.adjusted_ohlc)},
                         'volume': str(bar.volume * remaining_splits)})
            remaining_splits *= bar.split
        return list(reversed(rows))


def validate_bars(bars, calendar, as_of):
    if as_of.tzinfo is None:
        raise DataError('An explicit timezone-aware as-of time is required')
    if not bars:
        raise HistoryUnavailable('No completed market bars')
    previous = None
    for bar in bars:
        bar.validate()
        if previous is not None and bar.day <= previous:
            raise DataError('Duplicate or unordered daily bars')
        if bar.usable_at > as_of:
            raise DataError('Incomplete daily candle')
        if calendar.bounds(bar.day) != (bar.opened_at, bar.closed_at):
            raise DataError('Bar timestamps differ from exchange calendar')
        previous = bar.day
    expected = calendar.sessions(bars[0].day, bars[-1].day)
    actual = tuple(bar.day for bar in bars)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        raise DataError(f'Missing market sessions: {", ".join(map(str, missing[:10]))}')


def parse_provider(payload, ticker, fetched_at, calendar=None):
    if ticker not in TICKERS:
        raise DataError('Unsupported ticker')
    if not isinstance(payload, dict):
        raise DataError('Provider response must be a JSON object')
    if fetched_at.tzinfo is None:
        raise DataError('Timezone-aware retrieval timestamp required')
    meta, rows = payload.get('Meta Data', {}), payload.get('Time Series (Daily)')
    if not isinstance(meta, dict) or not isinstance(rows, dict) or not rows:
        raise DataError('Provider returned no daily adjusted data (check entitlement, symbol or rate limit)')
    if meta.get('2. Symbol') != ticker or meta.get('5. Time Zone') not in ('US/Eastern', 'America/New_York'):
        raise DataError('Provider symbol/timezone mismatch')
    days = sorted(date.fromisoformat(key) for key in rows)
    calendar = calendar or USCalendar(days[0] - timedelta(days=7), max(days[-1], fetched_at.date()) + timedelta(days=7))
    bars = []
    for day in days:
        opened, closed = calendar.bounds(day)
        if closed > fetched_at:  # Never cache an uncompleted daily candle as usable data.
            continue
        row = rows[day.isoformat()]
        if not isinstance(row, dict):
            raise DataError(f'{day}: daily bar must be an object')
        try:
            volume = number(row['6. volume'], zero=True)
            if volume.denominator != 1:
                raise DataError('Volume must be an integer')
            bars.append(Bar(day, opened, closed, *(number(row[k]) for k in
                         ('1. open', '2. high', '3. low', '4. close')), int(volume),
                         number(row['5. adjusted close']), number(row['7. dividend amount'], zero=True),
                         number(row['8. split coefficient'])))
        except KeyError as exc:
            raise DataError(f'{day}: missing OHLCV or corporate-action field') from exc
    bars = tuple(bars)
    validate_bars(bars, calendar, fetched_at)
    return Dataset(ticker, bars, SOURCE, fetched_at, str(meta.get('3. Last Refreshed', 'unknown')),
                   digest(payload), calendar.name)


class HistoryCache:
    """Content-addressed snapshots. Refresh creates a new file, never overwrites history."""
    def __init__(self, root):
        self.root = Path(root)

    def save(self, ticker, payload, fetched_at, calendar=None):
        dataset = parse_provider(payload, ticker, fetched_at, calendar)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f'{ticker}-{dataset.content_hash}.json'
        envelope = {'format_version': 1, 'source': SOURCE, 'adjustment_policy': POLICY,
                    'ticker': ticker, 'fetched_at': fetched_at.isoformat(),
                    'payload_sha256': dataset.content_hash, 'payload': payload,
                    'adjusted_ohlcv': dataset.adjusted_ohlcv()}
        if not path.exists():
            with tempfile.NamedTemporaryFile(dir=self.root, suffix='.tmp', mode='wb', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(canonical(envelope))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Exclusive hard-link publication: concurrent refresh cannot replace a snapshot.
                os.link(temporary, path)
            except FileExistsError:
                pass
            finally:
                temporary.unlink()
        return path

    def load(self, path, calendar=None):
        envelope = strict_json(Path(path).read_bytes())
        if (envelope.get('format_version') != 1 or envelope.get('source') != SOURCE or
                envelope.get('adjustment_policy') != POLICY or
                digest(envelope['payload']) != envelope.get('payload_sha256')):
            raise DataError('Cache schema, source, adjustment policy or checksum mismatch')
        dataset = parse_provider(envelope['payload'], envelope['ticker'], timestamp(envelope['fetched_at']), calendar)
        if envelope.get('adjusted_ohlcv') != dataset.adjusted_ohlcv():
            raise DataError('Cached adjusted OHLCV differs from source normalization')
        return dataset

    def latest(self, ticker, calendar=None):
        if ticker not in TICKERS:
            raise DataError('Unsupported ticker')
        paths = list(self.root.glob(f'{ticker}-*.json'))
        if not paths:
            raise HistoryUnavailable(f'{ticker}: no cache; fetch explicitly or import a provider response')
        # Selection is by recorded retrieval time, not filesystem modification time.
        loaded = [(self.load(path, calendar), path) for path in paths]
        return max(loaded, key=lambda item: item[0].fetched_at)


def fetch_history(ticker, cache, *, api_key=None, now=None, opener=urlopen):
    if ticker not in TICKERS:
        raise DataError('Unsupported ticker')
    key = api_key or os.environ.get('ALPHAVANTAGE_API_KEY')
    if not key:
        raise DataError('Set ALPHAVANTAGE_API_KEY locally; daily adjusted data requires provider entitlement')
    query = urlencode({'function': 'TIME_SERIES_DAILY_ADJUSTED', 'symbol': ticker,
                       'outputsize': 'full', 'datatype': 'json', 'apikey': key})
    request = Request('https://www.alphavantage.co/query?' + query,
                      headers={'User-Agent': 'Dual-MA-AI-paper-research/1'})
    try:
        with opener(request, timeout=30) as response:
            raw = response.read(25_000_001)
        if len(raw) > 25_000_000:
            raise DataError('Provider response exceeds size limit')
        payload = strict_json(raw)
    except (URLError, OSError, json.JSONDecodeError) as exc:
        # Do not propagate credential-bearing request URLs into logs/errors.
        raise DataError('Historical data request failed; no cache was published') from None
    return cache.save(ticker, payload, now or datetime.now(UTC))


def causal_adjusted(bars):
    """Forward split/dividend-adjusted OHLCV. Never uses a future adjustment factor."""
    factor = Fraction(1)
    split_factor = Fraction(1)
    previous_close = None
    for bar in bars:
        if previous_close is not None:
            split_factor *= bar.split
            factor *= bar.split
            reference = previous_close / bar.split
            if bar.dividend:
                if bar.dividend >= reference:
                    raise DataError('Dividend exceeds prior split-adjusted close; unsupported distribution')
                factor *= reference / (reference - bar.dividend)
        # First row is the numeraire: actions before any observed holding are irrelevant.
        yield tuple(value * factor for value in (bar.open, bar.high, bar.low, bar.close)) + (Fraction(bar.volume) / split_factor,)
        previous_close = bar.close
