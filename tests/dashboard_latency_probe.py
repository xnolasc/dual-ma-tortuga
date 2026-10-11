"""Read-only latency probe; monitor and wallet state are temporary copies."""
import concurrent.futures
import json
import sqlite3
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from dual_ma_ai.dashboard import Dashboard, make_server, wallet_view
from dual_ma_ai.monitor import STATE_ROOT, status as monitor_status
from dual_ma_ai.research import CACHE_ROOT, REPORT_ROOT
from dual_ma_ai.alpaca_data import CACHE_ROOT as ALPACA_ROOT
from dual_ma_ai.wallet import Wallet


def elapsed(fn):
    start = time.monotonic()
    result = fn()
    return round((time.monotonic() - start) * 1000, 2), result


with tempfile.TemporaryDirectory(prefix='dual-ma-dashboard-latency-') as tmp:
    root = Path(tmp)
    state = root / 'monitor'
    state.mkdir()
    source = (STATE_ROOT / 'monitor.sqlite3').resolve()
    if source.exists():
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(state / 'monitor.sqlite3') as dst:
            src.backup(dst)
    wallet = Wallet(root / 'wallet.sqlite3')
    app = Dashboard(wallet, reports=REPORT_ROOT, cache=CACHE_ROOT,
                    alpaca_cache=ALPACA_ROOT, monitor_root=state, read_only=True)
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'

    def get(path):
        start = time.monotonic()
        with urllib.request.urlopen(base + path, timeout=45) as response:
            body = response.read()
            return round((time.monotonic() - start) * 1000, 2), response.status, len(body)

    import dual_ma_ai.dashboard as dashboard_module
    breakdown = {'cache_latest_ms': 0.0, 'calendar_construct_ms': 0.0,
                 'calendar_sessions_ms': 0.0, 'calendar_bounds_ms': 0.0,
                 'strategy_signals_ms': 0.0, 'calendar_instances': 0,
                 'strategy_calls': 0}
    original_latest = app.alpaca.latest
    def timed_latest(*args, **kwargs):
        start = time.monotonic()
        try: return original_latest(*args, **kwargs)
        finally: breakdown['cache_latest_ms'] += (time.monotonic() - start) * 1000
    app.alpaca.latest = timed_latest
    original_signals = dashboard_module.signals
    def timed_signals(*args, **kwargs):
        start = time.monotonic(); breakdown['strategy_calls'] += 1
        try: return original_signals(*args, **kwargs)
        finally: breakdown['strategy_signals_ms'] += (time.monotonic() - start) * 1000
    dashboard_module.signals = timed_signals
    original_calendar = dashboard_module.USCalendar
    class TimedCalendar:
        def __init__(self, *args, **kwargs):
            start = time.monotonic(); self._calendar = original_calendar(*args, **kwargs)
            breakdown['calendar_construct_ms'] += (time.monotonic() - start) * 1000
            breakdown['calendar_instances'] += 1
        def sessions(self, *args, **kwargs):
            start = time.monotonic()
            try: return self._calendar.sessions(*args, **kwargs)
            finally: breakdown['calendar_sessions_ms'] += (time.monotonic() - start) * 1000
        def bounds(self, *args, **kwargs):
            start = time.monotonic()
            try: return self._calendar.bounds(*args, **kwargs)
            finally: breakdown['calendar_bounds_ms'] += (time.monotonic() - start) * 1000
        def __getattr__(self, name): return getattr(self._calendar, name)
    dashboard_module.USCalendar = TimedCalendar
    direct = {}
    for name, fn in (
        ('monitor_status', lambda: monitor_status(state)),
        ('wallet_view', lambda: wallet_view(wallet)),
        ('catalog', app.catalog),
        ('signal_panel', lambda: app.signal_panel('alpaca', 'iex')),
    ):
        direct[name] = elapsed(fn)[0]
    direct['signal_panel_breakdown_ms'] = dict(breakdown)

    paths = ['/api/session', '/api/wallet', '/api/monitor', '/api/reports', '/api/signals?provider=alpaca&feed=iex']
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        concurrent_results = list(pool.map(get, paths * 2))

    # Simulate genuinely slow endpoint work without tying up or writing state.
    original_catalog = app.catalog
    original_wallet_view = __import__('dual_ma_ai.dashboard', fromlist=['wallet_view']).wallet_view
    import dual_ma_ai.dashboard as dashboard_module
    def slow_catalog():
        time.sleep(3)
        return original_catalog()
    def slow_wallet_view(value):
        time.sleep(3)
        return original_wallet_view(value)
    app.catalog = slow_catalog
    dashboard_module.wallet_view = slow_wallet_view
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        slow = [pool.submit(get, '/api/reports'), pool.submit(get, '/api/wallet')]
        time.sleep(.1)
        monitor_during_slow = get('/api/monitor')
        slow_results = [f.result() for f in slow]

    print(json.dumps({'isolated': True, 'direct_stage_ms': direct,
                      'concurrent_endpoints_ms_status_bytes': concurrent_results,
                      'slow_endpoint_results': slow_results,
                      'monitor_while_reports_wallet_slow_ms_status_bytes': monitor_during_slow}, indent=2))
    server.shutdown()
    server.server_close()
    thread.join()
