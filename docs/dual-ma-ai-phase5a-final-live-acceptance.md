# Phase 5A final live acceptance — 2026-10-11

**Disposition: PASS.** The authorized READ-ONLY market-data and dashboard LaunchAgents remain enabled and running. No PAPER trades, brokerage requests, wallet mutations, dependency installations, commits, or pushes occurred.

## Live service checks

- Market-data worker was activated first. Three successive completed monitoring cycles were observed at approximately one-minute intervals. Each ended idle with `in_progress=false`, health `DEGRADED`, seven validated symbols, and SPCX/AAOI blocked with no signals. Persisted monitor event IDs were unique.
- The cycle-observer helper raised a Python `TypeError` only while serializing its final summary (`flush` was passed to `json.dumps`). It had already printed all three cycle observations; those values were reconstructed in its evidence file and checked against persisted worker state. The worker itself completed normally.
- Dashboard service is the sole listener on `127.0.0.1:8895`. Both launchd services show one active instance and are enabled.
- Browser acceptance ran against the actual launchd dashboard for approximately 80 seconds. It made nine monitor polls without reloading, observed heartbeat advancement, and rendered `DEGRADED`. API requests all succeeded; no failed requests, console errors, or page errors occurred. Monitor responses reported `READ_ONLY` and `paper_execution_enabled=false`.
- SPCX and AAOI were suppressed as `UNAVAILABLE` in the live signal panel, with no strategy signals.

## Live concurrent workload

Two synchronized bursts each issued 48 `/api/monitor`, four `/api/reports`, four `/api/signals?provider=alpaca&feed=iex`, and four identical `/api/coverage?feed=iex` GET requests to the live dashboard. The second burst was run after the cache window had elapsed, exercising cold duplicate coverage coalescing.

| Run | Monitor samples | Monitor p95 | Monitor max | Research requests | Outcomes |
| --- | ---: | ---: | ---: | ---: | --- |
| Initial live burst | 48 | 693.518 ms | 704.385 ms | 12/12 | All 60 HTTP 200 |
| Post-expiry cold coverage burst | 48 | 611.629 ms | 623.884 ms | 12/12 | All 60 HTTP 200 |

For the post-expiry burst, logs show one `alpaca.coverage` computation (82.285 ms compute) serving the four identical callers. Signals and reports each ran one shared refresh for their four callers. No research request timed out or returned an error. All monitor results retained `DEGRADED` status and blocked-symbol suppression.

## Integrity and safety gates

- Wallet and audit-history SHA-256 digests matched the verified pre-activation baseline. No positions or trading activity were introduced; SQLite `integrity_check` reports `ok`.
- All 254 protected artifacts match the pre-activation manifest; zero changed and zero missing. LEGACY parity had 21 passing tests before activation, and frozen original LEGACY sources remain hash-identical. Turtle Mini and BHAP were not modified.
- The dashboard and worker remain READ-ONLY; automatic PAPER execution is disabled.

The compact browser, cycle, and raw per-request latency evidence is in [the acceptance evidence JSON](dual-ma-ai-phase5a-final-live-acceptance-evidence.json). Raw full-response captures and the browser evidence are retained under `/tmp` on this machine.

## Operational commands

Stop services:

```sh
launchctl disable gui/501/local.dual-ma-ai.dashboard
launchctl bootout gui/501/local.dual-ma-ai.dashboard
launchctl disable gui/501/local.dual-ma-ai.market-data
launchctl bootout gui/501/local.dual-ma-ai.market-data
```

Restart both (dashboard after worker):

```sh
launchctl kickstart -k gui/501/local.dual-ma-ai.market-data
launchctl kickstart -k gui/501/local.dual-ma-ai.dashboard
```

Check status and logs:

```sh
launchctl print gui/501/local.dual-ma-ai.market-data
launchctl print gui/501/local.dual-ma-ai.dashboard
curl -fsS http://127.0.0.1:8895/api/monitor
 tail -f data/dual_ma_ai/monitor/worker.log data/dual_ma_ai/monitor/dashboard-stderr.log
```
