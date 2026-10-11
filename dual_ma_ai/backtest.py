"""Ephemeral PAPER research simulator. Never imports, opens or mutates Wallet."""
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction

from .market_data import DataError, HistoryUnavailable, digest, validate_bars
from .strategy import PRESETS, signals
from .wallet import TICKERS, cents, usd

WARNINGS = (
    'Retrospective user-selected universe; not a point-in-time investable universe. '
    'No claim of survivorship-bias-free results or historical ticker-identity verification.',
    'Historical provider snapshots may contain later revisions; no point-in-time vintage guarantee.',
    'Daily open fills, fixed slippage and commissions; no liquidity/market-impact model.',
    'Dividends, only when supplied in input data, are credited on ex-date rather than pay date; Alpaca price-return inputs exclude them. Fractional split shares are paid at ex-date open.',
)


def round_half_up(value):
    value = Fraction(value)
    if value < 0:
        return -round_half_up(-value)
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def floor(value):
    return value.numerator // value.denominator


def ceil(value):
    return -floor(-value)


@dataclass(frozen=True)
class Execution:
    capital: str = '2000.00'
    commission_bps: int = 10
    slippage_bps: int = 5
    minimum_commission: str = '0.00'
    allocation_bps: int = 1000  # 10% of preceding marked equity per new position.
    warmup: int = 200  # Common comparison warmup; engine needs long+1 bars.

    def __post_init__(self):
        cents(self.capital)
        cents(self.minimum_commission, zero=True)
        if any(type(v) is not int for v in (self.commission_bps, self.slippage_bps, self.allocation_bps, self.warmup)):
            raise DataError('Execution parameters must be integers')
        if not (0 <= self.commission_bps <= 1000 and 0 <= self.slippage_bps < 10000 and
                0 < self.allocation_bps <= 10000 and 0 < self.warmup <= 1000):
            raise DataError('Execution parameters outside valid range')

    def fee(self, notional):
        return max(cents(self.minimum_commission, zero=True),
                   round_half_up(Fraction(notional * self.commission_bps, 10000)))

    def price(self, raw, side):
        adjusted = raw * 100 * Fraction(10000 + (self.slippage_bps if side == 'BUY' else -self.slippage_bps), 10000)
        value = ceil(adjusted) if side == 'BUY' else floor(adjusted)
        if value <= 0:
            raise DataError('Slipped fill price rounds to zero cents')
        return value


def metrics(curve, closed_trades, fill_count):
    """Decimal analytics, sample standard deviation, 252 sessions/year, zero risk-free rate."""
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)) as ctx:
        equities = [Decimal(point['equity']) for point in curve]
        start, end = equities[0], equities[-1]
        days = (date.fromisoformat(curve[-1]['day']) - date.fromisoformat(curve[0]['day'])).days
        cagr = ((end / start).ln() * Decimal('365.2425') / days).exp() - 1 if end > 0 and days > 0 else Decimal(-1) if end == 0 else None
        peak, drawdown = start, Decimal(0)
        for equity in equities:
            peak = max(peak, equity)
            if peak:
                drawdown = max(drawdown, (peak - equity) / peak)
        returns = [b / a - 1 for a, b in zip(equities, equities[1:])] if all(a > 0 for a in equities[:-1]) else []
        sharpe = None
        if len(returns) >= 2:
            mean = sum(returns) / len(returns)
            variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
            if variance:
                sharpe = mean / variance.sqrt() * Decimal(252).sqrt()
        profits = [Decimal(trade['pnl']) for trade in closed_trades]
        gains = sum((p for p in profits if p > 0), Decimal(0))
        losses = -sum((p for p in profits if p < 0), Decimal(0))
        def out(value):
            if value is None:
                return None
            # Short synthetic windows can annualize to enormous values. Keep those
            # finite Decimal results in scientific notation instead of failing quantize.
            if value.adjusted() >= ctx.prec - 10:
                return format(value, '.8E')
            return format(value.quantize(Decimal('0.00000001')), 'f')
        return {'cagr': out(cagr), 'max_drawdown': out(drawdown), 'sharpe': out(sharpe),
                'win_rate': out(Decimal(sum(p > 0 for p in profits)) / len(profits)) if profits else None,
                'profit_factor': out(gains / losses) if losses else None,
                'profit_factor_status': 'finite' if losses else 'no_losses' if gains else 'no_closed_profit_or_loss',
                'number_of_trades': len(closed_trades), 'number_of_fills': fill_count,
                'initial_equity': format(start, '.2f'), 'final_equity': format(end, '.2f'),
                'total_return': out(end / start - 1)}


def run_backtest(datasets, strategy, start, end, execution, *, calendar, as_of):
    """One shared-capital portfolio, fresh in-memory state on every invocation."""
    if not datasets or start > end:
        raise DataError('Nonempty universe and valid date range required')
    if execution.warmup < strategy.long:
        raise DataError('Common warmup must be at least the long MA period')
    universe = sorted(datasets)
    feeds = {d.feed for d in datasets.values()}
    if any(feeds) and len(feeds) != 1:
        raise DataError('Mixed feeds/providers prohibited; select one explicit market-data feed')
    if any(ticker not in TICKERS or datasets[ticker].ticker != ticker for ticker in universe):
        raise DataError('Dataset ticker mismatch/unsupported universe')
    sessions = calendar.sessions(start, end)
    if not sessions:
        raise HistoryUnavailable('No market sessions in requested window')
    signal_by_day, bars_by_ticker = {}, {}
    baseline_day = None
    for ticker in universe:
        dataset = datasets[ticker]
        if dataset.fetched_at.tzinfo is None or dataset.adjustment_policy is None:
            raise DataError('Dataset provenance is required')
        completed = tuple(bar for bar in dataset.bars if bar.usable_at <= as_of)
        validate_bars(completed, calendar, as_of)
        selected = {bar.day: bar for bar in completed}
        missing = set(sessions) - set(selected)
        before = tuple(bar for bar in completed if bar.day < sessions[0])
        if missing or len(before) < execution.warmup + 1:
            raise HistoryUnavailable(f'{ticker}: full window and {execution.warmup + 1} prior completed sessions required; '
                                     f'available {completed[0].day} through {completed[-1].day}')
        prior = before[-1].day
        if baseline_day is not None and baseline_day != prior:
            raise DataError('Universe calendars are not aligned')
        baseline_day = prior
        # Fixed common warmup for SMA and EMA; no signals from before the evaluation boundary.
        relevant = before[-execution.warmup - 1:] + tuple(selected[day] for day in sessions)
        from dataclasses import replace
        clipped = replace(dataset, bars=relevant)
        for signal in signals(clipped, strategy, as_of=as_of):
            if signal.day >= baseline_day:
                signal_by_day.setdefault(signal.day, []).append(signal)
        bars_by_ticker[ticker] = selected
    capital = cash = cents(execution.capital)
    realized = 0
    positions, pending, executed = {}, {}, set()
    fills, trades, events, curve = [], [], [], []

    def point(day):
        market = round_half_up(sum((Fraction(pos['quantity']) * bars_by_ticker[ticker][day].close * 100
                                  for ticker, pos in positions.items()), Fraction(0)))
        cost = sum(pos['cost'] for pos in positions.values())
        if cash < 0 or cash + cost != capital + realized:
            raise AssertionError('Paper backtest accounting invariant violated')
        return {'day': day.isoformat(), 'cash': usd(cash), 'market_value': usd(market),
                'equity': usd(cash + market), 'realized_pnl': usd(realized),
                'unrealized_pnl': usd(market - cost), 'net_contributions': usd(capital),
                'positions': {t: p['quantity'] for t, p in sorted(positions.items())}}

    curve.append(point(baseline_day))
    for signal in signal_by_day.get(baseline_day, ()):
        pending[signal.ticker] = signal
    for day in sessions:
        prior_equity = cents(curve[-1]['equity'], zero=True)
        # Actions affect only overnight holders, before any open fills on this session.
        for ticker in tuple(sorted(positions)):
            bar, pos = bars_by_ticker[ticker][day], positions[ticker]
            if bar.split != 1:
                new_quantity = pos['quantity'] * bar.split
                whole = floor(new_quantity)
                fractional = new_quantity - whole
                if fractional:
                    basis = round_half_up(Fraction(pos['cost']) * fractional / new_quantity)
                    credit = round_half_up(fractional * bar.open * 100)
                    cash += credit
                    realized += credit - basis
                    pos['cost'] -= basis
                    pos['income'] += credit - basis
                    events.append({'day': day.isoformat(), 'ticker': ticker, 'kind': 'cash_in_lieu', 'cash': usd(credit)})
                pos['quantity'] = whole
                events.append({'day': day.isoformat(), 'ticker': ticker, 'kind': 'split', 'ratio': str(bar.split)})
                if not whole:
                    trades.append({'ticker': ticker, 'entry_day': pos['entry_day'], 'exit_day': day.isoformat(),
                                   'pnl': usd(pos['income']), 'exit_reason': 'split_cash_in_lieu'})
                    del positions[ticker]
                    continue
            if bar.dividend:
                income = round_half_up(pos['quantity'] * bar.dividend * 100)
                cash += income
                realized += income
                pos['income'] += income
                events.append({'day': day.isoformat(), 'ticker': ticker, 'kind': 'dividend', 'cash': usd(income)})
        # Exit first, then entries sorted by ticker. Same order and budget for all strategies.
        requests = sorted(pending.items(), key=lambda item: (item[1].side != 'SELL', item[0]))
        for ticker, signal in requests:
            bar = bars_by_ticker[ticker][day]
            if signal.closed_at >= bar.opened_at:
                raise AssertionError('Same-bar/lookahead execution prohibited')
            if bar.volume == 0:
                events.append({'day': day.isoformat(), 'ticker': ticker, 'kind': 'deferred_zero_volume', 'key': signal.key})
                continue
            del pending[ticker]
            if signal.key in executed:
                raise AssertionError('Duplicate execution')
            executed.add(signal.key)
            pos = positions.get(ticker)
            if (signal.side == 'BUY' and pos) or (signal.side == 'SELL' and not pos):
                events.append({'day': day.isoformat(), 'ticker': ticker, 'kind': 'ignored_position_state', 'key': signal.key})
                continue
            price = execution.price(bar.open, signal.side)
            if signal.side == 'BUY':
                budget = min(cash, prior_equity * execution.allocation_bps // 10000)
                low, high = 0, budget // price
                while low < high:
                    middle = (low + high + 1) // 2
                    if middle * price + execution.fee(middle * price) <= budget:
                        low = middle
                    else:
                        high = middle - 1
                quantity = low
                if not quantity:
                    events.append({'day': day.isoformat(), 'ticker': ticker, 'kind': 'insufficient_budget', 'key': signal.key})
                    continue
                fee = execution.fee(quantity * price)
                cost = quantity * price + fee
                cash -= cost
                positions[ticker] = {'quantity': quantity, 'cost': cost, 'entry_day': day.isoformat(), 'income': 0}
            else:
                quantity = pos['quantity']
                fee = execution.fee(quantity * price)
                proceeds = quantity * price - fee
                if proceeds < 0:
                    events.append({'day': day.isoformat(), 'ticker': ticker, 'kind': 'fee_exceeds_proceeds', 'key': signal.key})
                    continue
                pnl = proceeds - pos['cost']
                cash += proceeds
                realized += pnl
                trades.append({'ticker': ticker, 'entry_day': pos['entry_day'], 'exit_day': day.isoformat(),
                               'pnl': usd(pnl + pos['income']), 'exit_reason': 'bearish_crossover'})
                del positions[ticker]
            fills.append({'ticker': ticker, 'side': signal.side, 'signal_day': signal.day.isoformat(),
                          'signal_closed_at': signal.closed_at.isoformat(), 'fill_day': day.isoformat(),
                          'fill_opened_at': bar.opened_at.isoformat(), 'quantity': quantity,
                          'price': usd(price), 'commission': usd(fee), 'key': signal.key})
        curve.append(point(day))
        for signal in signal_by_day.get(day, ()):
            old = pending.get(signal.ticker)
            if old:
                events.append({'day': day.isoformat(), 'ticker': signal.ticker, 'kind': 'superseded_signal', 'key': old.key})
            pending[signal.ticker] = signal
    config = {'strategy': asdict(strategy), 'execution': asdict(execution), 'start': start.isoformat(),
              'end': end.isoformat(), 'universe': universe, 'engine_version': 1,
              'ema_decimal_places': 24, 'cash_precision': 'USD cents, half-up',
              'fill_price_precision': 'USD cents, adverse rounding',
              'datasets': {ticker: datasets[ticker].content_hash for ticker in universe}}
    return {'mode': 'PAPER_BACKTEST', 'run_id': digest(config), 'config': config,
            'warnings': list(WARNINGS) + sorted({warning for ds in datasets.values() for warning in ds.warnings}), 'metrics': metrics(curve, trades, len(fills)),
            'equity_curve': curve, 'fills': fills, 'closed_trades': trades, 'events': events,
            'open_positions': {ticker: {'quantity': pos['quantity'], 'cost_basis': usd(pos['cost'])}
                               for ticker, pos in sorted(positions.items())},
            'unfilled_signals': [signal.key for _, signal in sorted(pending.items())],
            'provenance': {ticker: {'source': datasets[ticker].source,
                                  'feed': datasets[ticker].feed,
                                  'validation_status': datasets[ticker].validation_status,
                                  'fetched_at': datasets[ticker].fetched_at.isoformat(),
                                  'source_refreshed_at': datasets[ticker].source_refreshed_at,
                                  'calendar': datasets[ticker].calendar,
                                  'adjustment_policy': datasets[ticker].adjustment_policy,
                                  'available_first_session': datasets[ticker].bars[0].day.isoformat(),
                                  'available_last_session': datasets[ticker].bars[-1].day.isoformat(),
                                  'latest_usable_at': datasets[ticker].bars[-1].usable_at.isoformat(),
                                  'payload_sha256': datasets[ticker].content_hash} for ticker in universe}}


def years_before(day, years):
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # February 29 -> February 28 in a non-leap target year.
        return day.replace(year=day.year - years, day=28)


def compare(datasets, end, execution, *, calendar, as_of, horizons=(1, 3, 5, 10), strategies=PRESETS):
    if not strategies or execution.warmup < max(strategy.long for strategy in strategies):
        raise DataError('Shared warmup must cover every comparison strategy')
    results = []
    for years in horizons:
        if type(years) is not int or years not in (1, 3, 5, 10):
            raise DataError('Supported horizons: 1, 3, 5, 10 calendar years')
        start = years_before(end, years)
        try:
            runs = [run_backtest(datasets, strategy, start, end, execution, calendar=calendar, as_of=as_of)
                    for strategy in strategies]
            results.append({'years': years, 'status': 'available', 'runs': runs})
        except HistoryUnavailable as exc:
            results.append({'years': years, 'status': 'unavailable', 'reason': str(exc), 'runs': []})
    return {'mode': 'PAPER_BACKTEST_COMPARISON', 'warnings': list(WARNINGS), 'comparisons': results}
