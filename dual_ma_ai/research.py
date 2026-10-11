"""Explicit research CLI. Fixed cache/report directories; no wallet database argument."""
import argparse
import csv
import json
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from .backtest import Execution, compare
from .market_data import (Bar, DataError, Dataset, HistoryCache, USCalendar, digest,
                          fetch_history, strict_json)
from .strategy import PRESETS, Strategy
from .wallet import TICKERS, WalletError

ROOT = Path(__file__).resolve().parent.parent
CACHE_ROOT = ROOT / 'data' / 'dual_ma_ai' / 'history'
REPORT_ROOT = ROOT / 'data' / 'dual_ma_ai' / 'backtests'


def demo_datasets(tickers, end, calendar):
    """Deliberately synthetic fixture, never a historical performance claim."""
    sessions = calendar.sessions(date(2014, 1, 2), end)
    datasets = {}
    for ticker in tickers:
        bars = []
        split_factor = 1
        index_offset = TICKERS.index(ticker) * 13
        for index, day in enumerate(sessions):
            split = 2 if index == 1000 else 1
            split_factor *= split
            # Cyclical exact prices guarantee multiple crosses in all presets.
            phase = (index + index_offset) % 300
            value = Fraction(10000 + (phase if phase < 150 else 300 - phase) * 30, 100 * split_factor)
            opened, closed = calendar.bounds(day)
            bars.append(Bar(day, opened, closed, value, value + Fraction(1, split_factor),
                            value - Fraction(1, split_factor), value, 1_000_000,
                            value, Fraction(0), Fraction(split)))
        source = 'SYNTHETIC deterministic triangle/split fixture v1; NOT market data'
        datasets[ticker] = Dataset(ticker, tuple(bars), source,
                                  datetime(2026, 10, 10, tzinfo=timezone.utc), end.isoformat(),
                                  digest({'source': source, 'ticker': ticker, 'end': end.isoformat()}), calendar.name)
    return datasets


def write_report(report):
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    identity = digest(report)
    json_path = REPORT_ROOT / f'{identity}.json'
    json_path.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    summary = []
    for item in report['comparisons']:
        for run in item['runs']:
            run_id = run['run_id']
            with (REPORT_ROOT / f'{run_id}-equity.csv').open('w', newline='') as stream:
                fields = ('day', 'cash', 'market_value', 'equity', 'realized_pnl', 'unrealized_pnl', 'net_contributions')
                writer = csv.DictWriter(stream, fields, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(run['equity_curve'])
            summary.append({'years': item['years'], 'strategy': run['config']['strategy'], **run['metrics']})
        if not item['runs']:
            summary.append({'years': item['years'], 'status': item['status'], 'reason': item['reason']})
    return {'mode': report['mode'], 'synthetic': report.get('synthetic', False),
            'evaluation_end': report.get('evaluation_end'),
            'report': str(json_path), 'results': summary, 'warnings': report['warnings']}


def main():
    parser = argparse.ArgumentParser(description='Explicit PAPER historical research; never accesses persistent wallet')
    commands = parser.add_subparsers(dest='command', required=True)
    fetch = commands.add_parser('fetch', help='Retrieve/cache full Alpha Vantage daily adjusted history')
    fetch.add_argument('--tickers', nargs='+', choices=TICKERS, default=list(TICKERS))
    imported = commands.add_parser('import-response', help='Validate/cache a previously retrieved provider JSON response')
    imported.add_argument('ticker', choices=TICKERS)
    imported.add_argument('path', type=Path)
    imported.add_argument('--fetched-at', required=True, help='Actual timezone-aware original retrieval timestamp')
    run = commands.add_parser('compare', help='Compare presets on the same isolated portfolio/window')
    run.add_argument('--provider', choices=('alpaca', 'alpha-vantage'), default='alpaca')
    run.add_argument('--feed', choices=('iex', 'sip'), help='Required explicit feed for real Alpaca research')
    run.add_argument('--tickers', nargs='+', choices=TICKERS, default=list(TICKERS))
    run.add_argument('--years', nargs='+', type=int, choices=(1, 3, 5, 10), default=[1, 3, 5, 10])
    run.add_argument('--end', type=date.fromisoformat)
    run.add_argument('--capital', default='2000.00')
    run.add_argument('--commission-bps', type=int, default=10)
    run.add_argument('--minimum-commission', default='0.00')
    run.add_argument('--slippage-bps', type=int, default=5)
    run.add_argument('--allocation-bps', type=int, default=1000)
    run.add_argument('--warmup', type=int, default=200)
    run.add_argument('--kind', choices=('SMA', 'EMA'), help='Run one custom strategy instead of the three presets')
    run.add_argument('--short', type=int)
    run.add_argument('--long', type=int)
    run.add_argument('--demo', action='store_true', help='Use explicitly SYNTHETIC reproducible data, no network')
    args = parser.parse_args()
    cache = HistoryCache(CACHE_ROOT)
    try:
        if args.command == 'fetch':
            if len(set(args.tickers)) != len(args.tickers):
                raise DataError('Duplicate ticker arguments')
            for ticker in args.tickers:
                path = fetch_history(ticker, cache)
                dataset = cache.load(path)
                print(json.dumps({'ticker': ticker, 'cache': str(path), 'source': dataset.source,
                                  'first': dataset.bars[0].day.isoformat(), 'last': dataset.bars[-1].day.isoformat()}))
            return
        if args.command == 'import-response':
            from .market_data import timestamp
            fetched = timestamp(args.fetched_at)
            if fetched > datetime.now(timezone.utc):
                raise DataError('Retrieval timestamp cannot be in the future')
            print(cache.save(args.ticker, strict_json(args.path.read_bytes()), fetched))
            return
        if len(set(args.tickers)) != len(args.tickers):
            raise DataError('Duplicate ticker arguments')
        if any(value is not None for value in (args.kind, args.short, args.long)):
            if any(value is None for value in (args.kind, args.short, args.long)):
                raise DataError('Custom strategy requires --kind, --short and --long together')
            strategies = (Strategy(args.kind, args.short, args.long),)
        else:
            strategies = PRESETS
        execution = Execution(args.capital, args.commission_bps, args.slippage_bps,
                              args.minimum_commission, args.allocation_bps, args.warmup)
        now = datetime.now(timezone.utc)
        if args.demo:
            end = args.end or date(2026, 10, 9)
            if not date(2015, 1, 1) <= end <= date(2026, 10, 9):
                raise DataError('Synthetic demo end must be 2015-01-01 through 2026-10-09')
            calendar = USCalendar(date(2014, 1, 1), end + timedelta(days=7))
            datasets = demo_datasets(args.tickers, end, calendar)
            now = datetime(2026, 10, 10, tzinfo=timezone.utc)
        else:
            if args.provider == 'alpaca':
                from .alpaca_data import AlpacaCache
                if not args.feed:
                    raise DataError('Alpaca research requires explicit --feed iex or sip')
                alpaca = AlpacaCache()
                datasets = {ticker: alpaca.latest(ticker, args.feed)[0][0] for ticker in args.tickers}
            else:
                datasets = {ticker: cache.latest(ticker)[0] for ticker in args.tickers}
            end = args.end or min(ds.bars[-1].day for ds in datasets.values())
            calendar = USCalendar(min(min(ds.bars[0].day for ds in datasets.values()), end.replace(year=end.year-11, day=min(end.day,28))) - timedelta(days=7),
                                  max(end, max(ds.bars[-1].day for ds in datasets.values())) + timedelta(days=7))
        report = compare(datasets, end, execution, calendar=calendar, as_of=now,
                         horizons=tuple(args.years), strategies=strategies)
        report['evaluation_end'] = end.isoformat()
        report['synthetic'] = args.demo
        report['provider'] = 'synthetic' if args.demo else args.provider
        report['feed'] = None if args.demo else args.feed
        if not args.demo:
            report['warnings'].extend(sorted({warning for ds in datasets.values() for warning in ds.warnings}))
        if args.demo:
            report['warnings'].insert(0, 'SYNTHETIC DATA ONLY: these results are software fixtures, not historical market performance.')
        print(json.dumps(write_report(report), indent=2, allow_nan=False))
        if not any(item['status'] == 'available' for item in report['comparisons']):
            parser.exit(2, 'No requested horizon has sufficient validated data and warmup.\n')
    except (DataError, WalletError, OSError, ValueError) as exc:
        parser.exit(2, f'Research request rejected: {exc}\n')


if __name__ == '__main__':
    main()
