"""Deterministic causal SMA/EMA crossover engine; no I/O or execution authority."""
from dataclasses import dataclass
from fractions import Fraction

from .market_data import DataError, causal_adjusted
from .legacy import LEGACY  # Named audit profile; deliberately excluded from PRESETS.


@dataclass(frozen=True)
class Strategy:
    kind: str = 'SMA'
    short: int = 50
    long: int = 200

    def __post_init__(self):
        if self.kind not in ('SMA', 'EMA') or type(self.short) is not int or type(self.long) is not int or not 0 < self.short < self.long <= 1000:
            raise DataError('Strategy requires SMA/EMA and integer periods 0 < short < long <= 1000')

    @property
    def name(self):
        return f'{self.kind}_{self.short}_{self.long}'


PRESETS = (Strategy('SMA', 50, 200), Strategy('SMA', 20, 50), Strategy('EMA', 20, 50))


@dataclass(frozen=True)
class Signal:
    ticker: str
    day: object
    closed_at: object
    side: str
    strategy: str

    @property
    def key(self):
        return f'{self.strategy}:{self.ticker}:{self.day}:{self.side}'


def average_series(prices, period, kind):
    result = []
    ema = None
    alpha = Fraction(2, period + 1)
    scale = 10**24
    def ema_precision(value):
        scaled = value * scale
        return Fraction((2 * scaled.numerator + scaled.denominator) // (2 * scaled.denominator), scale)
    rolling = Fraction(0)
    for index, price in enumerate(prices):
        if kind == 'SMA':
            rolling += price
            if index >= period:
                rolling -= prices[index-period]
            if index + 1 < period:
                result.append(None)
            else:
                result.append(rolling / period)
        elif index + 1 < period:
            rolling += price
            result.append(None)
        elif index + 1 == period:
            rolling += price
            ema = ema_precision(rolling / period)
            result.append(ema)
        else:
            # Fixed decimal precision prevents exponential denominator growth on
            # decade-long EMA runs. All rounding is deterministic integer half-up.
            ema = ema_precision(alpha * price + (1 - alpha) * ema)
            result.append(ema)
    return tuple(result)


def signals_many(dataset, strategies, *, as_of):
    """Calculate several named modern strategies from one validated price stream."""
    strategies = tuple(strategies)
    if any(not isinstance(strategy, Strategy) for strategy in strategies):
        raise DataError('Modern signals require Strategy; use legacy.replay for the separate LEGACY profile')
    if as_of.tzinfo is None:
        raise DataError('Explicit timezone-aware as-of required')
    bars = dataset.bars
    previous_day = None
    for bar in bars:
        bar.validate()
        if previous_day is not None and bar.day <= previous_day:
            raise DataError('Signals require unique chronological bars')
        previous_day = bar.day
    completed = tuple(bar for bar in bars if bar.usable_at <= as_of)
    prices = tuple(row[3] for row in causal_adjusted(completed))
    averages = {}
    for strategy in strategies:
        for kind, period in ((strategy.kind, strategy.short), (strategy.kind, strategy.long)):
            key = (kind, period)
            if key not in averages:
                averages[key] = average_series(prices, period, kind)
    results = {}
    for strategy in strategies:
        short = averages[(strategy.kind, strategy.short)]
        long = averages[(strategy.kind, strategy.long)]
        result = []
        for index in range(strategy.long, len(completed)):
            previous = short[index - 1] - long[index - 1]
            current = short[index] - long[index]
            side = 'BUY' if previous <= 0 < current else 'SELL' if previous >= 0 > current else None
            if side:
                bar = completed[index]
                result.append(Signal(dataset.ticker, bar.day, bar.usable_at, side, strategy.name))
        results[strategy.name] = tuple(result)
    return results


def signals(dataset, strategy, *, as_of):
    if not isinstance(strategy, Strategy):
        raise DataError('Modern signals require Strategy; use legacy.replay for the separate LEGACY profile')
    return signals_many(dataset, (strategy,), as_of=as_of)[strategy.name]
