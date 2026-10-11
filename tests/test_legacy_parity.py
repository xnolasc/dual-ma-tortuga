"""Parity against executable, hash-pinned main source, with all external I/O blocked."""
import copy
import hashlib
import json
import random
import subprocess
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from dual_ma_ai import legacy
from dual_ma_ai.backtest import Execution, run_backtest
from dual_ma_ai.market_data import DataError
from dual_ma_ai.strategy import LEGACY, PRESETS, Strategy, signals
from test_research import CAL, NOW, dataset

ROOT = Path(__file__).resolve().parent.parent


def read_reference(name):
    result = subprocess.run(['git', 'show', f'{legacy.REFERENCE_COMMIT}:{name}'], cwd=ROOT,
                            check=True, capture_output=True)
    source = result.stdout
    if hashlib.sha256(source).hexdigest() != legacy.REFERENCE_SHA256[name]:
        raise AssertionError('Reference source changed: a new audit is required')
    return source.decode()


def reference_modules():
    common, ledger, trader = (types.ModuleType('audit_reference_' + name) for name in ('common', 'ledger', 'trader'))
    for module in (common, ledger, trader):
        module.__file__ = str(ROOT / 'tests' / 'reference-not-on-disk.py')
    blocked_requests = types.ModuleType('requests')
    def forbidden(*args, **kwargs):
        raise AssertionError('External I/O is forbidden in parity tests')
    blocked_requests.get = forbidden
    exec(compile(read_reference('dualma_common.py'), 'main:dualma_common.py', 'exec'), common.__dict__)
    with patch.dict('sys.modules', {'dualma_common': common, 'dualma_ledger': ledger, 'requests': blocked_requests}):
        exec(compile(read_reference('dualma_ledger.py'), 'main:dualma_ledger.py', 'exec'), ledger.__dict__)
        exec(compile(read_reference('dualma_trader.py'), 'main:dualma_trader.py', 'exec'), trader.__dict__)
    ledger.load_ledger = ledger.save_ledger = forbidden
    trader.load_ledger = trader.save_ledger = forbidden
    return common, ledger, trader


def klines(closes, spread=1.0):
    return [{'high': float(value) + spread, 'low': float(value) - spread, 'close': float(value)} for value in closes]


class LegacyParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.common, cls.original, cls.trader = reference_modules()

    def setUp(self):
        self.now = 1700000000
        self.clock = lambda: self.now
        self.original.time = types.SimpleNamespace(time=self.clock)
        self.messages = []
        self.trader.log = self.messages.append

    def oracle_process(self, ledger, ticker, mode, rows):
        def get(symbol, limit):
            self.assertEqual(symbol, self.common.symbol_for(ticker))
            self.assertEqual(limit, 45)
            return copy.deepcopy(rows)
        self.trader.get_daily_klines = get
        self.trader.procesar_ticker(ledger, ticker, mode)

    def both(self, rows, mode='porcentaje', ticker='MU', initial=None):
        before = initial if initial is not None else self.original.empty_ledger(1000.0)
        expected, actual = copy.deepcopy(before), copy.deepcopy(before)
        messages = []
        self.oracle_process(expected, ticker, mode, rows)
        legacy.process_ticker(actual, ticker, mode, copy.deepcopy(rows), clock=self.clock, log=messages.append)
        self.assertEqual(actual, expected)
        self.assertEqual(messages, self.messages)
        self.messages.clear()
        return actual

    def test_reference_main_commit_and_all_five_sources(self):
        for name in legacy.REFERENCE_SHA256:
            self.assertTrue(read_reference(name))
        current_main = subprocess.run(['git', 'rev-parse', 'main'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(current_main, legacy.REFERENCE_COMMIT, 'main moved: document a new audit before updating the oracle')

    def test_named_config_and_presets_separate(self):
        self.assertEqual(LEGACY.name, 'LEGACY')
        self.assertEqual(LEGACY.tickers, tuple(self.common.TICKERS))
        self.assertEqual(LEGACY.short, self.common.PROMEDIO_CORTO_DIAS)
        self.assertEqual(LEGACY.long, self.common.PROMEDIO_LARGO_DIAS)
        self.assertEqual(LEGACY.initial_capital, self.common.CAPITAL_INICIAL_USD)
        self.assertEqual(LEGACY.commission_pct, self.common.COMMISSION_PCT)
        self.assertEqual(LEGACY.minimum_notional, self.common.MIN_NOTIONAL_USD)
        self.assertEqual(LEGACY.fixed_max_pct, self.common.PORCENTAJE_FIJO_MAX)
        self.assertEqual([p.name for p in PRESETS], ['SMA_50_200', 'SMA_20_50', 'EMA_20_50'])
        with self.assertRaises(DataError):
            signals(dataset([10] * 45), LEGACY, as_of=NOW)

    def test_symbol_mapping(self):
        for ticker in ('MU', 'SNDK', 'WDC', 'BE', 'NVDA', 'SPCX', 'BTC'):
            self.assertEqual(legacy.symbol_for(ticker), self.common.symbol_for(ticker))

    def test_sma_exact_float_parity_and_short_history(self):
        for closes in ([], [10.0] * 9, [0.1] * 45, [1e16, 1.0, -1e16] * 15, [float(i) for i in range(45)]):
            for period in (10, 30):
                self.assertEqual(legacy.compute_sma(closes, period), self.trader.compute_sma(closes, period))

    def test_atr_simple_last_fourteen_ranges_including_gaps(self):
        for rows in ([], klines([10.0] * 14), klines([10.0] * 45), klines([10.0] * 44 + [50.0]), klines(list(range(1, 46)))):
            self.assertEqual(legacy.compute_atr(rows), self.trader.compute_atr(rows))
        self.assertEqual(legacy.compute_atr(klines([10.0] * 45)), 2.0)
        self.assertEqual(legacy.compute_atr(klines([10.0] * 44 + [50.0])), (13 * 2 + 41) / 14)

    def test_all_sizing_modes_minimum_and_initial_capital_anchor(self):
        for cash in (0.0, 9.99, 10.0, 10.009, 10.01, 149.0, 1000.0, 2000.0):
            ledger = legacy.empty_ledger()
            ledger['capital_disponible'] = cash
            for price in (0.01, 10.0, 123.45, 10000.0):
                for atr in (0.0, 0.01, 2.0, 100.0):
                    for mode in LEGACY.sizing_modes:
                        self.assertEqual(legacy.calculate_shares(mode, ledger, price, atr),
                                         self.trader.calcular_shares(mode, ledger, price, atr))
        ledger = legacy.empty_ledger()
        ledger['capital_disponible'] = 2000.0
        self.assertEqual(legacy.sizing_porcentaje(ledger, 10.0), 15.0)
        self.assertEqual(legacy.sizing_atr(ledger, 10.0, 2.0), 2.5)
        self.assertEqual(legacy.sizing_todo(legacy.empty_ledger(10.0), 10.0), 0.0)

    def test_bullish_entry_and_bearish_exit_for_all_modes(self):
        for mode in LEGACY.sizing_modes:
            ledger = self.both(klines([10.0] * 44 + [12.0]), mode)
            self.assertIn('MU', ledger['posiciones'])
            self.assertEqual(ledger['posiciones']['MU']['entry_price'], 12.0)
            self.assertEqual(ledger['historial_eventos'][-1]['reason'], 'cruce_arriba')
            closed = self.both(klines([12.0] * 44 + [8.0]), mode, initial=ledger)
            self.assertEqual(closed['posiciones'], {})
            self.assertEqual(closed['historial_eventos'][-1]['reason'], 'cruce_abajo')

    def test_equality_flat_trend_position_gate_and_repeated_cycle(self):
        self.assertEqual(self.both(klines([10.0] * 45))['historial_eventos'], [])
        ledger = self.both(klines([10.0] * 44 + [12.0]))
        repeated = self.both(klines([10.0] * 44 + [12.0]), initial=ledger)
        self.assertEqual(repeated, ledger)
        self.assertEqual(self.both(klines([12.0] * 44 + [8.0]))['historial_eventos'], [])
        self.assertEqual(self.both(klines(list(range(1, 46))))['historial_eventos'], [])

    def test_insufficient_missing_and_zero_atr(self):
        for rows in (None, [], klines([10.0] * 30)):
            self.assertEqual(self.both(rows)['historial_eventos'], [])
        ledger = legacy.empty_ledger(9.0)
        for mode in LEGACY.sizing_modes:
            self.assertEqual(self.both(klines([10.0] * 44 + [12.0]), mode, initial=ledger)['historial_eventos'], [])
        self.assertEqual(legacy.sizing_atr(legacy.empty_ledger(), 10.0, 0), 0.0)

    def test_binary_rounding_penny_overdraft_and_direct_overwrite(self):
        expected, actual = self.original.empty_ledger(100.0), legacy.empty_ledger(100.0)
        price = 100.009 / 1.001
        self.assertEqual(self.original.comprar(expected, 'MU', 1.0, price),
                         legacy.buy(actual, 'MU', 1.0, price, clock=self.clock))
        self.assertEqual(expected, actual)
        self.assertEqual(actual['capital_disponible'], -0.01)
        for book in (expected, actual):
            book['capital_disponible'] = 100.0
        self.original.comprar(expected, 'MU', 0.5, 10.0)
        legacy.buy(actual, 'MU', 0.5, 10.0, clock=self.clock)
        self.assertEqual(expected, actual)
        self.assertEqual(actual['posiciones']['MU']['shares'], 0.5)
        self.assertEqual(round(2.675, 2), 2.67)  # Original float rounding is not decimal half-up.
        expected, actual = self.original.empty_ledger(1000.0), legacy.empty_ledger()
        self.original.comprar(expected, 'MU', 1.0, 10.005 / 1.001)
        legacy.buy(actual, 'MU', 1.0, 10.005 / 1.001, clock=self.clock)
        self.assertEqual(actual, expected)
        self.assertEqual(actual['capital_disponible'], 990.0)
        self.assertEqual(actual['posiciones']['MU']['costo_total'], 10.01)
        self.assertEqual(actual['capital_disponible'] + actual['posiciones']['MU']['costo_total'], 1000.01)

    def test_low_level_sell_and_rejected_buy_unchanged(self):
        expected, actual = self.original.empty_ledger(1000.0), legacy.empty_ledger()
        self.assertIsNone(legacy.sell(actual, 'MU', 10.0, clock=self.clock))
        self.assertIsNone(self.original.vender(expected, 'MU', 10.0))
        self.assertFalse(legacy.buy(actual, 'MU', 1000.0, 10.0, clock=self.clock))
        self.assertFalse(self.original.comprar(expected, 'MU', 1000.0, 10.0))
        self.assertEqual(actual, expected)
        for book, operation in ((expected, self.original.comprar), (actual, lambda *args: legacy.buy(*args, clock=self.clock))):
            operation(book, 'MU', 1.2345, 12.3456)
        self.assertEqual(legacy.sell(actual, 'MU', 13.9876, clock=self.clock), self.original.vender(expected, 'MU', 13.9876))
        self.assertEqual(actual, expected)

    def test_timestamp_calls_preserved(self):
        expected, actual = legacy.empty_ledger(), legacy.empty_ledger()
        times = iter((1700000000, 1700000001))
        self.original.time = types.SimpleNamespace(time=lambda: next(times))
        self.original.comprar(expected, 'MU', 1.0, 10.0)
        times2 = iter((1700000000, 1700000001))
        legacy.buy(actual, 'MU', 1.0, 10.0, clock=lambda: next(times2))
        self.assertEqual(actual, expected)

    def test_wipeout_exact_boundaries(self):
        for cash in (-0.01, 0, 9.99, 10.0, 10.01):
            book = legacy.empty_ledger(cash)
            self.assertEqual(legacy.wipeout_total(book), self.original.wipeout_total(book))
            book['posiciones']['MU'] = {'shares': 0.01}
            self.assertEqual(legacy.wipeout_total(book), self.original.wipeout_total(book))
            self.assertFalse(legacy.wipeout_total(book))

    def test_seeded_random_identical_polling_stream_full_ledgers(self):
        rng = random.Random(31030)
        original = {mode: self.original.empty_ledger(1000.0) for mode in LEGACY.sizing_modes}
        actual = {mode: legacy.empty_ledger() for mode in LEGACY.sizing_modes}
        history = {ticker: [10.0] * 45 for ticker in LEGACY.tickers}
        for cycle in range(80):
            self.now += 60
            for ticker in LEGACY.tickers:
                history[ticker] = history[ticker][1:] + [rng.randrange(800, 1601) / 100]
            for mode in LEGACY.sizing_modes:
                for ticker in LEGACY.tickers:
                    rows = klines(history[ticker])
                    self.oracle_process(original[mode], ticker, mode, rows)
                    legacy.process_ticker(actual[mode], ticker, mode, rows, clock=self.clock)
                self.assertEqual(actual[mode], original[mode], f'{mode}, cycle {cycle}')
        self.assertTrue(all(book['historial_eventos'] for book in actual.values()))

    def test_unfinished_bar_and_same_close_are_preserved(self):
        rows = klines([10.0] * 44 + [12.0])
        rows[-1]['complete'] = False
        book = self.both(rows)
        self.assertEqual(book['posiciones']['MU']['entry_price'], 12.0)
        ds = dataset([10] * 44 + [12])
        modern = signals(ds, Strategy('SMA', 10, 30), as_of=ds.bars[-1].opened_at)
        self.assertEqual(modern, ())

    def test_same_candle_whipsaw_can_reenter_without_idempotency_key(self):
        book = None
        for last in (12.0, 8.0, 12.0):
            rows = klines([10.0] * 44 + [last])
            rows[-1]['candle_id'] = 'same-unfinished-candle'
            rows[-1]['complete'] = False
            book = self.both(rows, initial=book)
        self.assertEqual([event['evento'] for event in book['historial_eventos']], ['compra', 'venta', 'compra'])

    def test_numeric_boundary_difference_is_explicit(self):
        # Identical decimal provider inputs collapse to 0.1 in original float conversion.
        prices = ['0.1'] * 44 + ['0.10000000000000001']
        rows = legacy.normalize_klines([{'high': p, 'low': p, 'close': p} for p in prices])
        book = self.both(rows)
        self.assertEqual(book['historial_eventos'], [])
        modern = signals(dataset(prices), Strategy('SMA', 10, 30), as_of=NOW)
        self.assertEqual([signal.side for signal in modern], ['BUY'])

    def test_modern_same_ma_has_matching_normal_signal_but_different_fill(self):
        original_book = self.both(klines([10.0] * 44 + [12.0]))
        ds = dataset([10] * 44 + [12, 15])
        strategy = Strategy('SMA', 10, 30)
        modern_signals = signals(ds, strategy, as_of=NOW)
        self.assertEqual([(s.day, s.side) for s in modern_signals], [(ds.bars[44].day, 'BUY')])
        result = run_backtest({'MU': ds}, strategy, ds.bars[43].day, ds.bars[45].day,
                              Execution(capital='1000.00', commission_bps=10, slippage_bps=0,
                                        allocation_bps=1500, warmup=30), calendar=CAL, as_of=NOW)
        self.assertEqual(original_book['posiciones']['MU']['entry_price'], 12.0)
        self.assertEqual(original_book['posiciones']['MU']['shares'], 12.5)
        self.assertEqual(result['fills'][0]['fill_day'], ds.bars[45].day.isoformat())
        self.assertEqual(result['fills'][0]['price'], '15.00')
        self.assertEqual(result['fills'][0]['quantity'], 9)

    def test_fixture_replay_all_modes_against_original(self):
        document = json.loads((ROOT / 'tests/fixtures/legacy_snapshots.json').read_text())
        actual = legacy.replay(document['cycles'])
        expected = {mode: self.original.empty_ledger(1000.0) for mode in LEGACY.sizing_modes}
        for cycle in document['cycles']:
            self.now = cycle['timestamp']
            for mode in LEGACY.sizing_modes:
                for ticker in LEGACY.tickers:
                    rows = legacy.normalize_klines(cycle['klines'].get(mode, {}).get(ticker))
                    self.oracle_process(expected[mode], ticker, mode, rows)
        self.assertEqual(actual['ledgers'], expected)
        self.assertTrue(actual['warnings'])
        self.assertEqual(legacy.replay(document['cycles']), actual)

    def test_legacy_snapshot_modes_and_universe_not_silently_changed(self):
        for rows in ({'bad': {}}, {'todo': {'NVDA': []}}):
            with self.assertRaises(ValueError):
                legacy.replay([{'timestamp': 0, 'klines': rows}])

    def test_reference_files_and_persistent_wallet_untouched(self):
        watched = [ROOT / name for name in legacy.REFERENCE_SHA256] + [ROOT / 'data/dual_ma_ai/wallet.sqlite3']
        before = {str(path): path.read_bytes() for path in watched if path.exists()}
        document = json.loads((ROOT / 'tests/fixtures/legacy_snapshots.json').read_text())
        with patch('dual_ma_ai.wallet.Wallet', side_effect=AssertionError('Wallet forbidden')), \
             patch('sqlite3.connect', side_effect=AssertionError('SQLite forbidden')):
            legacy.replay(document['cycles'])
        after = {str(path): path.read_bytes() for path in watched if path.exists()}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
