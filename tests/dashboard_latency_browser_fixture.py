"""Isolated dashboard server using temporary wallet/monitor state and read-only research caches."""
import sqlite3
import tempfile
from pathlib import Path

from dual_ma_ai.alpaca_data import CACHE_ROOT as ALPACA_ROOT
from dual_ma_ai.dashboard import Dashboard, make_server
from dual_ma_ai.monitor import STATE_ROOT
from dual_ma_ai.research import CACHE_ROOT, REPORT_ROOT
from dual_ma_ai.wallet import Wallet

if __name__ == '__main__':
    _temporary = tempfile.TemporaryDirectory(prefix='dual-ma-latency-browser-')
    _root = Path(_temporary.name)
    _monitor = _root / 'monitor'
    _monitor.mkdir()
    _source = STATE_ROOT / 'monitor.sqlite3'
    if _source.is_file():
        with sqlite3.connect(_source.resolve().as_uri() + '?mode=ro', uri=True) as src, \
             sqlite3.connect(_monitor / 'monitor.sqlite3') as dst:
            src.backup(dst)
    _app = Dashboard(Wallet(_root / 'wallet.sqlite3'), reports=REPORT_ROOT,
                     cache=CACHE_ROOT, alpaca_cache=ALPACA_ROOT,
                     monitor_root=_monitor, read_only=True)
    _server = make_server(_app, 0)
    print(_server.server_port, flush=True)
    try:
        try:_server.serve_forever()
        except KeyboardInterrupt:pass
    finally:
        _server.server_close()
        _temporary.cleanup()
