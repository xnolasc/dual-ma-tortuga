import hashlib
import io
import importlib.util
import json
import math
import statistics
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal, ROUND_DOWN, localcontext
from fractions import Fraction as F
from pathlib import Path
from unittest.mock import patch

from dual_ma_ai.backtest import Execution, compare, metrics, run_backtest, years_before
from dual_ma_ai.market_data import (Bar, DataError, Dataset, HistoryCache, HistoryUnavailable,
                                   POLICY, USCalendar, causal_adjusted, fetch_history, parse_provider,
                                   strict_json, validate_bars)
from dual_ma_ai.strategy import PRESETS, Strategy, average_series, signals, signals_many

UTC = timezone.utc


class FixtureCalendar:
    """Explicit artificial weekday schedule, never a production calendar fallback."""
    name = 'SYNTHETIC weekday fixture calendar'

    def sessions(self, start, end):
        days = []
        while start <= end:
            if start.weekday() < 5:
                days.append(start)
            start += timedelta(days=1)
        return tuple(days)

    def bounds(self, day):
        if day.weekday() > 4:
            raise DataError('Fixture non-session')
        return datetime.combine(day, time(14), UTC), datetime.combine(day, time(21), UTC)


CAL = FixtureCalendar()
NOW = datetime(2030, 1, 1, tzinfo=UTC)


def dataset(prices, ticker='MU', start=date(2024, 1, 1)):
    days = CAL.sessions(start, start + timedelta(days=len(prices) * 2))[:len(prices)]
    bars = tuple(Bar(day, *CAL.bounds(day), F(str(price)), F(str(price)), F(str(price)),
                     F(str(price)), 1000, F(str(price))) for day, price in zip(days, prices))
    return Dataset(ticker, bars, 'SYNTHETIC fixture', NOW, days[-1].isoformat(),
                   hashlib.sha256(str(prices).encode()).hexdigest(), CAL.name)


def payload(ds):
    def decimal_string(value):
        with localcontext() as ctx:
            ctx.prec = 40
            return str(Decimal(value.numerator) / Decimal(value.denominator))
    return {'Meta Data': {'2. Symbol': ds.ticker, '3. Last Refreshed': ds.bars[-1].day.isoformat(),
                          '5. Time Zone': 'US/Eastern'},
            'Time Series (Daily)': {bar.day.isoformat(): {
                '1. open': decimal_string(bar.open), '2. high': decimal_string(bar.high),
                '3. low': decimal_string(bar.low), '4. close': decimal_string(bar.close),
                '5. adjusted close': decimal_string(bar.adjusted_close), '6. volume': str(bar.volume),
                '7. dividend amount': decimal_string(bar.dividend), '8. split coefficient': decimal_string(bar.split)}
                for bar in ds.bars}}


class StrategyTests(unittest.TestCase):
    def test_rolling_average_matches_exact_reference_and_batched_presets(self):
        prices=tuple(F((i*37)%113+1,7) for i in range(260))
        for kind in ('SMA','EMA'):
            for period in (1,2,7,20,50,200):
                expected=[];ema=None
                for index,price in enumerate(prices):
                    if kind=='SMA':
                        expected.append(None if index+1<period else sum(prices[index-period+1:index+1],F(0))/period)
                    elif index+1<period:expected.append(None)
                    elif index+1==period:
                        raw=sum(prices[:period],F(0))/period
                        scaled=raw*10**24
                        ema=F((2*scaled.numerator+scaled.denominator)//(2*scaled.denominator),10**24)
                        expected.append(ema)
                    else:
                        alpha=F(2,period+1);scaled=(alpha*price+(1-alpha)*ema)*10**24
                        ema=F((2*scaled.numerator+scaled.denominator)//(2*scaled.denominator),10**24)
                        expected.append(ema)
                self.assertEqual(average_series(prices,period,kind),tuple(expected),(kind,period))
        ds=dataset([((i*37)%113+1)/7 for i in range(260)])
        batched=signals_many(ds,PRESETS,as_of=NOW)
        self.assertEqual(set(batched),{strategy.name for strategy in PRESETS})
        for strategy in PRESETS:
            self.assertEqual(batched[strategy.name],signals(ds,strategy,as_of=NOW))

    def test_presets_and_invalid_parameters(self):
        self.assertEqual([s.name for s in PRESETS], ['SMA_50_200', 'SMA_20_50', 'EMA_20_50'])
        for args in [('SMA', 0, 2), ('SMA', 2, 2), ('EMA', 4, 2), ('BAD', 1, 2), ('SMA', True, 2)]:
            with self.assertRaises(DataError):
                Strategy(*args)

    def test_known_sma_crossovers_and_equality(self):
        ds = dataset([10, 10, 12, 8, 8, 12])
        result = signals(ds, Strategy('SMA', 1, 2), as_of=NOW)
        self.assertEqual([(s.day, s.side) for s in result],
                         [(ds.bars[2].day, 'BUY'), (ds.bars[3].day, 'SELL'), (ds.bars[5].day, 'BUY')])
        self.assertEqual(len({s.key for s in result}), len(result))
        self.assertEqual(signals(dataset([10] * 220), PRESETS[0], as_of=NOW), ())

    def test_known_ema_seed_and_crossovers(self):
        ds = dataset([10, 10, 10, 12, 10, 8, 10, 12])
        averages = average_series(tuple(b.close for b in ds.bars), 2, 'EMA')
        self.assertEqual(averages[:3], (None, F(10), F(10)))
        self.assertLess(abs(averages[3] - F(34, 3)), F(1, 10**24))
        self.assertEqual([(s.day, s.side) for s in signals(ds, Strategy('EMA', 2, 3), as_of=NOW)],
                         [(ds.bars[3].day, 'BUY'), (ds.bars[4].day, 'SELL'), (ds.bars[7].day, 'BUY')])

    def test_completed_candles_and_warmup(self):
        ds = dataset([10, 10, 12])
        strategy = Strategy('SMA', 1, 2)
        self.assertEqual(signals(ds, strategy, as_of=ds.bars[-1].closed_at - timedelta(seconds=1)), ())
        self.assertEqual(len(signals(ds, strategy, as_of=ds.bars[-1].closed_at)), 1)
        with self.assertRaises(DataError):
            signals(ds, strategy, as_of=datetime(2024, 1, 10))
        self.assertEqual(signals(dataset([1] * 200), PRESETS[0], as_of=NOW), ())

    def test_prefix_invariance_and_future_split(self):
        ds = dataset([10, 10, 12, 8, 10, 12, 8, 10])
        strategy = Strategy('EMA', 1, 3)
        before = signals(replace(ds, bars=ds.bars[:5]), strategy, as_of=NOW)
        changed = replace(ds, bars=ds.bars[:5] + tuple(replace(b, open=b.open / 2, high=b.high / 2,
                          low=b.low / 2, close=b.close / 2, split=F(2) if i == 0 else F(1))
                          for i, b in enumerate(ds.bars[5:])))
        after = tuple(s for s in signals(changed, strategy, as_of=NOW) if s.day <= ds.bars[4].day)
        self.assertEqual(before, after)
        self.assertEqual(tuple(causal_adjusted(ds.bars[:5])), tuple(causal_adjusted(changed.bars))[:5])

    def test_split_and_dividend_continuity(self):
        ds = dataset([100, 100, 50, 49])
        bars = list(ds.bars)
        bars[2] = replace(bars[2], split=F(2))
        bars[3] = replace(bars[3], dividend=F(1))
        adjusted = tuple(causal_adjusted(bars))
        self.assertEqual([row[3] for row in adjusted], [F(100)] * 4)
        self.assertEqual(adjusted[2][4], F(500))
        self.assertEqual(signals(replace(ds, bars=tuple(bars)), Strategy('SMA', 1, 2), as_of=NOW), ())
        bars[3] = replace(bars[3], dividend=F(50))
        with self.assertRaises(DataError):
            tuple(causal_adjusted(bars))

    def test_duplicate_and_unordered_bars(self):
        ds = dataset([10, 10, 12])
        for bars in ((ds.bars[0], ds.bars[0]), tuple(reversed(ds.bars))):
            with self.assertRaises(DataError):
                signals(replace(ds, bars=bars), Strategy('SMA', 1, 2), as_of=NOW)


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = HistoryCache(Path(self.temp.name) / 'cache')
        self.ds = dataset([10, 10, 12, 8, 10])

    def test_cache_roundtrip_metadata_and_integrity(self):
        path = self.cache.save('MU', payload(self.ds), NOW, CAL)
        result = self.cache.load(path, CAL)
        self.assertEqual(result.bars, self.ds.bars)
        self.assertEqual(result.adjustment_policy, POLICY)
        self.assertEqual(result.fetched_at, NOW)
        self.assertIn('Alpha Vantage', result.source)
        self.assertEqual(self.cache.save('MU', payload(self.ds), NOW + timedelta(days=1), CAL), path)
        self.assertEqual(self.cache.load(path, CAL).fetched_at, NOW)
        envelope = json.loads(path.read_text())
        envelope['payload']['Meta Data']['2. Symbol'] = 'NVDA'
        path.write_text(json.dumps(envelope))
        with self.assertRaises(DataError):
            self.cache.load(path, CAL)

    def test_adjusted_ohlc_ratio(self):
        ds = dataset([100, 50])
        p = payload(ds)
        p['Time Series (Daily)'][ds.bars[0].day.isoformat()]['5. adjusted close'] = '50'
        p['Time Series (Daily)'][ds.bars[1].day.isoformat()]['8. split coefficient'] = '2'
        parsed = parse_provider(p, 'MU', NOW, CAL)
        self.assertEqual(parsed.bars[0].adjusted_ohlc, (F(50),) * 4)
        self.assertEqual(parsed.bars[0].close, F(100))
        self.assertEqual(parsed.adjusted_ohlcv()[0]['volume'], '2000')
        self.assertEqual(parsed.adjusted_ohlcv()[1]['volume'], '1000')

    def test_missing_sessions_and_fields_not_forward_filled(self):
        for change in ('session', 'field', 'null', 'bad_price', 'volume', 'symbol', 'timezone'):
            p = payload(self.ds)
            row = p['Time Series (Daily)'][self.ds.bars[2].day.isoformat()]
            if change == 'session':
                del p['Time Series (Daily)'][self.ds.bars[2].day.isoformat()]
            elif change == 'field':
                del row['2. high']
            elif change == 'null':
                row['1. open'] = None
            elif change == 'bad_price':
                row['2. high'] = '1'
            elif change == 'volume':
                row['6. volume'] = '-1'
            elif change == 'symbol':
                p['Meta Data']['2. Symbol'] = 'TSM'
            else:
                p['Meta Data']['5. Time Zone'] = 'UTC'
            with self.subTest(change=change), self.assertRaises(DataError):
                self.cache.save('MU', p, NOW, CAL)
        self.assertFalse(self.cache.root.exists())

    def test_incomplete_provider_candle_excluded(self):
        result = parse_provider(payload(self.ds), 'MU', self.ds.bars[-1].closed_at - timedelta(seconds=1), CAL)
        self.assertEqual(result.bars, self.ds.bars[:-1])
        with self.assertRaises(DataError):
            validate_bars(self.ds.bars, CAL, self.ds.bars[-1].closed_at - timedelta(seconds=1))

    def test_strict_json_duplicates_nonfinite(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}'):
            with self.assertRaises(DataError):
                strict_json(raw)

    def test_no_cache_and_no_credentials(self):
        with self.assertRaises(HistoryUnavailable):
            self.cache.latest('MU', CAL)
        with patch.dict('os.environ', {}, clear=True), self.assertRaises(DataError):
            fetch_history('MU', self.cache)

    def test_fetch_transport_and_safe_errors(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, limit):
                return json.dumps(payload(dataset([10, 10, 12], start=date(2024, 1, 2)))).encode()
        request_seen = []
        def opener(request, timeout):
            request_seen.append((request, timeout))
            return Response()
        with patch('dual_ma_ai.market_data.USCalendar', return_value=CAL):
            path = fetch_history('MU', self.cache, api_key='secret-test-key', now=NOW, opener=opener)
        self.assertIn('function=TIME_SERIES_DAILY_ADJUSTED', request_seen[0][0].full_url)
        self.assertNotIn('secret-test-key', path.read_text())
        self.assertEqual(self.cache.load(path, CAL).ticker, 'MU')
        def failing(*args, **kwargs):
            raise OSError('https://example.invalid?apikey=secret-test-key')
        with self.assertRaises(DataError) as error:
            fetch_history('MU', self.cache, api_key='secret-test-key', opener=failing)
        self.assertNotIn('secret-test-key', str(error.exception))

    def test_provider_errors_are_not_cached(self):
        for p in ({'Note': 'rate limited'}, {'Information': 'premium required'}, {'Error Message': 'invalid symbol'}):
            with self.assertRaises(DataError):
                self.cache.save('MU', p, NOW, CAL)
        self.assertFalse(self.cache.root.exists())

    @unittest.skipUnless(importlib.util.find_spec('exchange_calendars'), 'Phase 3 calendar dependency not installed')
    def test_exchange_holiday_early_close_and_special_closure(self):
        cal = USCalendar(date(2024, 1, 1), date(2025, 12, 31))
        self.assertEqual(cal.sessions(date(2024, 7, 3), date(2024, 7, 5)), (date(2024, 7, 3), date(2024, 7, 5)))
        self.assertEqual(cal.bounds(date(2024, 7, 3))[1].hour, 17)
        self.assertEqual(cal.sessions(date(2025, 1, 9), date(2025, 1, 9)), ())


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.ds = dataset([10, 10, 12, 8, 12, 8, 10, 10])
        self.strategy = Strategy('SMA', 1, 2)
        self.execution = Execution(commission_bps=0, slippage_bps=0, allocation_bps=10000, warmup=2)

    def run_case(self, ds=None, execution=None, end=None):
        ds = ds or self.ds
        return run_backtest({ds.ticker: ds}, self.strategy, ds.bars[3].day, end or ds.bars[-1].day,
                            execution or self.execution, calendar=CAL, as_of=NOW)

    def test_known_next_open_fills_and_pnl(self):
        result = self.run_case()
        first, second = result['fills'][:2]
        self.assertEqual((first['signal_day'], first['fill_day'], first['quantity'], first['price']),
                         (self.ds.bars[2].day.isoformat(), self.ds.bars[3].day.isoformat(), 250, '8.00'))
        self.assertEqual(second['price'], '12.00')
        self.assertEqual(result['closed_trades'][0]['pnl'], '1000.00')
        for fill in result['fills']:
            self.assertLess(fill['signal_closed_at'], fill['fill_opened_at'])
            self.assertLess(fill['signal_day'], fill['fill_day'])
        self.assertEqual(len({f['key'] for f in result['fills']}), len(result['fills']))
        for point in result['equity_curve']:
            self.assertGreaterEqual(F(point['cash']), 0)
            self.assertEqual(F(point['equity']), F(point['net_contributions']) + F(point['realized_pnl']) + F(point['unrealized_pnl']))

    def test_commission_slippage_and_whole_share_cash_limit(self):
        result = self.run_case(execution=replace(self.execution, commission_bps=100, slippage_bps=100, minimum_commission='1.00'))
        first = result['fills'][0]
        self.assertEqual(first['price'], '8.08')
        self.assertEqual(first['quantity'], 245)
        self.assertEqual(first['commission'], '19.80')
        self.assertEqual(result['fills'][1]['price'], '11.88')
        self.assertLess(F(result['closed_trades'][0]['pnl']), F(1000))

    def test_zero_volume_defers_to_next_eligible_bar(self):
        bars = list(self.ds.bars)
        bars[3] = replace(bars[3], volume=0, open=F(12), close=F(12), high=F(12), low=F(12))
        result = self.run_case(replace(self.ds, bars=tuple(bars)))
        self.assertEqual(result['fills'][0]['fill_day'], bars[4].day.isoformat())
        self.assertTrue(any(e['kind'] == 'deferred_zero_volume' for e in result['events']))

    def test_no_forced_last_close_liquidation(self):
        result = self.run_case(end=self.ds.bars[3].day)
        self.assertEqual(len(result['fills']), 1)
        self.assertEqual(result['metrics']['number_of_trades'], 0)
        self.assertIn('MU', result['open_positions'])
        self.assertTrue(result['unfilled_signals'])
        self.assertIsNone(result['metrics']['win_rate'])

    def test_future_changes_do_not_change_prior_fills(self):
        prefix = self.run_case(end=self.ds.bars[4].day)
        bars = self.ds.bars[:5] + tuple(replace(b, open=F(100), high=F(100), low=F(100), close=F(100)) for b in self.ds.bars[5:])
        full = self.run_case(replace(self.ds, bars=bars))
        self.assertEqual(prefix['fills'], [f for f in full['fills'] if f['fill_day'] <= self.ds.bars[4].day.isoformat()])
        self.assertEqual(prefix['equity_curve'], full['equity_curve'][:len(prefix['equity_curve'])])

    def test_insufficient_cash_and_fee_exceeds_sale(self):
        result = self.run_case(execution=replace(self.execution, capital='1.00'))
        self.assertEqual(result['fills'], [])
        self.assertTrue(any(e['kind'] == 'insufficient_budget' for e in result['events']))
        bars = list(self.ds.bars)
        bars[4] = replace(bars[4], open=F(1, 100), low=F(1, 100))
        result = self.run_case(replace(self.ds, bars=tuple(bars)),
                               replace(self.execution, minimum_commission='10.00'))
        self.assertTrue(any(e['kind'] == 'fee_exceeds_proceeds' for e in result['events']))
        self.assertTrue(all(F(p['cash']) >= 0 for p in result['equity_curve']))

    def test_split_holding_and_dividend_accounting(self):
        ds = dataset([100, 100, 120, 100, 50, 49, 49, 49])
        bars = list(ds.bars)
        bars[4] = replace(bars[4], split=F(2))
        bars[5] = replace(bars[5], dividend=F(1))
        result = self.run_case(replace(ds, bars=tuple(bars)))
        # Buy 20 @100 at day3, split to40 before day4 open, sell bearish signal at50.
        self.assertEqual(result['fills'][0]['quantity'], 20)
        self.assertEqual(result['fills'][1]['quantity'], 40)
        self.assertEqual(result['closed_trades'][0]['pnl'], '0.00')
        # Separate rising case keeps holding across dividend; no double counting.
        ds = dataset([100, 100, 120, 120, 60, 59, 59, 60])
        bars = list(ds.bars)
        bars[4] = replace(bars[4], split=F(2))
        bars[5] = replace(bars[5], dividend=F(1))
        result = self.run_case(replace(ds, bars=tuple(bars)))
        self.assertTrue(any(e['kind'] == 'dividend' and e['cash'] == '32.00' for e in result['events']))
        for p in result['equity_curve']:
            self.assertEqual(F(p['equity']), F(2000) + F(p['realized_pnl']) + F(p['unrealized_pnl']))

    def test_reverse_split_cash_in_lieu(self):
        ds = dataset([10, 10, 12, 10, 30, 30, 30, 30])
        bars = list(ds.bars)
        bars[4] = replace(bars[4], split=F(1, 3))
        result = self.run_case(replace(ds, bars=tuple(bars)))
        self.assertTrue(any(e['kind'] == 'cash_in_lieu' and e['cash'] == '20.00' for e in result['events']))
        self.assertEqual(result['fills'][0]['quantity'], 200)
        self.assertTrue(all(type(f['quantity']) is int for f in result['fills']))

    def test_shared_portfolio_and_deterministic_order(self):
        other = replace(self.ds, ticker='NVDA')
        result = run_backtest({'NVDA': other, 'MU': self.ds}, self.strategy, self.ds.bars[3].day,
                              self.ds.bars[-1].day, self.execution, calendar=CAL, as_of=NOW)
        self.assertEqual(result['fills'][0]['ticker'], 'MU')
        self.assertTrue(any(e['kind'] == 'insufficient_budget' and e['ticker'] == 'NVDA' for e in result['events']))
        for p in result['equity_curve']:
            self.assertGreaterEqual(F(p['cash']), 0)

    def test_repeatability_decimal_context_and_duplicate_signals(self):
        expected = self.run_case()
        with localcontext() as ctx:
            ctx.prec = 2
            ctx.rounding = ROUND_DOWN
            self.assertEqual(expected, self.run_case())
        original = signals(self.ds, self.strategy, as_of=NOW)
        with patch('dual_ma_ai.backtest.signals', return_value=original + original):
            result = self.run_case()
        self.assertEqual(result['fills'], expected['fills'])

    def test_missing_history_warmup_and_future_bar_rejected(self):
        for ds in (replace(self.ds, bars=self.ds.bars[:4] + self.ds.bars[5:]),
                   replace(self.ds, bars=self.ds.bars[1:])):
            with self.assertRaises((DataError, HistoryUnavailable)):
                run_backtest({'MU': ds}, self.strategy, self.ds.bars[3].day, self.ds.bars[-1].day,
                             self.execution, calendar=CAL, as_of=NOW)
        with self.assertRaises(DataError):
            self.run_case(execution=replace(self.execution, warmup=1))
        with self.assertRaises(HistoryUnavailable):
            run_backtest({'MU': self.ds}, self.strategy, self.ds.bars[3].day, self.ds.bars[-1].day,
                         self.execution, calendar=CAL, as_of=self.ds.bars[3].opened_at)

    def test_all_horizons_coverage_and_identical_assumptions(self):
        ds = dataset([10] * 1200, start=date(2020, 1, 1))
        end = ds.bars[-1].day
        result = compare({'MU': ds}, end, Execution(), calendar=CAL, as_of=NOW)
        self.assertEqual([r['status'] for r in result['comparisons']], ['available', 'available', 'unavailable', 'unavailable'])
        runs = result['comparisons'][0]['runs']
        self.assertEqual([r['config']['strategy']['kind'] for r in runs], ['SMA', 'SMA', 'EMA'])
        self.assertEqual(len({json.dumps(r['config']['execution'], sort_keys=True) for r in runs}), 1)
        self.assertEqual(len({r['equity_curve'][0]['day'] for r in runs}), 1)
        self.assertEqual(years_before(date(2024, 2, 29), 1), date(2023, 2, 28))
        self.assertTrue(result['warnings'])

    def test_no_silent_universe_exclusion(self):
        long = dataset([10] * 1200, start=date(2020, 1, 1))
        short = dataset([10] * 100, 'SPCX', start=long.bars[-100].day)
        result = compare({'MU': long, 'SPCX': short}, long.bars[-1].day, Execution(), calendar=CAL, as_of=NOW)
        self.assertTrue(all(item['status'] == 'unavailable' for item in result['comparisons']))
        self.assertTrue(all('SPCX' in item['reason'] for item in result['comparisons'][:2]))

    def test_metrics_known_drawdown_sharpe_and_trade_counts(self):
        curve = [{'day': d, 'equity': e} for d, e in [('2020-01-01', '1000.00'), ('2020-05-01', '1200.00'),
                                                     ('2020-09-01', '900.00'), ('2021-01-01', '1100.00')]]
        result = metrics(curve, [{'pnl': '100.00'}, {'pnl': '-50.00'}], 4)
        self.assertEqual(result['max_drawdown'], '0.25000000')
        self.assertEqual(result['win_rate'], '0.50000000')
        self.assertEqual(result['profit_factor'], '2.00000000')
        self.assertEqual(result['number_of_trades'], 2)
        self.assertEqual(result['number_of_fills'], 4)
        self.assertEqual(result['total_return'], '0.10000000')
        self.assertAlmostEqual(float(result['cagr']), 1.1 ** (365.2425 / 366) - 1, places=8)
        known_returns = [0.2, -0.25, 2 / 9]
        expected_sharpe = statistics.mean(known_returns) / statistics.stdev(known_returns) * math.sqrt(252)
        self.assertAlmostEqual(float(result['sharpe']), expected_sharpe, places=8)
        flat = metrics([{'day': '2024-01-01', 'equity': '2000.00'},
                        {'day': '2024-01-02', 'equity': '2000.00'}], [], 0)
        self.assertIsNone(flat['sharpe'])
        self.assertIsNone(flat['win_rate'])
        self.assertIsNone(flat['profit_factor'])

    def test_wallet_is_never_opened_or_modified(self):
        actual = Path(__file__).resolve().parent.parent / 'data/dual_ma_ai/wallet.sqlite3'
        before = actual.read_bytes() if actual.exists() else None
        with patch('dual_ma_ai.wallet.Wallet', side_effect=AssertionError('Forbidden wallet access')):
            self.run_case()
        after = actual.read_bytes() if actual.exists() else None
        self.assertEqual(before, after)

    def test_bad_execution_boundaries(self):
        for kwargs in ({'capital': '-1'}, {'slippage_bps': 10000}, {'commission_bps': -1},
                       {'allocation_bps': 0}, {'allocation_bps': 10001}, {'warmup': True}, {'minimum_commission': '-1'}):
            with self.assertRaises(ValueError):
                Execution(**kwargs)
        with self.assertRaises(DataError):
            Execution(slippage_bps=9999).price(F(1, 100), 'SELL')


class ResearchCLITests(unittest.TestCase):
    def test_demo_report_csv_and_wallet_isolation(self):
        from dual_ma_ai import research
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch.object(research, 'REPORT_ROOT', Path(directory)), \
                 patch.object(research, 'USCalendar', return_value=CAL), \
                 patch('sys.argv', ['research', 'compare', '--demo', '--tickers', 'MU', '--years', '1']), \
                 patch('dual_ma_ai.wallet.Wallet', side_effect=AssertionError('Forbidden wallet access')), \
                 redirect_stdout(output):
                research.main()
            summary = json.loads(output.getvalue())
            self.assertTrue(summary['synthetic'])
            report = json.loads(Path(summary['report']).read_text())
            self.assertTrue(report['synthetic'])
            self.assertEqual(len(summary['results']), 3)
            self.assertEqual(len(list(Path(directory).glob('*-equity.csv'))), 3)
            self.assertTrue(all('SYNTHETIC' in run['provenance']['MU']['source']
                                for run in report['comparisons'][0]['runs']))

    def test_reject_incomplete_custom_strategy(self):
        from dual_ma_ai import research
        with patch('sys.argv', ['research', 'compare', '--demo', '--kind', 'EMA']), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                research.main()
        self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
