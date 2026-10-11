# Phase 5A — dashboard concurrency correction

**Disposition:** isolated acceptance passes; production LaunchAgents remain disabled and unloaded. No production activation was attempted. The dashboard continues to be READ ONLY and PAPER execution remains disabled.

## Findings

The five-connection listener backlog was a real contributor to the monitor latency failure. The preserved live run had 48 successful monitor responses at p95 **4,191.195 ms** and max **4,201.608 ms**, while the recorded handler-side p95/max were only **252.479/260.497 ms**. That left most of the delay before the request handler. The older server did not record accept time, so the original run alone could not prove the cause.

I ran an A/B regression against two isolated dashboard listeners with the same 60-request shape: 48 `/api/monitor` requests plus four each to `/api/reports`, `/api/signals?provider=alpaca&feed=iex`, and `/api/coverage?feed=iex`. Only the listener backlog differed: the old size of 5 versus the corrected size of 128. Across repeated runs, queue 5 monitor p95 ranged **218.84–2,087.67 ms**, while queue 128 ranged **81.63–129.84 ms**. In a cold run, queue 5 reached **2,070.76 ms p95 / 3,985.18 ms max** and **2,060.52 ms p95 client-to-accept**; queue 128 measured **81.63 ms p95 / 83.63 ms max**. The corrected exact-load regression most recently measured **98.46 ms p95 / 104.56 ms max** across 48 monitor requests. The backlog hypothesis is confirmed as a material contributor; run-to-run variance shows it was not the only load factor.

The prior four report requests shared one cold catalog calculation. The catalog re-parsed and hash-verified 23 large report files; duplicate callers waited behind the same cache-key calculation. Signal and coverage work shared the same two-process pool. That pool accepted unbounded pending submissions and callers waited as long as 120 seconds, allowing research requests to queue behind one another. The preserved failure lacks old per-stage pool timing, so it cannot separate worker startup, queued wait, and CPU time for the historical 103–107 second report request. A read-only profile through the corrected process pool measured catalog **198.19 ms pool wait + 1,010.18 ms compute** (1,218.75 ms request total), signals **0.35 ms wait + 6,040.19 ms compute** (6,045.38 ms total), and coverage **0.34 ms wait + 17.09 ms compute** (18.90 ms total). This shows the old timeouts were contention/starvation, not the inherent cost of the cached data operations after removing redundant work.

The real Alpaca profile exposed repeated full-history parsing and calendar creation inside per-ticker loops. Signal calculation now creates one XNYS calendar per calculation and passes it through all ticker validations. Coverage uses one shared calendar. `AlpacaCache` now keeps a bounded 32-entry decoded-history cache, keyed by file identity/size/timestamps and calendar range. It revalidates on a changed fingerprint and checks that the file did not change during validation. With actual local IEX history, the isolated signal panel measured **6.443 seconds** before this cache optimization; after it, the process-pool profile measured **6.045 seconds** on the first calculation and **18.90 ms** for coverage using the validated cache.

## Changes

- Raised the finite TCP listen backlog to 128 and bounded active dashboard handlers at 96. When handler capacity is full, the server returns a bounded 503 and closes that connection instead of creating unlimited threads.
- Kept health reads on the lightweight SQLite monitor snapshot path. Monitor requests do not acquire research locks, wait for process-pool slots, read report files, or build exchange calendars.
- Capped process-pool work at two running workers plus two queued jobs. A third-party request beyond that capacity returns promptly as unavailable. Completed/timed-out jobs are bounded and keyed by function/input so duplicate requests share work rather than submit duplicates.
- Added a 20-second research request deadline and a five-second same-key wait. On a deadline, the endpoint fails closed; an already submitted job remains reusable so a later request can consume its result. Cache entries for unchanged inputs may be served after the display TTL; changed fingerprints are never returned as current.
- Added timing for client-to-accept, accept-to-thread dispatch, accept-to-handler, handler/serialization/socket-write, and child process-pool queue/compute stages. Request IDs are sanitized before logging. The client-side regression records complete response latency.
- Kept in-memory request timing to the latest 1,024 samples and process-pool timing to the latest 128 samples so diagnostics remain bounded over long service lifetimes.
- Corrected `dashboard_browser_live_activation.cjs` to measure the full polling window and request cadence independently of heartbeat changes. It no longer ends after the first heartbeat update. The real-cache browser test also waits for its initial UI signal/coverage requests before sending duplicate direct probes.
- Added clean process-pool child SIGINT handling so a parent dashboard can shut down without idle spawned workers printing tracebacks.

## Verification

The exact 48+4+4+4 localhost workload passed with **48/48 monitor HTTP 200**, p95 **102.05 ms**, max **106.58 ms** in the final full-suite run. That run recorded client-to-accept p95 **89.50 ms**, max **95.09 ms**, thread-dispatch p95 **1.29 ms**, and handler p95 **17.43 ms**. Report, signal, and coverage calculations completed once each; the bounded pool logged their queue and compute stages. A separate test fills all four admitted research slots with CPU work, verifies excess work is rejected, and confirms monitor reads remain below the two-second target. Event IDs remained unique and append-only, monitor status remained `DEGRADED`, and SPCX/AAOI remained `BLOCKED` with empty signal arrays.

The corrected browser polling test passed against isolated temporary monitor state: **9 monitor polls in 80 seconds**, advancing visible heartbeat, one page navigation, no failed requests, no JavaScript errors. The eight-tab browser probe passed with all APIs HTTP 200 and no console/page errors. The read-only REAL-cache browser test passed for IEX provenance and unavailable tickers.

Final checks:

- `.venv/bin/python -m unittest discover -s tests -v` — **197 passed**, 127.494 seconds. This includes the full LEGACY parity suite.
- Browser suites passed: `dashboard_browser.cjs`, `dashboard_browser_alpaca.cjs`, `dashboard_browser_live.cjs`, `dashboard_browser_health.cjs`, `dashboard_browser_health_replay.cjs`, `dashboard_browser_monitor.cjs`, eight-tab `dashboard_browser_latency_probe.cjs`, and corrected `dashboard_browser_live_activation.cjs` against temporary state.
- All 254 files from the recorded protected-artifact baseline match their SHA256 values; no protected files are missing.
- Wallet and audit-history digests matched the verified preflight; SQLite `integrity_check` is `ok`. No wallet or audit records changed.
- Both LaunchAgents still have disabled overrides and are unloaded. Port 8895 is free; no dashboard/worker fixture process remains.

No new dependencies were installed. No real or virtual trade was made. No report, historical backtest, LEGACY source, Turtle Mini, BHAP, or persistent wallet file was changed. No commit or push was made.

## Remaining risk and disposition

The latency numbers above are isolated localhost measurements, not a new live-launchd acceptance. Machine load and macOS scheduling can change tail latency. Research work that exceeds 20 seconds now fails closed and may require a later dashboard refresh to consume a completed background result. This preserves monitor responsiveness but can temporarily leave research views unavailable. The two-worker/four-job cap is intentionally small; increasing it should require a measured workload and new isolation review.

Production LaunchAgents remain disabled. A separate explicit authorization is required before another live activation.
