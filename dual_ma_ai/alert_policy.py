"""Versioned, exchange-session based policy for advisory alert expiry."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import importlib.metadata
import json
from pathlib import Path

from .market_data import DataError, USCalendar
from .strategy import PRESETS

UTC = timezone.utc
DEFAULT_POLICY = Path(__file__).with_name('alert_policy_defaults.json')
MAX_SESSION_TTL = 20


@dataclass(frozen=True)
class ExpiryPolicy:
    strategy: str
    timeframe: str
    session_ttl: int
    version: int = 1

    @property
    def policy_id(self):
        return f'{self.strategy}:{self.timeframe}:sessions={self.session_ttl}:v{self.version}'


class PolicySet:
    def __init__(self, policies, *, version=1, calendar='XNYS'):
        if type(version) is not int or version < 1 or calendar != 'XNYS':
            raise DataError('Unsupported alert policy or calendar version')
        self.version, self.calendar_name = version, calendar
        self.policies = dict(policies)
        expected = {s.name for s in PRESETS}
        if set(self.policies) != {(strategy, '1Day') for strategy in expected}:
            raise DataError('A daily expiry policy is required for every supported strategy')

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path else DEFAULT_POLICY
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            raise DataError('Alert expiry policy is unavailable or invalid') from None
        if not isinstance(doc, dict) or set(doc) != {'version', 'calendar', 'policies'} or not isinstance(doc['policies'], list):
            raise DataError('Invalid alert expiry policy structure')
        expected = {s.name for s in PRESETS}
        parsed = {}
        for item in doc['policies']:
            if not isinstance(item, dict) or set(item) != {'strategy', 'timeframe', 'session_ttl'}:
                raise DataError('Invalid alert expiry policy entry')
            strategy, timeframe, ttl = item['strategy'], item['timeframe'], item['session_ttl']
            if strategy not in expected or timeframe != '1Day' or type(ttl) is not int or not 1 <= ttl <= MAX_SESSION_TTL:
                raise DataError('Unsupported strategy/timeframe or session expiry')
            key = (strategy, timeframe)
            if key in parsed:
                raise DataError('Duplicate alert expiry policy')
            parsed[key] = ExpiryPolicy(strategy, timeframe, ttl, doc['version'])
        return cls(parsed, version=doc['version'], calendar=doc['calendar'])

    def for_signal(self, strategy, timeframe):
        try:
            return self.policies[(strategy, timeframe)]
        except KeyError:
            raise DataError('No expiry policy for this strategy/timeframe') from None


def expiry_for(candle, policy, *, calendar_factory=USCalendar):
    """Return deadline at the close of the Nth next XNYS session."""
    if isinstance(candle, str):
        try:
            candle = date.fromisoformat(candle)
        except ValueError:
            raise DataError('Invalid alert candle date') from None
    if not isinstance(candle, date) or not isinstance(policy, ExpiryPolicy):
        raise DataError('A daily candle and configured expiry policy are required')
    if policy.timeframe != '1Day' or not 1 <= policy.session_ttl <= MAX_SESSION_TTL:
        raise DataError('Unsupported expiry timeframe or session TTL')
    end = candle + timedelta(days=policy.session_ttl * 4 + 14)
    calendar = calendar_factory(candle - timedelta(days=2), end + timedelta(days=2))
    sessions = calendar.sessions(candle + timedelta(days=1), end)
    if len(sessions) < policy.session_ttl:
        raise DataError('Exchange calendar does not cover the configured expiry')
    expires = calendar.bounds(sessions[policy.session_ttl - 1])[1]
    if expires.tzinfo is None:
        raise DataError('Exchange calendar returned a timezone-naive close')
    try:
        calendar_version = importlib.metadata.version('exchange-calendars')
    except importlib.metadata.PackageNotFoundError:
        calendar_version = 'unknown'
    return expires.astimezone(UTC), f'{calendar.name};exchange_calendars={calendar_version}'
