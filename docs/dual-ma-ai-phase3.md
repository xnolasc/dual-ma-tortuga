# Phase 3 — deterministic strategy engine and backtesting

PAPER research only, on `feature/dual-ma-ai-v1`. No brokerage connection, scheduler,
HTTP listener, agent activation, commits or pushes. Existing wallet code and schema
are unchanged. The Phase 3 scope includes backtesting as requested in this review,
superseding the older Phase 1 roadmap numbering.

## Implementation

| File | Responsibility |
|---|---|
| `dual_ma_ai/market_data.py` | Alpha Vantage retrieval, strict parsing, exchange-session validation, adjusted OHLCV, corporate actions and immutable local cache |
| `dual_ma_ai/strategy.py` | Configurable SMA/EMA crossovers and deterministic signal identifiers |
| `dual_ma_ai/backtest.py` | Isolated shared-cash long-only simulation, execution assumptions, metrics and comparisons |
| `dual_ma_ai/research.py` | Explicit fetch/import/compare CLI, synthetic demo, JSON reports and equity CSV exports |
| `tests/test_research.py` | Data, signal, simulator, CLI and wallet-isolation tests |
| `requirements-backtest.txt` | Pinned direct calendar dependency |
| `requirements-backtest-lock.txt` | Exact installed calendar/transitive versions used for verification |

All allowed tickers: SPCX, LITE, MRVL, TSM, CIEN, COHR, MU, NVDA, AAOI. Default
comparison is one shared USD 2,000 portfolio across the full universe, running
SMA 50/200, SMA 20/50 and EMA 20/50 separately under identical execution assumptions.
Each run starts flat with fresh in-memory cash, holdings and execution-key state.
No `Wallet` object or SQLite database is used by the simulator. It imports only
pure exact-money conversion helpers and the allowed ticker list from `wallet.py`.
There is no backtest wallet path or configurable report output filename.

## Historical data source, cache and precision

The adapter uses HTTPS to Alpha Vantage `TIME_SERIES_DAILY_ADJUSTED`,
`outputsize=full`, historical entitlement only. It returns as-traded OHLCV,
adjusted close, dividends and split coefficients according to the
[provider documentation](https://www.alphavantage.co/documentation/#dailyadj).
This is a premium endpoint; an entitled API key must be configured locally as
`ALPHAVANTAGE_API_KEY`. No subscription is purchased or account configured here.
No key was available during implementation; real-market retrieval, ticker
coverage and historical performance remain **unverified**. Transport tests use
mocked responses, and demonstrated comparisons are explicitly synthetic.

The adapter validates exact ticker match and US/Eastern timezone. It does not
claim historical issuer identity or exchange/listing verification. XNYS sessions
are a common US-equity calendar proxy for the requested universe; holidays,
early closes and special closures are supplied by
[exchange_calendars](https://pypi.org/project/exchange_calendars/4.13.2/).
The verified environment uses version 4.13.2. There is no weekday-only fallback
for real data. Session times are stored in UTC; provider last-refresh text and
actual retrieval time are retained separately.

Each cache JSON records source, format/policy versions, ticker, fetched-at time,
source response, SHA-256 and derived adjusted OHLCV. Raw strings are retained;
JSON decimals are parsed as Decimal, never binary floats. Descriptive adjusted
OHLC = raw OHLC times provider adjusted-close/raw-close ratio. Volume is adjusted
for splits only: pre-split raw volume times subsequent split ratios. Derived
OHLCV values are stored as exact integer or `numerator/denominator` strings.
Cash calculations never consume those descriptive backward-adjusted values.

Cache files live in `data/dual_ma_ai/history/TICKER-HASH.json`. Publication writes
and fsyncs a temporary file, then uses an exclusive link so an existing snapshot
is not overwritten. Identical source payloads retain their original retrieval
time. Loading revalidates source/policy, checksum, normalized OHLCV and all bars.
Use the cache path or returned dataset for a pinned snapshot; the comparison CLI
selects the newest recorded retrieval per ticker. Later refreshes may therefore
change a CLI comparison; dataset hashes in each report identify the exact inputs.

Null/invalid/nonpositive prices, invalid OHLC ordering, negative/noninteger
volume, missing corporate-action fields, duplicate JSON keys, unordered or
non-session bars and missing interior sessions reject the dataset. No interpolation,
forward filling or missing-session skipping occurs. Zero volume is retained and
makes the open ineligible for fills. A bar enters the usable dataset only when
its exchange close is <= the retrieval timestamp. A requested backtest also
requires all session closes <= its explicit as-of time and full window coverage.
Leading/trailing unavailable history is exposed by horizon coverage checks;
it is never represented as a completed 10-year backtest.

## Signals and adjustment policy

SMA uses exact rational arithmetic. EMA is seeded with the first period's SMA,
then follows `EMA = alpha*close + (1-alpha)*prior`, `alpha = 2/(period+1)`.
EMA values are rounded half-up to 24 decimal places using integer arithmetic
after the seed and each recurrence. This is deterministic, independent of the
caller's Decimal context, and bounds denominator growth on decade-long runs.
It can affect a crossover closer than 1e-24 price units to equality.

Signals use a causal, forward-adjusted OHLCV series, rebased to the first warmup
bar's raw unit. Only actions on/before the current bar enter its factor:

```text
factor *= split_ratio
reference = prior_raw_close / current_split_ratio
factor *= reference / (reference - current_dividend)   # when dividend > 0
adjusted_current_OHLC = raw_current_OHLC * factor
adjusted_volume = raw_volume / cumulative_split_ratio
```

This maintains split/dividend continuity without using future corporate actions
to size old trades. Dividends >= the prior split-adjusted close are rejected as
unsupported distributions. The first observed bar is the numeraire; its actions
occur before any simulated holding. Descriptive provider-adjusted OHLCV is
available for inspection, while this causal series drives signals.

BUY: previous short MA <= previous long MA and current short MA > current long MA.
SELL: previous short MA >= previous long MA and current short MA < current long MA.
A persistent trend is not a repeated crossover; the initial portfolio stays flat
until an actual crossing. Signal identifiers include strategy, ticker, candle
date and side. A signal needs two fully initialized MA observations; a 200-day
long MA therefore needs at least 201 completed bars.

The comparison uses a common fixed 201-session pre-window history for all three
presets (`warmup=200`, plus one preceding observation). Each EMA is seeded from
that same clipped history. A crossover on the final warmup session may fill on
the first evaluation session. Older warmup signals never create pre-window holdings.
Custom strategies must use a shared warmup at least as large as their long period.

## Fills, corporate actions and cash

Only a market open strictly after the signal's completed close can fill it.
A zero-volume bar defers the intent to the next eligible session; a new crossover
can supersede a deferred intent and is logged. Data gaps reject the run rather
than fabricating a tradable bar. Last-window signals remain unfilled. Open
positions are marked at the last close; there is no forced close-bar liquidation.

Default commission: 10 bps each side, minimum USD 0.00. Default adverse slippage:
5 bps at the next raw open. Slipped BUY prices round up to cents; SELL prices
round down. Commission and marks round half-up to cents. Default allocation is
10% of preceding marked portfolio equity per new position, capped by current
cash and inclusive of fees. A binary search selects affordable whole shares.
There is one position per ticker, no shorts, pyramiding, borrowed funds or leverage.
Sells execute before buys, with alphabetical ticker priority within each side.
Cash scarcity and state/fee rejections are recorded, not silently executed.

Splits change overnight holdings at the event-date open. Forward splits can
increase whole shares; reverse-split fractions are paid in lieu at raw open,
with proportional cost allocation. This cash-in-lieu price is an explicit model
assumption. Dividends credit overnight holders on ex-date after split handling;
same-day buyers receive none and sellers retain their entitlement. Ex-date cash
availability approximates actual later pay dates. Both income and cash-in-lieu
P&L are included in realized trading P&L and closed-position trade P&L. Deposits
and withdrawals are unavailable in backtests; initial capital is the only
contribution. Every EOD point checks:

```text
cash >= 0
cash + open cost basis = initial contribution + realized P&L
equity = contribution + realized P&L + unrealized P&L
```

All money/sizing/action calculations use integers and rational values. Decimal
with a private 40-digit context is used for reporting statistics. Currency
input limits are inherited from the wallet's pure conversion helpers.

## Horizons, metrics and comparison reports

Supported calendar-year windows are 1, 3, 5 and 10, with February 29 anchored to
February 28 when necessary. Each window requires its full session span and the
common warmup for **every** selected ticker. If one ticker lacks coverage, the
entire portfolio horizon is marked `unavailable`; it is never silently removed.
Default evaluation end is the earliest latest completed cached session across
the selected universe, reported explicitly. Supply `--end` to pin it; stale
coverage cannot satisfy a later requested end.

- CAGR: `(ending/initial equity)^(365.2425/actual elapsed days) - 1`, including the
  baseline immediately preceding the first evaluation session.
- Maximum drawdown: largest peak-to-trough fraction from the full daily marked
  equity curve, including cash and open positions.
- Sharpe: mean simple daily return / sample standard deviation times sqrt(252),
  risk-free rate fixed at zero. Undefined for insufficient/zero variance or
  a return whose denominator is zero; JSON uses null.
- Win rate: profitable closed round trips / all closed round trips; includes fees
  and holding income, excludes still-open positions. Zero-P&L trades are not wins.
- Profit factor: gross positive closed P&L / absolute gross negative closed P&L.
  Null when there are no losses; `profit_factor_status` distinguishes that case.
- Number of trades: completed position round trips; number of fills is also
  reported independently. Equity curves include cash, market value, P&L and shares.

Rates are decimal fractions (0.10 = 10%), not percentages. Very large annualized
values from short synthetic windows use finite scientific notation. JSON reports
include input/config hashes, source/coverage timestamps, execution assumptions,
closed trades, fills, event reasons, unfilled signals and open positions. Each
run has a reproducible SHA-256 ID. Files are written only beneath
`data/dual_ma_ai/backtests/`; each run also exports an equity CSV. These directories
are already excluded by the Phase 2 runtime-data ignore rule.

## Run a reproducible sample

The Phase 2 wallet still needs only the standard library. Phase 3 requires the
calendar dependency. The exact verification lock needs Python 3.12+ (verified on
3.14.7); `requirements-backtest.txt` lets pip resolve compatible transitive
versions on other supported interpreters, which should be revalidated.

```bash
cd "$HOME/dual-ma-tortuga"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-backtest-lock.txt
.venv/bin/python -m dual_ma_ai.research compare --demo --tickers MU --years 1 --allocation-bps 10000
```

The sample is explicitly **SYNTHETIC**, with triangle prices and a known split;
it demonstrates software behavior, not MU market performance. It prints metrics
and the full report path. Inspect the JSON or open the accompanying equity CSV.
For all tickers and horizons under the default 10% allocation:

```bash
.venv/bin/python -m dual_ma_ai.research compare --demo --years 1 3 5 10
```

Real data, after an entitled key is configured locally:

```bash
.venv/bin/python -m dual_ma_ai.research fetch --tickers MU
.venv/bin/python -m dual_ma_ai.research compare --tickers MU --years 1 --allocation-bps 10000
```

Do not paste credentials into command arguments, documentation or version
control. Fetch reads the key from the environment and sanitizes network errors.
It does not automatically refresh during comparisons. To import a previously
retrieved response, retain its actual retrieval timestamp:

```bash
.venv/bin/python -m dual_ma_ai.research import-response MU /path/to/alpha-vantage-MU.json --fetched-at 2026-10-10T00:00:00Z
```

To fetch the full universe, omit `--tickers`; any provider error stops the fetch
and cached successful responses remain available. An unsupported ticker or rate
limit must be resolved without silently substituting another instrument.
Custom strategy and execution example:

```bash
.venv/bin/python -m dual_ma_ai.research compare --demo --tickers MU --years 1 --kind EMA --short 10 --long 30 --warmup 200 --commission-bps 15 --slippage-bps 10
```

Tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
python3 -m unittest discover -s tests -p test_wallet.py -v
```

## Limitations and remaining work

The fixed current universe is retrospectively selected. Reports prominently
state that results are not survivorship-bias-free; no delisted-universe coverage
or point-in-time membership is supplied. Matching provider symbols does not
verify historical ticker/issuer changes. Cached current-vintage data may contain
later corrections. Causal signal and fill calculations prevent algorithmic
lookahead; they do not turn a revised dataset into a point-in-time data archive.

Real-data entitlement, coverage and corporate actions for each requested ticker
remain to be verified. No real historical performance figures are claimed in
this phase. Missing sessions caused by halts are rejected; an explicit halt/status
model would be needed to distinguish them from feed outages. Unmodeled spinoffs,
mergers and special distributions require additional action types. The current
calendar is a US-equity proxy, not per-security listing verification. There is
no tax, interest, benchmark, liquidity, spread/order-book or market-impact model.
Reports/cache lack a remote attestation and are trusted local artifacts.

The next review may authorize real-dataset validation or additional models.
HTTP API/dashboard, external agent tools and automation remain unimplemented
and disabled. No Turtle Mini files or legacy trading scripts were touched.
