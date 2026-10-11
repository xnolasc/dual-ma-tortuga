# Phase 5A cold-start research correction

**Disposition:** isolated validation passed. Both production LaunchAgents remain disabled and unloaded; this work did not activate either service. The dashboard remains read-only, PAPER execution remains disabled, and no wallet or brokerage endpoint was used.

## Confirmed cause

The failed live acceptance recorded the initial `/api/signals` panel timing out at the unchanged **20-second** research deadline: the handler returned HTTP 503 after 20.112 seconds while the child calculation continued. An earlier cold run recorded 1,057.670 ms process-pool wait plus 33,005.249 ms signal-panel compute. This was an application cold-start cost, not a browser polling or JavaScript failure.

Before editing, I profiled `_signal_panel_compute` using the existing validated local Alpaca IEX cache, alongside the same 23-report catalog scan. The signal child took 16,883.46 ms under `cProfile` (17,157.98 ms total concurrent wall time). It made 21 independent strategy calls for seven REAL tickers and three presets. Those calls repeated full-bar validation, causal adjustment, and identical moving-average work. The profile attributed 10,225 ms to `signals`, 7,183 ms to repeated `sum` operations over moving-average windows, and 4,964 ms to Alpaca cache load/parse work. The panel contained nine universe rows: seven REAL, two UNAVAILABLE, and one informational crossover.

Coverage requests had a separate coordination defect. `_memoized` held a per-key mutex for only five seconds. Identical cold `/api/coverage?feed=iex` requests therefore returned HTTP 503 while the first request was still computing; the observed live batch had three such 503 responses and one successful response. The coverage computation itself was shared by the process-pool key, but the HTTP callers did not share its in-flight result.

## Correction

- Added `signals_many`, which validates a dataset and applies causal adjustment once, then reuses each distinct moving-average series across the requested strategies. SMA uses a rolling exact `Fraction` sum, retaining the existing exact arithmetic. EMA seeding and deterministic precision are unchanged.
- The signal panel now calculates the three modern presets together once per ticker. This removes repeated full-history validation, causal adjustment, SMA 50 work, and duplicate EMA/SMA period calculations.
- Replaced the five-second per-key mutex with a shared `Future` keyed by request identity and the current source fingerprint. Duplicate requests wait for the same result or failure. In-flight keys are capped at 16 and each key admits at most 32 waiters. Waits preserve the existing 20-second deadline; capacity exhaustion and deadline expiry remain honest HTTP 503 responses.
- A producer always wakes waiters on success or failure and removes its flight entry. A later request can retry after a worker failure. Source fingerprints are checked after work before results are returned or cached, so changed inputs are not represented as current.
- The monitor path remains independent of research work. It does not use these futures, the process pool, the Alpaca cache, or research fingerprints.

## Measured results

| Cold validated-cache profile | Before | After |
| --- | ---: | ---: |
| Signal child compute, concurrent with catalog scan | 16,883.46 ms | 8,487.56 ms |
| Combined cold concurrent wall time | 17,157.98 ms | 8,760.19 ms |
| Signal result | 7 REAL, 2 UNAVAILABLE, 1 crossover | 7 REAL, 2 UNAVAILABLE, 1 crossover |

The signal child improved **49.7%** in this profile. The remaining approximately 4.8 seconds is validated cache file parsing/validation; the first-run exchange-calendar import adds approximately 1.9 seconds. These numbers include profiling overhead and are isolated measurements, not a live-launchd promise. No missing history was filled or substituted.

The exact isolated workload from the failed acceptance ran 48 simultaneous `/api/monitor` GETs alongside four `/api/reports`, four IEX `/api/signals`, and four `/api/coverage` GETs. Three runs measured **48/48 monitor HTTP 200** with p95/max of **89.59/96.44 ms**, **92.40/92.99 ms**, and **91.11/94.61 ms**. All 12 research requests returned HTTP 200 in each run; report, signal, and coverage process-pool work each ran once; the monitor fixture retained one event with one unique ID; and SPCX/AAOI remained `BLOCKED` with no signal values. The 20-second timeout regression still returns 503 and keeps the process-pool job reusable. A separate shared-flight test held an identical computation for 5.1 seconds and confirmed all six callers received the one result.

Eight isolated browser suites passed: desktop/mobile dashboard, mocked Alpaca feed selection, real cached Alpaca display, delayed-wallet health rendering, stale/blocked-symbol replay, monitor polling, eight-page latency probe, and the corrected 80-second polling acceptance. The final polling acceptance observed nine monitor polls, one page navigation, an advancing visible heartbeat, no failed requests, no console or page errors, and correct `DEGRADED`/blocked-symbol rendering. The eight-page latency probe completed in 2.05 seconds, with all API responses HTTP 200 and no JavaScript errors.

## Verification and safety

- `.venv/bin/python -m unittest discover -s tests -v`: **202 passed** in 111.495 seconds, including LEGACY parity, explicit flight/waiter capacity and deadline checks, and the exact 48+4+4+4 workload.
- All eight browser suites passed on temporary localhost services. Synthetic browser fixtures are identified as synthetic; the real-cache browser test used GET requests only.
- The protected-artifact set was recounted and SHA256 hashed: **254 files** (Dual MA historical/cache/backtest data excluding runtime monitor files, Phase 4.1/4.2 artifacts, and the five frozen original LEGACY/README sources). The local integrity manifest was used for verification and is intentionally not published. The live pre/post audit reports zero changed and zero missing artifacts. Browser screenshots and raw polling evidence were directed to temporary storage.
- Wallet and audit-history digests match their verified baselines. SQLite integrity is `ok`; no wallet or audit records changed.
- The full suite passed all LEGACY parity checks; no original LEGACY files, Turtle Mini, BHAP, historical backtest, or wallet file was edited. No dependencies were installed, no trades or orders were made, and nothing was committed or pushed.
- Both LaunchAgents remain disabled and unloaded. Port 8895 was not used; the isolated replay fixture was stopped after browser validation.

## Remaining limitation

The cold signal panel now completed under the 20-second request budget on the measured cache, but the first-run cache parse/calendar work remains the dominant cost. A larger cache, slower disk, or different host load could still cause a genuine timeout, which correctly remains a 503. The results here are isolated; production launchd acceptance has not been attempted and still requires separate authorization.
