# Run and inspect the Phase 2 PAPER wallet

From this repository on `feature/dual-ma-ai-v1`, use Python 3.11+.
No pip installation is needed. These commands do not start a trader or server.
The first invocation on a new database creates USD 2,000 of virtual capital;
reopening never resets it. Default path is relative to your working directory.
Use a dedicated database file; do not use a legacy or Turtle database.

```bash
python3 -m dual_ma_ai init
python3 -m dual_ma_ai status
python3 -m dual_ma_ai deposit 250.00 --key deposit-example-001
python3 -m dual_ma_ai withdraw 50.00 --key withdrawal-example-001
python3 -m dual_ma_ai history
```

Retry the identical deposit with the same key to verify no additional credit.
Use a new key for each new intended action. A reused key with a different
amount/operation is rejected. Amounts are USD strings with at most two decimal
places. Errors return CLI exit code 2 for rejected wallet requests.

Manual paper accounting examples, with whole shares only:

```bash
python3 -m dual_ma_ai buy NVDA 2 100.00 --fee 1.00 --key paper-buy-001
python3 -m dual_ma_ai mark NVDA 110.00 --key paper-mark-001
python3 -m dual_ma_ai status
python3 -m dual_ma_ai sell NVDA 1 115.00 --fee 0.50 --key paper-sell-001
python3 -m dual_ma_ai status
```

These are synthetic user-supplied fills, not market orders or current-price
claims. Equity uses the last supplied mark. Fees are included in P&L.

For an isolated experiment supply a path **before** the subcommand:

```bash
python3 -m dual_ma_ai --db /tmp/dual-ma-ai-example.sqlite3 init
python3 -m dual_ma_ai --db /tmp/dual-ma-ai-example.sqlite3 status
```

The local Python API accepts a database path and exact amounts:

```python
from dual_ma_ai import Wallet
wallet = Wallet('data/dual_ma_ai/wallet.sqlite3')
wallet.deposit('25.00', key='python-deposit-001')
print(wallet.snapshot())
print(wallet.history())
```

Inspect SQLite read-only, if the sqlite3 CLI is installed:

```bash
sqlite3 -readonly data/dual_ma_ai/wallet.sqlite3 '.schema'
sqlite3 -readonly data/dual_ma_ai/wallet.sqlite3 'SELECT * FROM wallet;'
sqlite3 -readonly data/dual_ma_ai/wallet.sqlite3 'SELECT * FROM positions;'
sqlite3 -readonly data/dual_ma_ai/wallet.sqlite3 'SELECT * FROM events ORDER BY id;'
sqlite3 -readonly data/dual_ma_ai/wallet.sqlite3 'PRAGMA integrity_check;'
```

SQL money values and event payload money fields are integer **cents**. CLI status
money values are USD decimal strings. Keep database inspection read-only; writes
must go through Wallet to enforce accounting invariants. Restrict the data folder
with `chmod 700 data/dual_ma_ai` after creation if other local accounts exist.

Run tests from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

Tests use disposable temporary databases, no network, no market feed and no
production wallet. They cover contributions, overdrafts, invalid requests,
fees/partial sale rounding, invariants, all supported tickers, atomic rollback,
audit immutability, concurrent requests, process restart and the CLI.

Files added: `dual_ma_ai/__init__.py`, `dual_ma_ai/__main__.py`,
`dual_ma_ai/wallet.py`, `dual_ma_ai/schema.sql`, `tests/test_wallet.py`, and the
architecture, wallet instructions, test-results document and raw test logs under
`docs/`. `.gitignore` adds the new runtime directory. Existing project code and
README are unchanged. Future work is listed in the architecture document;
HTTP API, strategy, backtesting, dashboard, agent tools and automation are pending.
