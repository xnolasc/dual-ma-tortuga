"""Explicit Alpaca equity inputs for offline LEGACY rules; no modern accounting."""
import argparse
import json
from datetime import date
from fractions import Fraction

from .alpaca_data import AlpacaCache, SOURCE, WARNINGS
from .legacy import LEGACY, replay
from .market_data import DataError, causal_adjusted
from .wallet import TICKERS


def replay_history(datasets):
    if not datasets or any(t not in LEGACY.tickers or ds.ticker != t for t, ds in datasets.items()):
        raise DataError('Only the original LEGACY symbols may enter this replay')
    if any(ds.source != SOURCE or ds.validation_status != 'VALIDATED' for ds in datasets.values()):
        raise DataError('Validated Alpaca inputs required')
    if len({ds.feed for ds in datasets.values()}) != 1:
        raise DataError('LEGACY replay cannot mix market-data feeds')
    days = sorted({b.day for ds in datasets.values() for b in ds.bars})
    rows = {ticker: dict(zip((b.day for b in ds.bars), causal_adjusted(ds.bars))) for ticker, ds in datasets.items()}
    histories = {ticker: [] for ticker in datasets}
    cycles = []
    for day in days:
        snapshots = {}
        closes = []
        for ticker, ds in datasets.items():
            if day not in rows[ticker]:
                continue
            row = rows[ticker][day]
            # Source-compatible float conversion is restricted to the frozen LEGACY replay.
            histories[ticker].append(dict(high=float(row[1]), low=float(row[2]), close=float(row[3])))
            snapshots[ticker] = histories[ticker][-LEGACY.requested_bars:].copy()
            closes.extend(b.usable_at for b in ds.bars if b.day == day)
        if snapshots:
            cycles.append(dict(timestamp=int(max(closes).timestamp()),
                               klines={mode: snapshots for mode in LEGACY.sizing_modes}))
    result = replay(cycles)
    result['input_data_kind'] = 'REAL'
    result['input_provider'] = SOURCE
    result['input_feed'] = next(iter(datasets.values())).feed
    result['input_universe'] = sorted(datasets)
    result['unavailable_legacy_symbols'] = sorted(set(LEGACY.tickers)-set(datasets))
    result['provenance'] = {t: dict(source=ds.source, feed=ds.feed, fetched_at=ds.fetched_at.isoformat(),
                                  first_session=str(ds.bars[0].day), last_session=str(ds.bars[-1].day),
                                  payload_sha256=ds.content_hash) for t,ds in datasets.items()}
    result['warnings'].extend(WARNINGS)
    result['warnings'].append('ALPACA EQUITY ADAPTATION: daily completed-candle replay on causal split-normalized prices. Same-close fills and fractional float accounting remain LEGACY. This is not original Binance token/polling-history parity or a modern next-bar backtest. Normalized price/share units differ from raw shares; no corporate actions are applied to LEGACY ledgers.')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--feed',required=True,choices=('iex','sip'))
    parser.add_argument('--tickers',nargs='+',choices=list(LEGACY.tickers),default=list(LEGACY.tickers))
    args=parser.parse_args()
    try:
        cache=AlpacaCache()
        datasets={t:cache.latest(t,args.feed)[0][0] for t in args.tickers}
        print(json.dumps(replay_history(datasets),indent=2,allow_nan=False))
    except (DataError,ValueError,OSError):
        parser.exit(2,'LEGACY Alpaca replay unavailable; validated original-symbol inputs required.\n')


if __name__=='__main__':
    main()
