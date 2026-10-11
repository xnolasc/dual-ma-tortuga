"""Temporary alert dashboard fixture; creates no repository data or live monitor changes."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import tempfile
from datetime import datetime, timezone

from dual_ma_ai.alert_evaluator import AlertEvaluator
from dual_ma_ai.alert_policy import PolicySet
from dual_ma_ai.alert_store import AlertStore
import dual_ma_ai.dashboard as dashboard_module
from dual_ma_ai.dashboard import Dashboard, make_server
from dual_ma_ai.wallet import Wallet
from test_alert_dashboard import current_snapshot


with tempfile.TemporaryDirectory(prefix='dual-ma-alert-browser-') as tmp:
    root=Path(tmp);now=datetime.now(timezone.utc);snapshot=current_snapshot(now)
    dashboard_module.monitor_status=lambda _root,_now=None:snapshot
    store=AlertStore(root/'alerts'/'alerts.sqlite3')
    evaluator=AlertEvaluator(store,root/'monitor',policies=PolicySet.load(),
        source_reader=lambda _root,_now:snapshot)
    evaluator.tick(now=now)
    app=Dashboard(Wallet(root/'wallet.sqlite3'),reports=root/'reports',cache=root/'cache',
        alpaca_cache=root/'alpaca',monitor_root=root/'monitor',read_only=True,alert_store=store)
    # This suite targets the alert panel; deterministic empty research fixtures
    # prevent unrelated cold-start process-pool work from affecting its results.
    app.cached_catalog=lambda:dict(reports=[],warnings=[])
    app.cached_signal_panel=lambda provider='alpaca',feed='iex':dict(rows=[],warnings=[],as_of=now.isoformat(),warning='Synthetic alert-browser fixture.')
    app.cached_coverage=lambda feed:dict(rows=[])
    with make_server(app,0) as server:
        print(server.server_port,flush=True)
        server.serve_forever()
