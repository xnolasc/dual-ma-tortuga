"""Deterministic paper wallet. No network, strategy execution, or automation."""
import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

TICKERS = ('SPCX', 'LITE', 'MRVL', 'TSM', 'CIEN', 'COHR', 'MU', 'NVDA', 'AAOI')
MAX_CENTS = 10**14  # USD 1 trillion; keeps products within SQLite integer range.


class WalletError(ValueError):
    """A rejected request; no financial state was changed."""


def cents(value, *, zero=False):
    """Accept decimal strings, Decimal or integer USD; never binary floats."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise WalletError('Use a decimal string, Decimal, or integer USD amount')
    try:
        with localcontext() as ctx:
            ctx.prec = 40
            amount = Decimal(value)
            if not amount.is_finite() or amount < 0 or amount > MAX_CENTS // 100:
                raise WalletError('Amount is outside allowed range')
            scaled = amount * 100
            if scaled != scaled.to_integral_value() or (scaled == 0 and not zero):
                raise WalletError('Amount must be positive with at most two decimal places')
            return int(scaled)
    except (InvalidOperation, OverflowError) as exc:
        raise WalletError('Invalid amount') from exc


def usd(value):
    sign = '-' if value < 0 else ''
    value = abs(value)
    return f'{sign}{value // 100}.{value % 100:02d}'


class Wallet:
    """One wallet per database. Each operation opens/closes its own connection."""

    def __init__(self, path):
        self.path = str(Path(path))
        if self.path == ':memory:':
            raise WalletError('A persistent database file is required')
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version not in (0, 1):
                    raise WalletError(f'Unsupported schema version: {version}')
                if version == 0 and db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone():
                    raise WalletError('Refusing to initialize a nonempty, unversioned database')
                # executescript implicitly commits: execute complete statements instead
                # so schema, version and initial contribution share one atomic lock.
                statement = ''
                for line in Path(__file__).with_name('schema.sql').read_text().splitlines(True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        db.execute(statement)
                        statement = ''
                if db.execute('SELECT id FROM wallet').fetchone() is None:
                    if version != 0:
                        raise WalletError('Existing wallet state is missing; refusing to reset')
                    db.execute('INSERT INTO wallet VALUES (1, 200000, 200000, 0)')
                    self._record(db, '__initial__', 'initial', {'amount': 200000})
                self._check(db)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @classmethod
    def open_existing(cls, path):
        """Attach without initializing or changing existing schema/header metadata."""
        path = Path(path).resolve()
        if not path.is_file():
            raise WalletError('Existing wallet required')
        wallet = cls.__new__(cls)
        wallet.path = str(path)
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise WalletError('Existing schema version 1 required')
            wallet._check(db)
            db.execute('SELECT * FROM events LIMIT 1').fetchone()
        return wallet

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('PRAGMA synchronous = FULL')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                self._check(db)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _check(self, db):
        w = db.execute('SELECT * FROM wallet WHERE id = 1').fetchone()
        cost = db.execute('SELECT COALESCE(SUM(cost),0) FROM positions').fetchone()[0]
        if w['cash'] + cost != w['contributions'] + w['realized']:
            raise WalletError('Accounting invariant violated')
        if any(abs(w[k]) > MAX_CENTS for k in ('cash', 'contributions', 'realized')) or cost > MAX_CENTS:
            raise WalletError('Wallet limit exceeded')

    def _record(self, db, key, kind, payload):
        w = db.execute('SELECT * FROM wallet WHERE id = 1').fetchone()
        cursor = db.execute(
            'INSERT INTO events (key,kind,payload,cash_after,contributions_after,realized_after) VALUES (?,?,?,?,?,?)',
            (key, kind, json.dumps(payload, sort_keys=True), w['cash'], w['contributions'], w['realized']))
        return dict(db.execute('SELECT * FROM events WHERE id=?', (cursor.lastrowid,)).fetchone())

    def _apply(self, kind, payload, key, update):
        if not isinstance(key, str) or not key.strip() or len(key) > 200 or key.startswith('__'):
            raise WalletError('A nonempty idempotency key (up to 200 characters) is required')
        with self._transaction() as db:
            prior = db.execute('SELECT * FROM events WHERE key=?', (key,)).fetchone()
            if prior:
                if prior['kind'] != kind or json.loads(prior['payload']) != payload:
                    raise WalletError('Idempotency key already used for a different request')
                return dict(prior)
            update(db)
            return self._record(db, key, kind, payload)

    def deposit(self, amount, key):
        value = cents(amount)
        return self._apply('deposit', {'amount': value}, key, lambda db: db.execute(
            'UPDATE wallet SET cash=cash+?, contributions=contributions+? WHERE id=1', (value, value)))

    def withdraw(self, amount, key):
        value = cents(amount)
        def update(db):
            cash = db.execute('SELECT cash FROM wallet').fetchone()[0]
            if value > cash:
                raise WalletError('Insufficient cash')
            db.execute('UPDATE wallet SET cash=cash-?, contributions=contributions-? WHERE id=1', (value, value))
        return self._apply('withdraw', {'amount': value}, key, update)

    @staticmethod
    def _trade(ticker, quantity, price, fee):
        if ticker not in TICKERS:
            raise WalletError('Unsupported ticker')
        if type(quantity) is not int or not 0 < quantity <= 10**9:
            raise WalletError('Quantity must be a positive whole number of shares')
        price, fee = cents(price), cents(fee, zero=True)
        if quantity * price + fee > MAX_CENTS:
            raise WalletError('Trade limit exceeded')
        return {'ticker': ticker, 'quantity': quantity, 'price': price, 'fee': fee}

    def buy(self, ticker, quantity, price, key, fee='0.00'):
        p = self._trade(ticker, quantity, price, fee)
        def update(db):
            cost = quantity * p['price'] + p['fee']
            if cost > db.execute('SELECT cash FROM wallet').fetchone()[0]:
                raise WalletError('Insufficient cash')
            db.execute('UPDATE wallet SET cash=cash-? WHERE id=1', (cost,))
            db.execute('INSERT INTO positions VALUES (?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET '
                       'quantity=quantity+excluded.quantity,cost=cost+excluded.cost,mark=excluded.mark',
                       (ticker, quantity, cost, p['price']))
        return self._apply('buy', p, key, update)

    def sell(self, ticker, quantity, price, key, fee='0.00'):
        p = self._trade(ticker, quantity, price, fee)
        def update(db):
            pos = db.execute('SELECT * FROM positions WHERE ticker=?', (ticker,)).fetchone()
            if pos is None or quantity > pos['quantity']:
                raise WalletError('Insufficient shares; shorting is prohibited')
            proceeds = quantity * p['price'] - p['fee']
            if proceeds < 0:
                raise WalletError('Fee exceeds proceeds')
            # Weighted average cost, nearest cent half-up. Final sale consumes all residual cost.
            basis = (2 * pos['cost'] * quantity + pos['quantity']) // (2 * pos['quantity'])
            db.execute('UPDATE wallet SET cash=cash+?, realized=realized+? WHERE id=1',
                       (proceeds, proceeds - basis))
            if quantity == pos['quantity']:
                db.execute('DELETE FROM positions WHERE ticker=?', (ticker,))
            else:
                db.execute('UPDATE positions SET quantity=quantity-?,cost=cost-?,mark=? WHERE ticker=?',
                           (quantity, basis, p['price'], ticker))
        return self._apply('sell', p, key, update)

    def mark(self, ticker, price, key):
        if ticker not in TICKERS:
            raise WalletError('Unsupported ticker')
        value = cents(price)
        def update(db):
            cursor = db.execute('UPDATE positions SET mark=? WHERE ticker=?', (value, ticker))
            if cursor.rowcount != 1:
                raise WalletError('No open position')
        return self._apply('mark', {'ticker': ticker, 'price': value}, key, update)

    def snapshot(self):
        with self._connection() as db:
            db.execute('BEGIN')  # Cash and positions from the same consistent read snapshot.
            self._check(db)
            w = db.execute('SELECT * FROM wallet WHERE id=1').fetchone()
            positions = [dict(p) for p in db.execute('SELECT * FROM positions ORDER BY ticker')]
            market = sum(p['quantity'] * p['mark'] for p in positions)
            unrealized = market - sum(p['cost'] for p in positions)
            equity = w['cash'] + market
            return {'mode': 'PAPER', 'currency': 'USD', 'cash': usd(w['cash']),
                    'net_contributions': usd(w['contributions']), 'realized_pnl': usd(w['realized']),
                    'unrealized_pnl': usd(unrealized), 'trading_pnl': usd(w['realized'] + unrealized),
                    'market_value': usd(market), 'equity': usd(equity),
                    'positions': [{'ticker': p['ticker'], 'quantity': p['quantity'],
                                   'cost_basis': usd(p['cost']), 'mark': usd(p['mark'])} for p in positions]}

    def history(self):
        with self._connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM events ORDER BY id')]
