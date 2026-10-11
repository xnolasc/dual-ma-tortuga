"""Synthetic read-only monitor UI fixture; never Alpaca/wallet runtime files."""
import json
from pathlib import Path
import tempfile
import threading
import time
from datetime import datetime,timezone
from dual_ma_ai.dashboard import Dashboard,make_server
from dual_ma_ai.alert_store import AlertStore
from dual_ma_ai.monitor import Store,schedule
from dual_ma_ai.alpaca_data import daily_ready_at
from dual_ma_ai.wallet import Wallet

if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);store=Store(root/'monitor');stop=threading.Event()
        def updates():
            count=0
            while not stop.is_set():
                now=datetime.now(timezone.utc);count+=1;day=schedule(now)['expected_candle']
                store.save(dict(running=True,health='RUNNING',connection_health='MOCKED TEST',heartbeat=now.isoformat(),
                    last_market_update=now.isoformat(),last_successful_refresh=now.isoformat(),next_scheduled_refresh=now.isoformat(),
                    message='SYNTHETIC browser fixture; no live-data claim.',
                    quotes=[dict(ticker='MU',bid=str(100+count),ask=str(101+count),timestamp=now.isoformat(),status='VALIDATED')],
                    schedule=schedule(now),
                    daily=[dict(ticker='MU',status='VALIDATED',candle=day,signals=[dict(strategy=s,signal='NO CROSS',candle=day,usable_at=now.isoformat(),evaluated_at=now.isoformat(),explanation='Synthetic completed-candle fixture, read only.') for s in ('SMA_50_200','SMA_20_50','EMA_20_50')])]))
                stop.wait(1)
        thread=threading.Thread(target=updates,daemon=True);thread.start()
        app=Dashboard(Wallet(root/'wallet.sqlite3'),reports=root/'reports',cache=root/'alpha',alpaca_cache=root/'alpaca',monitor_root=store.root,read_only=True,alert_store=AlertStore(root/'alerts.sqlite3'))
        app.cached_catalog=lambda:dict(reports=[],warnings=[])
        app.cached_signal_panel=lambda provider='alpaca',feed='iex':dict(rows=[],warnings=[],as_of='synthetic',warning='Synthetic monitor-only fixture.')
        app.cached_coverage=lambda feed:dict(rows=[])
        try:
            with make_server(app,0) as server:
                print(server.server_port,flush=True)
                try:server.serve_forever()
                except KeyboardInterrupt:pass
        finally:stop.set();thread.join()
