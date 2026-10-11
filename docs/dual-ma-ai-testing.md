# Dual MA AI regression testing

Run the complete Python suite from the repository root:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The market-data tests use mocked provider responses. Wallet, backtest, and browser fixture tests use isolated temporary state; they do not update `data/dual_ma_ai/wallet.sqlite3`.

Browser checks are optional and require Node.js plus Playwright and a Chromium-compatible browser already available in the test environment. Playwright resolves as the Node module `playwright` by default. Set `DUAL_MA_PLAYWRIGHT` to a module path when using a separately managed installation, and set `DUAL_MA_CHROME` only when a specific browser executable is needed. Browser tests that capture screenshots accept output-path environment variables; generated screenshots are ignored by Git.

The isolated browser regressions are:

```sh
node tests/dashboard_browser.cjs
node tests/dashboard_browser_alpaca.cjs
node tests/dashboard_browser_health.cjs
node tests/dashboard_browser_health_replay.cjs
node tests/dashboard_browser_monitor.cjs
node tests/dashboard_browser_latency_probe.cjs
node tests/dashboard_browser_live.cjs
```

`dashboard_browser_live.cjs` reads the existing real-data cache and wallet through read-only interfaces and verifies wallet byte identity. `dashboard_browser_live_activation.cjs` targets a running dashboard and is reserved for separately authorized live acceptance; it is not part of the isolated test list above. Tests do not install dependencies or start/stop LaunchAgents.
