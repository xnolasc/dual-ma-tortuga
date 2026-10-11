import sqlite3
import subprocess
import sys
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

from dual_ma_ai.wallet import MAX_CENTS, TICKERS, Wallet, WalletError


class WalletTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'wallet.sqlite3'
        self.wallet = Wallet(self.path)

    def invariant(self):
        s = self.wallet.snapshot()
        self.assertEqual(Decimal(s['equity']), Decimal(s['net_contributions']) + Decimal(s['trading_pnl']))
        self.assertEqual(Decimal(s['trading_pnl']), Decimal(s['realized_pnl']) + Decimal(s['unrealized_pnl']))
        self.assertGreaterEqual(Decimal(s['cash']), 0)
        return s

    def test_initial_and_restart(self):
        self.assertEqual(self.wallet.snapshot()['cash'], '2000.00')
        self.assertEqual(self.wallet.snapshot()['trading_pnl'], '0.00')
        self.wallet.deposit('250.25', 'd')
        self.wallet.withdraw('10.00', 'w')
        self.wallet.buy('MU', 2, '100.00', 'b', fee='1.00')
        self.wallet.mark('MU', '110.00', 'm')
        self.wallet.sell('MU', 1, '115.00', 's')
        before = self.wallet.snapshot(), self.wallet.history()
        self.wallet = Wallet(self.path)
        self.assertEqual(before, (self.wallet.snapshot(), self.wallet.history()))
        self.assertEqual(sum(e['kind'] == 'initial' for e in self.wallet.history()), 1)
        self.invariant()

    def test_contributions_are_not_profit(self):
        self.wallet.deposit('500.00', 'd')
        self.wallet.withdraw('100.00', 'w')
        s = self.invariant()
        self.assertEqual(s['cash'], '2400.00')
        self.assertEqual(s['net_contributions'], '2400.00')
        self.assertEqual(s['trading_pnl'], '0.00')
        self.assertEqual([e['kind'] for e in self.wallet.history()], ['initial', 'deposit', 'withdraw'])

    def test_invalid_amounts(self):
        before = self.wallet.snapshot(), self.wallet.history()
        for value in ('0', '-1', '0.001', 'NaN', 'Infinity', '-Infinity', 'bad', '1e10000', '', 1.2, True, None):
            for operation in (self.wallet.deposit, self.wallet.withdraw):
                with self.subTest(value=value, operation=operation.__name__):
                    with self.assertRaises(WalletError):
                        operation(value, 'invalid')
        self.assertEqual(before, (self.wallet.snapshot(), self.wallet.history()))

    def test_full_withdrawal_and_overdraft(self):
        with self.assertRaises(WalletError):
            self.wallet.withdraw('2000.01', 'fail')
        self.wallet.withdraw('2000.00', 'all')
        with self.assertRaises(WalletError):
            self.wallet.withdraw('0.01', 'fail')
        self.assertEqual(self.invariant()['cash'], '0.00')
        self.assertEqual(len(self.wallet.history()), 2)

    def test_idempotency_and_conflicts(self):
        first = self.wallet.deposit('10', 'same')
        self.assertEqual(first, self.wallet.deposit(Decimal('10.00'), 'same'))
        self.assertEqual(first, Wallet(self.path).deposit('10.0', 'same'))
        for op, value in ((self.wallet.deposit, '11'), (self.wallet.withdraw, '10')):
            with self.assertRaises(WalletError):
                op(value, 'same')
        self.assertEqual(self.invariant()['cash'], '2010.00')
        self.assertEqual(len(self.wallet.history()), 2)

    def test_invalid_keys(self):
        for key in ('', ' ', '__initial__', 'x' * 201, None, 12):
            with self.assertRaises(WalletError):
                self.wallet.deposit('1', key)
        self.assertEqual(len(self.wallet.history()), 1)

    def test_realized_unrealized_and_fees(self):
        self.wallet.buy('NVDA', 10, '100', 'b', fee='1')
        s = self.invariant()
        self.assertEqual(s['cash'], '999.00')
        self.assertEqual(s['unrealized_pnl'], '-1.00')
        self.wallet.mark('NVDA', '120', 'm')
        self.assertEqual(self.invariant()['unrealized_pnl'], '199.00')
        self.wallet.sell('NVDA', 4, '120', 's1', fee='0.50')
        s = self.invariant()
        self.assertEqual(s['realized_pnl'], '79.10')
        self.assertEqual(s['unrealized_pnl'], '119.40')
        self.wallet.deposit('100', 'd')
        self.wallet.withdraw('100', 'w')
        self.assertEqual(self.invariant()['trading_pnl'], '198.50')
        self.wallet.sell('NVDA', 6, '90', 's2', fee='0.50')
        s = self.invariant()
        self.assertEqual(s['realized_pnl'], '18.00')
        self.assertEqual(s['unrealized_pnl'], '0.00')
        self.assertEqual(s['equity'], '2018.00')
        self.assertEqual(s['positions'], [])

    def test_partial_sale_rounding_and_average_cost(self):
        self.wallet.buy('MU', 1, '1', 'b1', fee='0.01')
        self.wallet.buy('MU', 2, '1', 'b2')
        self.wallet.sell('MU', 1, '1', 's1')
        self.assertEqual(self.invariant()['positions'][0]['cost_basis'], '2.01')
        self.wallet.sell('MU', 1, '1', 's2')
        self.assertEqual(self.invariant()['positions'][0]['cost_basis'], '1.00')
        self.wallet.sell('MU', 1, '1', 's3')
        self.assertEqual(self.invariant()['realized_pnl'], '-0.01')

    def test_trade_and_mark_rejections(self):
        before = self.wallet.snapshot(), self.wallet.history()
        calls = [lambda: self.wallet.buy('BAD', 1, '1', 'x'),
                 lambda: self.wallet.buy('MU', 0, '1', 'x'),
                 lambda: self.wallet.buy('MU', True, '1', 'x'),
                 lambda: self.wallet.buy('MU', 1.5, '1', 'x'),
                 lambda: self.wallet.buy('MU', 1, '0', 'x'),
                 lambda: self.wallet.buy('MU', 1, '1', 'x', fee='-1'),
                 lambda: self.wallet.buy('MU', 1, '2001', 'x'),
                 lambda: self.wallet.sell('MU', 1, '1', 'x'),
                 lambda: self.wallet.mark('MU', '1', 'x'),
                 lambda: self.wallet.mark('BAD', '1', 'x')]
        for call in calls:
            with self.assertRaises(WalletError):
                call()
        self.assertEqual(before, (self.wallet.snapshot(), self.wallet.history()))
        self.wallet.buy('MU', 1, '1', 'b')
        for quantity, fee in ((2, '0'), (1, '2')):
            with self.assertRaises(WalletError):
                self.wallet.sell('MU', quantity, '1', 's', fee=fee)
        self.invariant()

    def test_all_supported_tickers(self):
        for ticker in TICKERS:
            self.wallet.buy(ticker, 1, '1', 'buy-' + ticker)
        self.assertEqual(len(self.invariant()['positions']), 9)

    def test_all_mutations_idempotent(self):
        commands = [lambda: self.wallet.buy('TSM', 2, '10', 'b'),
                    lambda: self.wallet.mark('TSM', '12', 'm'),
                    lambda: self.wallet.sell('TSM', 1, '12', 's'),
                    lambda: self.wallet.withdraw('1', 'w')]
        for command in commands:
            first = command()
            before = self.wallet.snapshot(), self.wallet.history()
            self.assertEqual(command(), first)
            self.assertEqual(before, (self.wallet.snapshot(), self.wallet.history()))
        self.invariant()

    def test_atomic_rollback_on_audit_failure(self):
        before = self.wallet.snapshot(), self.wallet.history()
        with patch.object(self.wallet, '_record', side_effect=RuntimeError('injected audit failure')):
            with self.assertRaises(RuntimeError):
                self.wallet.buy('MU', 1, '100', 'b')
        self.assertEqual(before, (self.wallet.snapshot(), self.wallet.history()))
        self.wallet.buy('MU', 1, '100', 'b')  # Failed key was not consumed.
        self.invariant()

    def test_limits_and_decimal_context_independence(self):
        with self.assertRaises(WalletError):
            self.wallet.deposit(str(MAX_CENTS // 100), 'too-much')
        self.assertEqual(len(self.wallet.history()), 1)
        with localcontext() as ctx:
            ctx.prec = 2
            self.wallet.deposit(Decimal('123.45'), 'd')
        self.assertEqual(self.invariant()['cash'], '2123.45')

    def test_concurrent_duplicate_deposits(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            events = list(pool.map(lambda _: Wallet(self.path).deposit('10', 'd'), range(16)))
        self.assertEqual(len({e['id'] for e in events}), 1)
        self.assertEqual(self.invariant()['cash'], '2010.00')
        self.assertEqual(len(self.wallet.history()), 2)

    def test_concurrent_withdrawals_prevent_overdraft(self):
        def withdraw(i):
            try:
                Wallet(self.path).withdraw('1500', f'w{i}')
                return True
            except WalletError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(withdraw, range(2)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.invariant()['cash'], '500.00')

    def test_concurrent_initialization(self):
        path = Path(self.temp.name) / 'new.sqlite3'
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: Wallet(path), range(16)))
        self.assertEqual(Wallet(path).snapshot()['cash'], '2000.00')
        self.assertEqual(len(Wallet(path).history()), 1)

    def test_database_constraints_and_audit_immutability(self):
        with sqlite3.connect(self.path) as db:
            for statement in ('UPDATE wallet SET cash=-1', 'UPDATE wallet SET cash=1.5',
                              'DELETE FROM events', "UPDATE events SET kind='withdraw'"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement)
        self.invariant()

    def test_reject_future_schema(self):
        with sqlite3.connect(self.path) as db:
            db.execute('PRAGMA user_version=2')
        with self.assertRaises(WalletError):
            Wallet(self.path)

    def test_reject_unrelated_database(self):
        path = Path(self.temp.name) / 'unrelated.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE unrelated (id INTEGER)')
        with self.assertRaises(WalletError):
            Wallet(path)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                             [('unrelated',)])

    def test_initialization_rolls_back_schema_and_funding(self):
        path = Path(self.temp.name) / 'failed-init.sqlite3'
        with patch.object(Wallet, '_record', side_effect=RuntimeError('injected failure')):
            with self.assertRaises(RuntimeError):
                Wallet(path)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
        self.assertEqual(Wallet(path).snapshot()['cash'], '2000.00')

    def test_missing_wallet_is_not_silently_reset(self):
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM wallet')
        with self.assertRaises(WalletError):
            Wallet(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wallet').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)

    def test_cli_across_processes(self):
        def run(*args):
            return subprocess.run([sys.executable, '-m', 'dual_ma_ai', '--db', str(self.path), *args],
                                  capture_output=True, text=True)
        result = run('deposit', '25.50', '--key', 'cli')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(run('deposit', '25.50', '--key', 'cli').returncode, 0)
        result = run('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['cash'], '2025.50')
        result = run('withdraw', '3000', '--key', 'rejected')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Insufficient cash', result.stderr)
        self.assertEqual(len(self.wallet.history()), 2)


if __name__ == '__main__':
    unittest.main()
