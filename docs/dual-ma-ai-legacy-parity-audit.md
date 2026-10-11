# Legacy strategy parity audit

Audit date: 2026-10-10. Work branch: `feature/dual-ma-ai-v1`.
Reference: local `main` commit **73b154a21eebedabe68346130f46972d3119102d**.
Local `origin/main` resolves to the same commit; no remote fetch or checkout was
performed. This audit describes that pinned source, not a claim about a later
GitHub revision. Phase 4 has not started.

## Conclusion and compatibility boundary

The Phase 3 presets are **not legacy-equivalent**. They change MA periods,
instruments, data adjustments, capital, sizing, execution timing and accounting.
A custom modern SMA 10/30 shares the original crossover inequalities, but it is
still not a full reproduction of the original trader.

Original behavior is now a named, immutable **LEGACY** configuration in
`dual_ma_ai/legacy.py`, also exported from `dual_ma_ai.strategy`. It preserves
SMA 10/30 and all three original sizing ledgers, including float arithmetic,
fractional shares, immediate latest-close fills and accounting quirks. It is an
**offline captured-polling replay**, not a live trader or an unbiased daily
backtest. It imports no networking, broker, SQLite or legacy persistence code.
It reads only a caller-supplied snapshot file and prints its report. No defaults
in the modern strategy, backtest or wallet were changed.

`PRESETS` still contains only SMA 50/200, SMA 20/50 and EMA 20/50. Modern `signals`
rejects LEGACY with an explicit instruction to use `legacy.replay`; LEGACY cannot
silently acquire the modern engine's different safety or execution semantics.
The compatibility profile is frozen to the audited version; changes to it require
a new profile/audit rather than silently modifying the reference behavior.

## Source inspection

Read directly with `git show main:<file>`:

- `dualma_trader.py`: `get_daily_klines`, `compute_sma`, `compute_atr`,
  `calcular_shares`, `procesar_ticker`, `correr_ledger` and `main`.
- `dualma_common.py`: tickers/symbol mapping, capital, fees, minimum notional,
  MA periods, fixed percentage, endpoints and JSON paths.
- `dualma_ledger.py`: initialization, sizing, buy/sell, wipeout, load/save.
- `dualma_dashboard.py`: live quote valuation, position P&L, cash display,
  wipeout presentation, server binding and refresh behavior.
- `README.md`: SMA 10/30 experiment, three independent USD 1,000 ledgers,
  tokenized four-ticker universe, paper-only intent and advertised report metrics.

Also inspected `main:comparar_ledgers.py` and `main:nueva_ronda.py` to verify the
README's report/accounting and reset claims. These supplemental files were not
executed or changed. None of the original live scripts, dashboard or reset
utility was launched. The five primary file SHA-256 values are pinned in
`REFERENCE_SHA256` and tested against the Git objects.

## Original rules exactly

Fetch 45 Binance `1d` klines per ticker, separately for each ledger, using
`MU/SNDK/WDC/BE -> <ticker>BUSDT`. Convert only high, low and close to Python
float. Discard candle timestamps, closure flags, open and volume. If no response
or fewer than 31 bars, log insufficient history and skip that ticker.

Calculate four simple arithmetic means: 10 and 30 closes ending at the latest
bar, plus the same windows excluding that last bar. When flat, buy only if
`short_prior <= long_prior` and `short_now > long_now`. When holding, sell only
if `short_prior >= long_prior` and `short_now < long_now`. No level-only entry,
stop-loss, take-profit, time exit, shorting or pyramiding exists in the trader.

Use the latest returned close as both the crossover input and immediate fill
price. There is no check that the candle is completed. Callers/polling frequency
control when the script runs; missed intermediate crossings are not replayed.

For sizing, let C = available cash, I = initial capital, P = latest close,
f = 0.001, and N = the arithmetic mean of the last 14 true ranges. True range is
`max(high-low, abs(high-previous_close), abs(low-previous_close))`; this ATR is
not Wilder-smoothed. Require at least 15 bars for ATR computation. The trader
already requires 31 for its MAs and computes ATR even for non-ATR ledgers.

```text
todo gross       = C / (1 + f)
porcentaje gross = min(I * 0.15, C / (1 + f))
atr shares       = I * 0.01 / (2 * N), when N > 0
atr gross        = min(atr shares * P, C / (1 + f))
all modes        = 0 shares when gross < 10 USD; otherwise gross / P
```

The percentage limit is **15% of initial capital before fees**, not current
portfolio equity. ATR risk also uses initial capital. `2*N` is a sizing divisor,
not an implemented stop or guaranteed portfolio-loss limit. Quantities are
fractional and unrounded; log display rounds shares to six decimals only.

Buy gross = shares * price; fee = gross * 0.001; total = gross + fee. Reject only
when `total > cash + 0.01`. On acceptance, round the new cash, cumulative fees
and stored total entry cost to two decimals using Python `round`. Keep raw
float quantity/entry price. `entry_time` and event `timestamp` are two separate
wall-clock calls. The lower-level buy function overwrites a same-ticker position;
normal trader position gating prevents that call while already holding.

Sell the entire stored quantity, calculate net proceeds after the same fee,
round event P&L = net proceeds minus rounded stored entry cost to two decimals,
round new cash/cumulative fees, remove the position and append a sale event.
A missing position returns None. The ledger has no stored aggregate realized
or unrealized P&L and no contribution ledger.

Run three independent ledgers sequentially in `todo`, `porcentaje`, `atr` order,
with USD 1,000 each and no shared cash. Within each ledger the priority order is
MU, SNDK, WDC, BE. Each ticker is processed once per invocation. Save each whole
JSON ledger after its ticker loop, with no atomic replacement, transaction,
locking or durable request key. Existing JSON is resumed; initial capital is
used only to create a missing ledger. A round reset archives and recreates
ledgers; it is not a deposit into existing positions.

## Difference inventory: signals and market data

| Area | Original main / preserved LEGACY | Modern Phase 3 engine |
|---|---|---|
| Profile identity | One SMA 10/30 signal rule, three sizing modes | Separate SMA 50/200, SMA 20/50 and EMA 20/50 presets; custom integer periods allowed |
| MA arithmetic | Python float `sum(window)/period` | Exact rational SMA; EMA seeded with period SMA and rounded half-up to 24 decimal places per step |
| EMA | Absent | Supported with alpha = 2/(period+1) |
| Bullish inequality | Prior short <= long, current short > long | Same inequality, but potentially different periods, prices and numerical ties |
| Bearish inequality | Prior short >= long, current short < long | Same inequality, with the same caveats |
| Position gating | Only calculate actionable entry when flat; exit when holding | Signals generated independent of holdings; executor records/ignores incompatible intents |
| Persistent trend/equality | No fresh BUY solely because short is above long; current equality does not trigger | Same, on modern normalized inputs |
| Warmup | Latest 45 requested bars; at least 31 returned; no initial backtest boundary | Long+1 observations required; comparison clips a common 201 pre-window sessions and starts flat |
| Warmup trades | Polling ledger may already hold positions from earlier calls | No pre-window positions; only final warmup crossover can fill first evaluation open |
| ATR | Simple mean of last 14 true ranges; only legacy ATR sizing uses it | No ATR sizing or ATR signal calculation in Phase 3 |
| Universe | MU, SNDK, WDC, BE | SPCX, LITE, MRVL, TSM, CIEN, COHR, MU, NVDA, AAOI; only MU overlaps |
| Instrument type | Tokenized bStock markets in README, mapped to BUSDT symbols | Requested equity identifiers from Alpha Vantage, no Binance mapping; historical identity still unverified |
| Quotation currency | Binance symbols end in USDT; ledger labels capital as USD, with no FX conversion | USD accounting/US equity data assumptions; no USDT equivalence or token redemption model |
| Source | Public Binance klines; dashboard quotes via Binance ticker endpoint | Entitled Alpha Vantage daily adjusted history; explicit source metadata |
| Candle completeness | Latest returned candle accepted even while forming | Completed exchange sessions only; exchange close <= as-of time |
| Calendar/time | No session validation; daily bars treated as ordinal provider observations | XNYS US-equity session proxy, holidays/early closes/special closures checked, UTC boundaries |
| Timestamp provenance | Provider timestamps discarded; audit records runtime epoch seconds | Session open/close, retrieval time, source refresh text, dataset/config hashes |
| Data fields | Float high/low/close only; open and volume discarded | Raw and adjusted OHLCV plus dividends/splits; raw open used for fills |
| Price adjustments | No declared policy or explicit corporate-action accounting | Descriptive adjusted OHLCV plus causal forward-adjusted signal series; raw as-traded fill/mark prices |
| Splits/dividends | No explicit quantity split, cash dividend or cash-in-lieu handling | Effective-date holding adjustment, ex-date dividend credit, fractional split cash-in-lieu assumption |
| Missing data | Fetch errors are logged then ticker skipped; gaps/duplicates/order/null quality not validated beyond conversions and bar count | Strict validation, no forward fill; malformed/interior missing data rejects; insufficient full window becomes unavailable |
| Volume | Unused | Zero volume defers a pending fill to a later eligible open |
| Data caching | None in trader/dashboard; ledger state is JSON | Content-addressed immutable snapshot cache with verification |
| Request consistency | Twelve separate per-cycle OHLC fetches across ledgers; responses can differ | All presets use the same pinned dataset snapshots/assumptions per comparison |
| Polling coverage | Only the two most recent windows are considered per call; intermediate crossings can be missed | Full chronological evaluation on every available session in a selected window |
| Survivor/issuer claims | Fixed four-ticker experiment; no membership/identity provenance | Fixed retrospective universe explicitly disclosed; no survivorship-free or point-in-time claim |

## Difference inventory: sizing, risk and execution

| Area | Original main / preserved LEGACY | Modern Phase 3 engine |
|---|---|---|
| Initial capital | USD 1,000 per ledger; USD 3,000 across the independent experiment | USD 2,000 per isolated strategy portfolio by default; configurable |
| Capital independence | Three parallel books for different sizing rules | Each strategy is a separate run/book; selected tickers share its single cash balance |
| All-in | Available cash divided by 1.001, fractional quantity | Not a default; configurable 100% allocation still applies whole shares, fee budget and modern timing |
| Percentage sizing | 15% of initial capital gross, fee added outside that limit | Default 10% of preceding marked equity including fees; configurable bps |
| ATR sizing | Initial capital * 1% / (2 * simple ATR), capped by cash | Absent; setting an allocation percentage cannot reproduce ATR sizing |
| Share units | Fractional shares with full float precision | Whole shares, affordable quantity found by binary search |
| Minimum trade | USD 10 gross threshold in sizing functions | No USD 10 minimum notional; at least one affordable share and valid cent fill price |
| Fees | Fixed 0.1% each side; raw float fee, cumulative fees rounded after each update | Default also 0.1% (10 bps), configurable rate/minimum; individual fees rounded half-up to cents |
| Slippage/spread | None | Default adverse 5 bps each side; configurable, fills rounded adversely to cents |
| Signal/fill relationship | Same latest-close price immediately | Strictly later eligible raw market open; no same-bar fill |
| Current-bar future information | Forming candle may affect entry/exit | Completed close drives signal; later bar open drives fill |
| Priority | Ticker order MU/SNDK/WDC/BE; buys/sells processed in that order | All exits first, then entries; alphabetical ticker order within each side |
| Cash constraint | Buy rejection allows cost <= cash + USD 0.01 | Cost including fees must fit cash/budget; cash cannot be negative |
| Concentration | All-in may exhaust entire ledger; fixed/ATR limits do not enforce portfolio-wide risk caps | Configurable per-entry equity allocation, one position per ticker, shared cash bound; no additional portfolio-loss budget |
| Stops/take-profit | Neither; ATR does not install a stop | Neither in Phase 3; bearish cross is the strategy exit |
| Shorting/leverage | Trader long/flat; direct ledger helpers lack generic amount validation | Long-only, no leverage/borrowing; exact financial input checks |
| Position additions | Trader prevents pyramiding; direct buy can overwrite same ticker | Backtester ignores BUY while held; SQLite Wallet's separate manual buy API aggregates basis, not used by backtests |
| Idempotency | Position state suppresses repeated BUY while held; no candle/request key; same-candle whipsaw can sell and re-enter | Deterministic strategy/ticker/date/side keys and per-run execution set; wallet mutations use durable unique keys |
| Failed trade reporting | Trader logs COMPRA without checking comprar's boolean return | Executor records rejection reason rather than a successful fill |
| Failed/new intents | No queued intent or retry after a skipped crossing unless a future polling response again satisfies it | Zero-volume intents persist; opposite new crossover may supersede them; rejected-budget intents are consumed/logged |
| Run-end holdings | Remain persisted until later invocation | Marked at last close, not forcibly liquidated; last-window signals recorded unfilled |
| Scheduling | Script runs one cycle when invoked; README gives manual command, no scheduler proven by these files | Research runs explicitly invoked; scheduling and automatic trading absent |

## Difference inventory: accounting, metrics and UI

| Area | Original main / preserved LEGACY | New wallet / modern backtester |
|---|---|---|
| Numeric money | Binary float plus Python round to 2 decimals; tie behavior can differ from Decimal | Integer cents/rational sizing and actions; half-up amounts, adverse fill-price rounding; private Decimal context for statistics |
| Financial invariants | Not checked; rounded cash/cost/P&L can drift | Check cash + open basis = contributions + realized P&L, and full equity identity |
| Initial funding | Initial balance field only | Wallet initial USD 2,000 is an audited contribution; backtest initial capital is its only contribution |
| Deposits/withdrawals | No funding API in original ledgers; README says no top-ups | Wallet supports audited funding; backtests have no funding actions and never touch that wallet |
| Entry basis | Gross+fee total rounded separately; quantity/entry price unrounded | Gross cent-price notional plus individually rounded fee; consistent cents basis |
| Realized P&L | Per-sale net P&L in event list, not a cumulative stored balance | Explicit realized accumulator, including corporate-action income in backtests |
| Unrealized display | Dashboard `(live quote - raw entry price)*shares`; percentage price return; excludes entry fee | Market value minus fee-inclusive open basis; hypothetical exit fee excluded |
| Marking time | Dashboard queries a fresh live quote independent of the trade candle | Backtest EOD raw close; wallet last explicitly supplied persisted mark |
| Equity display | Dashboard's large capital number is available cash, not cash+positions | Wallet/backtest explicitly report cash, market value and full equity |
| Equity report | Supplemental report adds available cash and quoted positions; missing quote silently omits that position value | Missing daily valuation rejects data/run instead of silently valuing it as absent |
| Drawdown | Supplemental report uses initial capital plus event cash_after, including purchase cash drops; its prose says sale points but code includes all events | Full daily marked-equity peak-to-trough drawdown, including open holdings |
| Returns/analytics | Supplemental report total return vs fixed initial capital, closed sales/win rate, fees, no-capital count and wipeout; no CAGR/Sharpe/profit factor | CAGR, Sharpe, profit factor, total return, closed round trips/fills, equity curves; funding separated |
| Win-rate units | Legacy report percentage 0–100; winning sale event has pnl_neto > 0 | Fraction 0–1; positive closed round-trip P&L includes modeled dividends/CIL |
| Wipeout | Cash < USD 10 AND no positions; dashboard duplicates hardcoded threshold | Modern engine has no legacy wipeout status; a run can continue with < USD 10 if a share is affordable |
| Cash-starved count | Report counts matching log lines, not necessarily distinct sessions | Backtest structured insufficient-budget events; not presented as equivalent legacy cycle count |
| Persistence | Whole JSON write, non-atomic, no lock/audit immutability guarantees | Wallet atomic SQLite transactions/immutable event triggers; backtests in-memory with descriptive JSON/CSV exports |
| Audit fields | Epoch-second runtime, reasons, rounded cash_after, no durable request key | Wallet UTC events and durable keys; backtest signal/fill/session times, deterministic keys, configs/provenance |
| Restarts | Original resumes saved JSON; round utility archives and recreates all three | Wallet resumes its DB; each backtest/replay starts fresh isolated state |
| Interface | HTML refresh every 15 sec on port 8894; binds 0.0.0.0, public quote requests | New system currently has wallet/research CLIs; no HTTP dashboard/server activated |
| Cross-project links | README mentions companion Turtle; actual original imports are local Dual MA modules | Independent package; no Turtle Mini imports, file access or configuration |

## Preserved quirks, confirmed examples and unresolved differences

LEGACY intentionally preserves, rather than silently repairs, these behaviors:

1. A buy with total cost USD 100.009 against USD 100 passes the `cash + 0.01`
   guard and produces **cash -0.01** after rounding. Source-vs-LEGACY test agrees.
2. A buy with computed total 10.005 (`price = 10.005/1.001`, one share) can record
   **cash 990.00 and basis 10.01** against initial 1,000.00: a one-cent accounting
   drift. Modern invariants must not be relaxed to reproduce this.
3. Direct `comprar` can overwrite a held ticker's quantity/basis rather than
   aggregate it; normal trader gating prevents this route.
4. Same unfinished candle snapshots `[10]*44+[12]`, then `+[8]`, then `+[12]`
   can produce BUY, SELL, BUY on the same candle identity. Original timestamps
   and completeness fields were discarded, so it has no candle idempotency.
5. For identical decimal strings `[0.1]*44+[0.10000000000000001]`, the original
   float conversion loses the final increment and generates no entry, while
   modern exact SMA 10/30 generates BUY. Numeric tie parity is not guaranteed
   between LEGACY and modern even when periods and quoted values match.
6. With flat 10s followed by 12, both detect a normal SMA 10/30 BUY. Original
   percentage ledger fills **12.5 shares at 12** immediately. Modern configured
   with the same capital and 15% budget, zero slippage, 10-bps fee, then next bar
   open 15, fills **9 whole shares at 15**. Matching MA parameters alone cannot
   reconcile timing, fee-budget semantics and fractional sizing.

Remaining parity limits:

- Source-vs-LEGACY numerical decisions and ledger transitions are established
  for the regression scenarios and seeded polling stream, not every possible
  malformed provider payload, platform floating-point edge or process crash.
- Tests pin five `main` source hashes and refuse a moved main ref. They execute
  original functions in isolated modules with injected clocks and snapshots;
  they do not run original network, filesystem persistence or server lifecycles.
- Polling replay can accept separate data per mode/ticker, matching the original
  twelve-request structure, but real historical polling responses and timing
  were not available. All shipped fixtures are explicitly synthetic. There is
  no claim of real historical fill or live Binance symbol-availability parity.
- Replay records decision messages, not the original wall-clock log prefix,
  network-exception text, start/end banners, HTML layout or file-write failures.
  It does not recreate original JSON files or bind a server.
- Captured cycle timestamps stamp replay events deterministically. Exact
  per-operation timing requires a supplied clock to the low-level functions;
  their original separate timestamp calls are regression-tested.
- Tokenized instruments/USDT cannot be substituted with new equity/USD bars
  and called source parity. SNDK/WDC/BE remain available only in LEGACY; adding
  them to the new SQLite schema/universe was not authorized and was not done.
- LEGACY's same-bar/unconfirmed behavior cannot also satisfy the modern
  completed-bar/next-open rule. LEGACY is therefore quarantined offline. Whether
  Phase 4 should use modern presets, a separately named safer SMA 10/30, or a
  production migration of other legacy rules remains a review decision.
- The original dashboard/report and modern metrics are not equivalent; the
  modern fee-inclusive P&L and equity definitions remain unchanged. The replay
  output exposes the exact original ledgers, not invented legacy Sharpe/CAGR.

## Run and inspect parity

No dependencies are needed beyond Python and local Git for the targeted audit:

```bash
python3 -m unittest discover -s tests -p test_legacy_parity.py -v
python3 -m dual_ma_ai.legacy tests/fixtures/legacy_snapshots.json
```

The sample is synthetic and provides separate `todo`, `porcentaje`, `atr` maps
per polling cycle. Each map has optional MU/SNDK/WDC/BE arrays of high/low/close
values. Missing ticker responses skip, exactly as the original failed/absent data
path at the decision layer. Values undergo the same float conversion as the
original fetched OHLC. Explicit `timestamp` is required; no real clock or network
is consulted. The sample report is also saved in
`docs/dual-ma-ai-legacy-synthetic-replay.json` for review.

Full regression suite with the existing Phase 3 environment:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Actual outputs and integrity verification are recorded in
[dual-ma-ai-legacy-test-results.md](dual-ma-ai-legacy-test-results.md).
No source-file modifications, SQLite wallet changes, Turtle Mini changes,
commits, pushes or Phase 4 implementation are part of this audit.
