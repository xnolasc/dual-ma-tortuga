# Dual MA AI architecture — Phase 1 and Phase 2

This document records the original Phase 1/2 baseline. The subsequently authorized
Phase 3 implementation and current boundaries are documented in
[dual-ma-ai-phase3.md](dual-ma-ai-phase3.md). Its strategy/backtesting scope
supersedes the pending items and phase numbering below; the wallet is unchanged.

Status: architecture and persistent wallet implemented; strategy, HTTP API,
backtesting, dashboard, agent integration and automation are planned only.
PAPER ONLY. Review is required before proceeding beyond Phase 2.

## Repository inspection and isolation

Inspected the existing README, common configuration, JSON ledger, trader,
dashboard, comparison report, round-reset utility, patch utilities and ignore
rules. The repository is a legacy three-ledger paper experiment with USD 1,000
per ledger, four tickers, float accounting, and public Binance requests. Its
standalone dashboard binds all interfaces. No test framework or package metadata
was present. The working tree was clean on `feature/dual-ma-ai-v1` before work.
No AGENTS.md was found in the repository or parent search.

The new package `dual_ma_ai` imports no legacy modules and does not read or write
legacy ledgers, logs, configuration, schedules or sibling projects. Turtle Mini
is outside this system's dependency graph. Existing scripts remain unchanged and
were not executed. No migration of legacy balances is performed. A separate
SQLite file defaults to `data/dual_ma_ai/wallet.sqlite3`; never point it at another
project's database. Runtime data is excluded from Git.

## Components and data flow

```text
Public data adapter -> validated immutable bars -> deterministic SMA strategy
                                                   |
                                               signal intents
                                                   |
                                            paper risk/execution
                                                   |
                                            SQLite wallet service
                                                   |
Local HTTP API <-> dashboard / scoped agent tools   |
                                                   |
Backtest runner -> same strategy + execution logic, separate run database
Automation -> explicitly enabled scheduler -> one idempotent paper cycle
```

| Component | Responsibility | Status / dependencies |
|---|---|---|
| Wallet service | Contributions, paper fills, marks, audit and equity | Implemented; Python standard library, SQLite |
| CLI | Explicit local calls to wallet service | Implemented; argparse and JSON |
| Data adapter | Equity OHLCV, exchange calendars, timestamps, provenance and validation | Future; public read-only source to be selected and verified |
| Strategy | Daily SMA 10/30 crossovers, versioned parameters and deterministic signals | Designed, not implemented |
| Paper execution | Risk limits, sizing, fill model, fees and intent deduplication | Future; no broker adapter |
| Backtester | Historical replay, execution parity, equity curve and contribution-adjusted metrics | Future; separate database per run |
| Local HTTP API | Versioned wallet/query and later paper execution endpoints | Designed below; future Python ASGI service |
| Dashboard | Cash, contributions, equity, realized/unrealized P&L, data freshness and audit | Future; local API only |
| Agent tools | Read state/explain signals; bounded paper requests | Future; structured schemas, no direct SQL or arbitrary commands |
| Automation | Restartable cycles, heartbeat, retries, kill switch and run audit | Future; disabled by default and not activated |

Supported universe: **SPCX, LITE, MRVL, TSM, CIEN, COHR, MU, NVDA, AAOI**.
These are explicit ticker identifiers, not presumed Binance token symbols.
Instrument identity, exchange, trading calendar and provider coverage must be
validated before using data. No provider has been connected in these phases.

## Strategy and execution design

Use completed daily equity bars in chronological exchange-session order.
Default short/long SMA periods are 10 and 30; require `0 < short < long` and at
least long+1 valid closes for two comparable windows. Enter when prior short <=
long and current short > long; exit when prior short >= long and current short <
long. Flat/equal averages do not repeatedly create signals. Reject duplicates,
missing sessions, nonpositive prices, invalid timestamps and insufficient history.
An unclosed current bar must never produce an actionable signal.

Signals carry ticker, bar time, data version and strategy version. Risk checks
precede every paper fill: long-only, no leverage, no negative cash, bounded
allocation and concentration, and valid data. Position-sizing defaults need
review in a later phase; legacy all-in/ATR sizing is not inherited. Signal and
execution deduplication keys include strategy version, ticker, bar and side.

Backtests signal at bar close and fill no earlier than the next eligible bar,
with explicit fees/slippage. Corporate actions must consistently adjust both
bars and holdings; never mix adjusted signals with incompatible fill prices.
Record dataset/config hashes, skipped sessions and all simulated fills. Use
contribution-adjusted returns, drawdown from full marked equity, and clearly
stated assumptions. Future return metrics must use time-weighted or money-weighted
methods when cash flows exist, rather than treating deposits as returns.

## Wallet accounting and storage

Python 3.11+ standard library only; verified here with Python 3.14.7. No install
step or third-party runtime/test dependency is required for Phase 2.
`schema.sql` is the executable version-1 schema. Unsupported future versions and
nonempty unversioned databases are rejected. Future schema changes need explicit,
transactional migrations and backup/restore tests.

There is one USD wallet per database. Initialization books USD 2,000 as an audited
initial contribution exactly once, including concurrent constructors. Each
mutation uses a fresh connection, `BEGIN IMMEDIATE`, SQLite FULL synchronous
writes, a ten-second lock timeout, an invariant check, commit or rollback, and
connection closure. Default SQLite rollback-journal mode is used. A read snapshot
uses one transaction so cash and positions reflect the same state.

Tables:

- `wallet`: singleton cash, net contributions, cumulative realized P&L, in cents.
- `positions`: allowed ticker, positive whole-share quantity, total cost basis
  in cents and last supplied price in cents.
- `events`: unique idempotency key, UTC timestamp, canonical request payload,
  operation kind and post-operation wallet balances. Payload monetary values
  and balance columns are integer cents. IDs provide ordering when timestamps
  coincide. Initial funding, deposits, withdrawals, fills and marks are audited.
  SQL triggers prohibit event updates/deletes through ordinary SQL.

All financial arithmetic uses integers. Decimal is used only to validate and
convert strings/Decimal/integer USD input to cents under a private precision
context. Reject floats, booleans, nonfinite, zero/negative amounts, sub-cent
amounts, unsupported tickers and invalid quantities. Fees may be zero. Maximum
individual amount and wallet cash/contribution/realized/cost magnitudes are
USD 1 trillion; individual trade quantities are at most one billion whole shares.
No rounding of deposits or withdrawals is performed. Cent prices and whole
shares are deliberate v1 limits; fractional shares and finer prices require a
reviewed precision/schema change.

Entry fees enter cost basis. Repeated buys use weighted average total cost.
Partial sales allocate that cost proportionally, rounded to nearest cent
half-up with integer arithmetic. A final sale consumes all residual basis.
Exit fees reduce sale proceeds; fees exceeding proceeds are rejected. Sale
price becomes the remaining position's mark. A buy also updates the aggregate
position's mark. Mark commands are explicit and persisted, with timestamps in
the audit. No live quote fetching occurs; equity reflects last supplied marks,
not guaranteed current prices. Unrealized P&L includes entry fees and excludes
hypothetical future exit fees.

```text
net contributions = initial capital + deposits - withdrawals
market value = sum(quantity * last mark)
unrealized P&L = market value - open cost basis
realized P&L += net sale proceeds - allocated sold cost basis
cash + open cost basis = net contributions + realized P&L
equity = cash + market value
trading P&L = realized P&L + unrealized P&L = equity - net contributions
```

Withdrawals use available cash only; holdings are never silently liquidated.
Net contributions may become negative after withdrawing earned profits; cash
may not. Keys are required for every mutation, unique across all operation
kinds and retained indefinitely. Identical normalized request retries return the
original audit event. Reuse for a different payload/kind raises WalletError.
Rejected requests consume no key and append no event. Financial effect, position
changes, audit and key reservation commit together. Audit records contain enough
inputs to replay deterministic accounting; automated replay verification is
future work.

## Local API contract (future HTTP implementation)

The implemented local Python API is `Wallet`: `deposit`, `withdraw`, `buy`,
`sell`, `mark`, `snapshot`, `history`. The CLI calls only this service.
No HTTP listener runs in Phase 2. Proposed HTTP API uses a thin ASGI layer over
this service; select and pin FastAPI/Uvicorn versions when that phase is approved.

| Route | Contract |
|---|---|
| `GET /v1/wallet` | PAPER state, USD decimal strings, positions and mark freshness |
| `GET /v1/wallet/events` | Paginated immutable event history |
| `POST /v1/wallet/deposits` | `amount` decimal string; required Idempotency-Key |
| `POST /v1/wallet/withdrawals` | Same contract; available-cash check |
| `POST /v1/paper/fills` | Later controlled paper executor, never brokerage orders |
| `POST /v1/paper/marks` | Explicit validated price/provenance updates |

HTTP adapter must translate invalid input to 422, insufficient cash or key
conflicts to 409, lock exhaustion to retryable 503, and successful retries to
the original event. Decimal values must remain JSON strings. Database paths and
SQL are never accepted from HTTP clients. API tests belong to its future phase.

## Security and governance boundaries

PAPER is structural: this package has no brokerage SDK, credentials, order
routing, network calls or real-money mode. An LLM may explain and propose
structured intents but never compute authoritative balances or circumvent risk
checks. Agent tool permissions must exclude funding mutations by default;
funding is an explicit user action. No shell execution or database path access
is exposed to agents. Keep model output outside trusted accounting decisions.

Future server binds `127.0.0.1` only, requires a local bearer token on writes,
checks Origin/Host, disables permissive CORS and rejects cross-site writes.
Loopback alone is not an authorization mechanism. Tokens stay outside source
control and out of logs. Authentication, request-size limits and pagination
are required before exposing HTTP. No listener or automation is added here.

Local CLI/filesystem users are trusted. SQLite is not encrypted; restrict the
data directory to its owner. Audit triggers deter accidental alteration, not
an attacker who controls the database file/schema. Direct manual SQL writes
are unsupported and can violate service invariants. Use local disk, not a
network filesystem. Back up with SQLite's backup API or while all writers are
stopped, retaining an intact database/journal pair for crash recovery. Storage
failure should raise an error; never reset or silently replace an existing wallet.
Physical disk/power-failure recovery has not been fault-tested in this phase.

No Turtle Mini changes, main-branch changes, commits, pushes, scheduler changes
or automatic trading activation are authorized by this work.

## Implementation phases and acceptance gates

1. **Architecture (complete):** isolated package plan, component contracts,
   accounting policies, dependencies and security boundaries documented.
2. **Virtual wallet (complete):** persistent SQLite service/schema, manual CLI,
   audit, idempotency, deterministic fee-aware accounting and automated tests.
   See `dual-ma-ai-test-results.md` for actual execution evidence.
3. **Data and strategy (pending review):** validate ticker identities/provider,
   calendars, split/dividend policy, SMA fixtures and deterministic signal IDs.
4. **Backtesting and paper executor (pending review):** replay parity, realistic
   fill model, sizing/risk policy, no-lookahead tests and contribution-aware metrics.
5. **Local HTTP API and dashboard (pending review):** implement security contract,
   data freshness, pagination and integration tests before any listener is enabled.
6. **Scoped agent tools (pending review):** schema validation, permissions,
   explanations and rejection tests; deterministic core stays authoritative.
7. **Automation (separate explicit authorization required):** disabled-by-default
   scheduler, kill switch, restart recovery, paper-only end-to-end soak testing.

Stop after Phase 2 for user review. No later phase is implemented or activated.
