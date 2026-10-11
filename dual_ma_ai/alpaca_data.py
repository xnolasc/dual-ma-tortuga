"""Alpaca daily market data only. No brokerage/account/order API capability."""
import argparse
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from collections import OrderedDict
from pathlib import Path
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from zoneinfo import ZoneInfo

from .market_data import (Bar, Dataset, DataError, HistoryUnavailable, USCalendar,
                          number, timestamp, strict_json, digest, canonical, validate_bars)
from .wallet import TICKERS

ROOT = Path(__file__).resolve().parent.parent
CACHE_ROOT = ROOT / 'data/dual_ma_ai/alpaca'
SOURCE = 'Alpaca Market Data daily bars'
# Additional original LEGACY equities are data/replay-only; modern wallet universe is unchanged.
SUPPORTED_SYMBOLS = TICKERS + ('SNDK', 'WDC', 'BE')
POLICY = 'raw-fills; paired-provider-split-adjusted-OHLCV; rational-split-events; causal-forward-signals; price-return-dividends-excluded-v1'
WARNINGS = ('PRICE RETURN ONLY: cash dividends, spin-offs and other distributions are excluded; not total return.',
            'Retrospective ticker universe; asof=- disables predecessor symbol mapping, not survivorship bias.',
            'Provider-adjusted pairs infer split events; historical provider revisions are not point-in-time snapshots.',
            'IEX is a single-exchange feed; SIP is consolidated. Never combine feeds within a research run.',
            'Daily aggregates are usable only after the New York day ends plus 16 minutes; calendar close timestamps remain regular-session references. Extended-hours volume and later provider corrections may differ from regular-session data.')


@dataclass(frozen=True)
class Credentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)
    feed: str = 'iex'
    origin: str = 'environment'


def load_credentials(env=None):
    """Only the two authorized process variables. Never consult files/registrations."""
    values = os.environ if env is None else env
    key = values.get('ALPACA_API_KEY', '').strip()
    secret = values.get('ALPACA_SECRET_KEY', '').strip()
    if not key or not secret:
        raise DataError('ALPACA_API_KEY and ALPACA_SECRET_KEY must be present in the process environment')
    if any(len(value) > 256 or any(ord(char) < 33 or ord(char) > 126 for char in value) for value in (key, secret)):
        raise DataError('Invalid Alpaca credential encoding; values are never logged')
    return Credentials(key, secret)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError('Market-data redirects refused')


class AlpacaHTTPError(DataError):
    def __init__(self, status, attempts=1, duration_ms=0):
        self.status, self.attempts, self.duration_ms = status, attempts, duration_ms
        super().__init__(f'Alpaca market-data HTTP {status}; no feed fallback performed')


class AlpacaTransportError(DataError):
    """Sanitized transport failure metadata; never retains provider messages."""
    def __init__(self, exception_class, attempts, duration_ms):
        self.exception_class, self.attempts, self.duration_ms = exception_class, attempts, duration_ms
        super().__init__('Alpaca market-data transport failed after bounded retries')


class Client:
    """Closed GET endpoint allowlist, bounded pages/retries/response sizes."""
    def __init__(self, credentials, *, opener=None, sleep=time.sleep, attempts=3, timeout=20,
                 jitter=None, clock=time.monotonic):
        if not 1 <= attempts <= 5 or not 0 < timeout <= 60:
            raise DataError('Invalid bounded request settings')
        self.credentials = credentials
        self.opener = opener or build_opener(NoRedirect()).open
        self.sleep, self.attempts, self.timeout = sleep, attempts, timeout
        self.jitter, self.clock = jitter or (lambda: random.uniform(0, 0.25)), clock

    def get(self, path, query):
        if path not in ('/v2/stocks/bars', '/v2/stocks/quotes/latest'):
            raise DataError('Only allowlisted market-data endpoints are permitted')
        if query.get('feed') not in ('iex', 'sip'):
            raise DataError('Explicit IEX/SIP feed required')
        request = Request('https://data.alpaca.markets' + path + '?' + urlencode(query),
                          headers={'APCA-API-KEY-ID': self.credentials.key,
                                   'APCA-API-SECRET-KEY': self.credentials.secret}, method='GET')
        started = self.clock()
        last_transport_class = None
        for attempt in range(self.attempts):
            delay = min(2 ** attempt + max(0.0, min(float(self.jitter()), 0.25)), 10)
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    raw = response.read(10_000_001)
                if len(raw) > 10_000_000:
                    raise DataError('Alpaca page exceeds size limit')
                result = strict_json(raw)
                if not isinstance(result, dict):
                    raise DataError('Alpaca JSON object required')
                return result
            except HTTPError as exc:
                status = exc.code
                exc.close()  # Never leave provider exceptions to emit resource-warning representations.
                retryable = status == 429 or 500 <= status <= 599
                if not retryable or attempt == self.attempts - 1:
                    raise AlpacaHTTPError(status, attempt + 1, int((self.clock()-started)*1000)) from None
                retry_after = exc.headers.get('Retry-After', '') if exc.headers else ''
                if retry_after.isdigit():
                    delay = min(max(delay, int(retry_after)), 30)
                reset = exc.headers.get('X-RateLimit-Reset', '') if exc.headers else ''
                if reset.isdigit():
                    delay = min(max(delay, int(reset) - int(time.time())), 30)
            except (URLError, OSError, TimeoutError, ConnectionError) as exc:
                last_transport_class = type(exc).__name__
                if attempt == self.attempts - 1:
                    raise AlpacaTransportError(last_transport_class, attempt + 1,
                                               int((self.clock()-started)*1000)) from None
            except (json.JSONDecodeError, UnicodeError):
                raise DataError('Invalid Alpaca JSON response') from None
            except ValueError as exc:
                if isinstance(exc, DataError):
                    raise
                raise DataError('Invalid market-data request/response encoding') from None
            self.sleep(delay)
        raise AlpacaTransportError(last_transport_class or 'UnknownTransportError', self.attempts,
                                   int((self.clock()-started)*1000))

    def bars(self, ticker, start, end, feed, adjustment):
        if ticker not in SUPPORTED_SYMBOLS or feed not in ('iex', 'sip') or adjustment not in ('raw', 'split') or start > end:
            raise DataError('Invalid historical request')
        requested_end = datetime.combine(end + timedelta(days=1), datetime.min.time(), ZoneInfo('America/New_York')).astimezone(timezone.utc) - timedelta(microseconds=1)
        delayed_end = min(requested_end, datetime.now(timezone.utc) - timedelta(minutes=16))
        requested_start = datetime.combine(start, datetime.min.time(), ZoneInfo('America/New_York')).astimezone(timezone.utc)
        query = dict(symbols=ticker, timeframe='1Day', start=requested_start.isoformat(), end=delayed_end.isoformat(),
                     feed=feed, adjustment=adjustment, asof='-', sort='asc', limit=10000)
        rows, seen = [], set()
        for _ in range(100):
            result = self.get('/v2/stocks/bars', query)
            data = result.get('bars')
            if not isinstance(data, dict) or set(data) - {ticker} or not isinstance(data.get(ticker, []), list):
                raise DataError('Unexpected Alpaca bar/symbol response')
            rows.extend(data.get(ticker, []))
            if len(rows) > 100_000:
                raise DataError('Historical row limit exceeded')
            token = result.get('next_page_token')
            if token is None:
                return rows
            if not isinstance(token, str) or not token or len(token) > 2048 or token in seen:
                raise DataError('Repeated or invalid pagination token')
            seen.add(token)
            query['page_token'] = token
        raise DataError('Pagination limit exceeded; partial history not published')

    def entitlements(self, now=None):
        now = now or datetime.now(timezone.utc)
        result = dict(checked_at=now.isoformat(), detection='Read-only endpoint probes; not a subscription/account query', feeds={})
        for feed in ('iex', 'sip'):
            states = {}
            for label, path, query in (
                ('historical', '/v2/stocks/bars', dict(symbols='MU', timeframe='1Day', start=(now.date()-timedelta(days=10)).isoformat(), end=(now-timedelta(minutes=16)).isoformat(), adjustment='raw', asof='-', limit=1, feed=feed)),
                ('recent', '/v2/stocks/quotes/latest', dict(symbols='MU', feed=feed))):
                try:
                    response = self.get(path, query)
                    collection = response.get('bars' if label == 'historical' else 'quotes')
                    if not isinstance(collection, dict):
                        raise DataError('Invalid feed-probe response')
                    states[label] = dict(status='PERMITTED', http_status=200, sample_available=any(bool(rows) for rows in response.get('bars', response.get('quotes', {})).values()))
                except AlpacaHTTPError as exc:
                    states[label] = dict(status='DENIED' if exc.status in (401, 403) else 'UNKNOWN', http_status=exc.status)
                except DataError:
                    states[label] = dict(status='UNKNOWN', reason='Connectivity/response failure')
            result['feeds'][feed] = states
        return result


def daily_ready_at(day):
    return datetime.combine(day + timedelta(days=1), datetime.min.time(), ZoneInfo('America/New_York')).astimezone(timezone.utc) + timedelta(minutes=16)


def parse_pair(ticker, payload, fetched_at, calendar=None):
    """Validate paired raw/split bars; never use backward prices as fill prices."""
    if ticker not in SUPPORTED_SYMBOLS or payload.get('feed') not in ('iex', 'sip'):
        raise DataError('Unsupported ticker or feed')
    start, end = date.fromisoformat(payload['start']), date.fromisoformat(payload['end'])
    if start > end or fetched_at.tzinfo is None:
        raise DataError('Invalid historical interval/retrieval time')
    calendar = calendar or USCalendar(start-timedelta(days=7), max(end, fetched_at.date())+timedelta(days=7))
    raw, adjusted = payload['raw'], payload['split']
    if not isinstance(raw, list) or not isinstance(adjusted, list) or len(raw) != len(adjusted):
        raise DataError('Raw/split coverage mismatch')
    if not raw:
        raise HistoryUnavailable(f'{ticker}: Alpaca returned no bars for this feed/interval')
    bars, previous_factor, excluded = [], None, 0
    previous_row_day = None
    if payload.get('asof') != '-':
        raise DataError('Explicit asof=- symbol policy required')
    for row, adj in zip(raw, adjusted):
        if not isinstance(row, dict) or not isinstance(adj, dict):
            raise DataError('Bar object required')
        try:
            if not isinstance(row.get('t'), str) or not isinstance(adj.get('t'), str):
                raise DataError('Explicit offset timestamp string required')
            instant = timestamp(row['t'])
            if instant != timestamp(adj['t']):
                raise DataError('Raw/split timestamp mismatch')
            local = instant.astimezone(ZoneInfo('America/New_York'))
            if local.hour or local.minute or local.second or local.microsecond:
                raise DataError('Alpaca daily timestamp must be midnight America/New_York')
            day = local.date()
            if not start <= day <= end:
                raise DataError('Bar outside requested interval')
            if previous_row_day is not None and day <= previous_row_day:
                raise DataError('Duplicate/unordered daily bars')
            previous_row_day = day
            opened, closed = calendar.bounds(day)
            values = tuple(number(row[k]) for k in ('o', 'h', 'l', 'c'))
            adjusted_values = tuple(number(adj[k]) for k in ('o', 'h', 'l', 'c'))
            vol, adjvol = number(row['v'], zero=True), number(adj['v'], zero=True)
            if vol.denominator != 1 or adjvol.denominator != 1:
                raise DataError('Volume must be an integer')
            factor = (adjvol / vol if vol else values[3] / adjusted_values[3]).limit_denominator(100)
            if factor <= 0:
                raise DataError('Invalid split adjustment')
            for price, normalized in zip(values, adjusted_values):
                # Alpaca rounds split-adjusted stock OHLC to cents. Validate
                # the exact rational factor against half a cent of quantization;
                # volume must still independently corroborate the same factor.
                if abs(price/factor-normalized) > max(Fraction(1,200), normalized/Fraction(1_000_000)):
                    raise DataError('Inconsistent or unsupported provider split adjustment')
            if abs(vol*factor-adjvol) > 1:
                raise DataError('Split-adjusted volume mismatch')
            split = previous_factor / factor if previous_factor is not None else Fraction(1)
            if split != 1 and (split.numerator > 10000 or split.denominator > 100):
                raise DataError('Unsupported split ratio')
            bar = Bar(day, opened, closed, *values, int(vol), adjusted_values[3], Fraction(0), split, available_at=daily_ready_at(day))
            bar.validate()
            if daily_ready_at(day) > fetched_at:
                excluded += 1
                continue
            if bars and bar.day <= bars[-1].day:
                raise DataError('Duplicate/unordered daily bars')
            if bar.high/bar.low > 2 or (bars and abs(bar.close*split/bars[-1].close-1) > Fraction(1,2)):
                raise DataError(f'{day}: extreme OHLC/return outlier requires manual source review')
            bars.append(bar)
            previous_factor = factor
        except (KeyError, TypeError) as exc:
            raise DataError('Missing/invalid Alpaca OHLCV fields') from None
    bars = tuple(bars)
    validate_bars(bars, calendar, fetched_at)
    # Missing leading history can be an IPO/feed start; trailing missing sessions are stale, not filled.
    expected = [d for d in calendar.sessions(start, min(end, fetched_at.date())) if daily_ready_at(d) <= fetched_at]
    warnings = list(WARNINGS)
    if excluded:
        warnings.append(f'{excluded} uncompleted candle(s) excluded')
    if expected and bars[-1].day < expected[-1]:
        warnings.append('STALE: latest completed requested session is unavailable')
    ds = Dataset(ticker, bars, SOURCE, fetched_at, "not supplied by Alpaca; fetched_at is retrieval time",
                 digest(payload), calendar.name, POLICY, feed=payload['feed'],
                 validation_status='VALIDATED', warnings=tuple(warnings))
    validation = dict(status='VALIDATED', feed=payload['feed'], data_kind='REAL',
                      first_session=str(bars[0].day), last_session=str(bars[-1].day),
                      latest_completed_candle=bars[-1].closed_at.isoformat(), latest_usable_at=daily_ready_at(bars[-1].day).isoformat(), sessions=len(bars),
                      excluded_incomplete=excluded, missing_internal_sessions=0,
                      requested_start=str(start), requested_end=str(end), warnings=warnings)
    return ds, validation


class AlpacaCache:
    MAX_VALIDATED_ENTRIES = 32

    def __init__(self, root=CACHE_ROOT):
        self.root = Path(root)
        self._validated = OrderedDict()
        self._validated_lock = threading.RLock()

    @staticmethod
    def _stat_identity(path):
        st=path.stat()
        return (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)

    def save(self, ticker, payload, fetched_at, calendar=None):
        ds, validation = parse_pair(ticker, payload, fetched_at, calendar)
        envelope = dict(format_version=1, source=SOURCE, policy=POLICY, ticker=ticker,
                        fetched_at=fetched_at.isoformat(), payload=payload, validation=validation)
        identity = digest(envelope)
        path = self.root / f'{ticker}-{payload["feed"]}-{identity}.json'
        self.root.mkdir(parents=True, exist_ok=True)
        import tempfile
        with tempfile.NamedTemporaryFile(dir=self.root, mode='wb', delete=False) as out:
            temporary = Path(out.name)
            out.write(canonical(envelope));out.flush();os.fsync(out.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        finally:
            temporary.unlink()
        return path

    def load(self, path, calendar=None):
        path = Path(path)
        if path.is_symlink():
            raise DataError('Cache symlink refused')
        before=self._stat_identity(path)
        if calendar is None:calendar_key=None
        elif hasattr(calendar,'start') and hasattr(calendar,'end'):
            calendar_key=(calendar.start,calendar.end)
        else:
            # Test/custom calendar implementations may not expose a range;
            # only reuse when the same calendar instance is supplied.
            calendar_key=(type(calendar).__module__,type(calendar).__qualname__,id(calendar))
        key=(str(path.resolve()),calendar_key)
        with self._validated_lock:
            cached=self._validated.get(key)
            if cached and cached[0]==before:
                self._validated.move_to_end(key)
                return cached[1]
        envelope = strict_json(path.read_bytes())
        identity = path.stem.rsplit('-', 1)[-1]
        if digest(envelope) != identity or envelope.get('source') != SOURCE or envelope.get('policy') != POLICY or envelope.get('format_version') != 1:
            raise DataError('Alpaca cache hash/source/policy mismatch')
        if not path.name.startswith(f"{envelope['ticker']}-{envelope['payload']['feed']}-"):
            raise DataError('Cache ticker/feed filename mismatch')
        ds, validation = parse_pair(envelope['ticker'], envelope['payload'], timestamp(envelope['fetched_at']), calendar)
        if validation != envelope.get('validation'):
            raise DataError('Cache validation metadata mismatch')
        if path.is_symlink() or self._stat_identity(path)!=before:
            raise DataError('Cache changed during validation; retry')
        value=(ds,validation)
        with self._validated_lock:
            self._validated[key]=(before,value)
            self._validated.move_to_end(key)
            while len(self._validated)>self.MAX_VALIDATED_ENTRIES:self._validated.popitem(last=False)
        return value

    def latest(self, ticker, feed, calendar=None):
        if ticker not in SUPPORTED_SYMBOLS or feed not in ('iex', 'sip'):
            raise DataError('Unsupported ticker/feed')
        paths = list(self.root.glob(f'{ticker}-{feed}-*.json'))
        if not paths:
            raise HistoryUnavailable(f'{ticker}: no validated Alpaca {feed.upper()} history')
        rows = [(self.load(p, calendar), p) for p in paths]
        return max(rows, key=lambda r: (r[0][0].fetched_at, r[1].name))

    def coverage(self, feed, *, end=None):
        if feed not in ('iex', 'sip'):
            raise DataError('Explicit IEX/SIP coverage feed required')
        from .backtest import years_before
        rows = []
        latest_day=end or datetime.now(timezone.utc).date()
        shared_calendar=USCalendar(date(2000,1,1),latest_day+timedelta(days=7))
        for ticker in TICKERS:
            try:
                (ds, validation), path = self.latest(ticker, feed, calendar=shared_calendar)
                evaluation_end = end or ds.bars[-1].day
                cal = shared_calendar
                horizons = []
                available = {b.day for b in ds.bars}
                for years in (1,3,5,10):
                    window = cal.sessions(years_before(evaluation_end,years), evaluation_end)
                    warmup = cal.sessions(years_before(evaluation_end,years)-timedelta(days=400), window[0]-timedelta(days=1))[-201:] if window else ()
                    missing = sorted(set(window+warmup)-available)
                    horizons.append(dict(years=years, status='available' if not missing and len(warmup)==201 else 'unavailable',
                                         missing_sessions=len(missing), reason=None if not missing else 'Actual coverage/warmup insufficient'))
                rows.append(dict(ticker=ticker, source=ds.source, feed=feed, data_kind='REAL',
                                 fetched_at=ds.fetched_at.isoformat(), evaluation_end=str(evaluation_end),
                                 **{k:v for k,v in validation.items() if k not in ('feed','data_kind')}, horizons=horizons))
            except (DataError, OSError, ValueError):
                rows.append(dict(ticker=ticker, feed=feed, data_kind='UNAVAILABLE', status='UNAVAILABLE',
                                 reason='No valid cached history; see acquisition report for request/validation failure',
                                 horizons=[dict(years=y,status='unavailable') for y in (1,3,5,10)]))
        return dict(mode='PAPER_MARKET_DATA_COVERAGE', feed=feed, rows=rows, warnings=list(WARNINGS))


def fetch(client, cache, ticker, start, end, feed, *, now=None, calendar=None):
    fetched_at = now or datetime.now(timezone.utc)
    payload = dict(feed=feed, start=str(start), end=str(end), asof='-',
                   raw=client.bars(ticker,start,end,feed,'raw'), split=client.bars(ticker,start,end,feed,'split'))
    # Explicit retrieval watermark for paired data, not a claim of point-in-time corporate-action knowledge.
    return cache.save(ticker, payload, fetched_at, calendar)


def live_probe(env=None, *, opener=None):
    values = os.environ if env is None else env
    presence = {key: bool(values.get(key, '').strip()) for key in ('ALPACA_API_KEY', 'ALPACA_SECRET_KEY')}
    if not all(presence.values()):
        return dict(checked_at=datetime.now(timezone.utc).isoformat(), credentials_present=presence,
                    authentication='NOT_ATTEMPTED', error='Authorized process environment variables unavailable',
                    feeds={feed: {kind: dict(status='UNVERIFIED', http_status=None)
                                  for kind in ('historical', 'recent')} for feed in ('iex', 'sip')})
    result = Client(load_credentials(values), opener=opener).entitlements()
    result['credentials_present'] = presence
    result['authentication'] = 'VERIFIED' if any(state['historical'].get('http_status') == 200 for state in result['feeds'].values()) else 'UNVERIFIED'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('probe','fetch','coverage'))
    parser.add_argument('--feed', choices=('iex','sip'))
    parser.add_argument('--tickers', nargs='+', choices=SUPPORTED_SYMBOLS, default=list(TICKERS))
    parser.add_argument('--start', type=date.fromisoformat, default=date(2015,1,1))
    parser.add_argument('--end', type=date.fromisoformat)
    args = parser.parse_args()
    try:
        if args.command == 'probe':
            result = live_probe()
            result['selected_feed'] = args.feed or 'iex'
            print(json.dumps(result, indent=2))
            if result['authentication'] != 'VERIFIED':
                parser.exit(2)
            return
        if args.command == 'fetch' and not args.feed:
            parser.error('Fetch requires explicit --feed iex or sip; no automatic feed selection')
        credentials = load_credentials() if args.command != 'coverage' else None
        feed = args.feed or (credentials.feed if credentials else None)
        if feed is None:
            parser.error('Coverage requires an explicit --feed')
        cache = AlpacaCache()
        if args.command == 'coverage':
            print(json.dumps(cache.coverage(feed,end=args.end),indent=2));return
        client = Client(credentials)
        if args.command == 'probe':
            result=client.entitlements();result['selected_feed']=feed
        else:
            now = datetime.now(timezone.utc)
            end = args.end or now.date()
            if args.start > end or end > now.date() or len(set(args.tickers)) != len(args.tickers):
                raise DataError('Invalid date range or duplicate tickers')
            result=dict(mode='PAPER_MARKET_DATA_ACQUISITION',feed=feed,fetched_at=now.isoformat(),rows=[])
            for ticker in args.tickers:
                try:
                    path=fetch(client,cache,ticker,args.start,end,feed)
                    ds, validation=cache.load(path)
                    result['rows'].append(dict(ticker=ticker,cache=path.name,**validation))
                except (DataError,OSError,ValueError) as exc:
                    result['rows'].append(dict(ticker=ticker,status='UNAVAILABLE',reason=str(exc)))
            cache.root.mkdir(parents=True,exist_ok=True)
            (cache.root/f'acquisition-{feed}.json').write_bytes(canonical(result))
        print(json.dumps(result,indent=2))
    except (DataError,OSError,ValueError):
        parser.exit(2,'Alpaca operation failed; credentials and provider bodies are never logged.\n')


if __name__ == '__main__':
    main()
