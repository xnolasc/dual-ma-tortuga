"""Isolated dashboard server using fully temporary synthetic state."""
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from dual_ma_ai.alpaca_data import CACHE_ROOT as ALPACA_ROOT
from dual_ma_ai.dashboard import Dashboard, make_server
from dual_ma_ai.alert_store import AlertStore
from dual_ma_ai.monitor import Store, schedule
from dual_ma_ai.research import CACHE_ROOT, REPORT_ROOT
from dual_ma_ai.wallet import Wallet

if __name__ == '__main__':
    _temporary = tempfile.TemporaryDirectory(prefix='dual-ma-latency-browser-')
    _root = Path(_temporary.name)
    _monitor = _root / 'monitor'
    _monitor.mkdir()
    _now=datetime.now(timezone.utc)
    Store(_monitor).save(dict(running=False,health='STOPPED',connection_health='NOT CONNECTED',
        heartbeat=None,phase='idle',schedule=schedule(_now),daily=[],quotes=[],
        message='Synthetic monitor latency fixture; no market data.'))
    _app = Dashboard(Wallet(_root / 'wallet.sqlite3'), reports=REPORT_ROOT,
                     cache=CACHE_ROOT, alpaca_cache=ALPACA_ROOT,
                     monitor_root=_monitor, read_only=True,
                     alert_store=AlertStore(_root / 'alerts.sqlite3'))
    _app.cached_catalog=lambda:dict(reports=[],warnings=[])
    _app.cached_signal_panel=lambda provider='alpaca',feed='iex':dict(rows=[],warnings=[],as_of='synthetic',warning='Synthetic monitor-only fixture.')
    _app.cached_coverage=lambda feed:dict(rows=[])
    _server = make_server(_app, 0)
    print(_server.server_port, flush=True)
    try:
        try:_server.serve_forever()
        except KeyboardInterrupt:pass
    finally:
        _server.server_close()
        _temporary.cleanup()
