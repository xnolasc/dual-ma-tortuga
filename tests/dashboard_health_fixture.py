"""Isolated dashboard fixture for monitor health synchronization regressions."""
import json,tempfile,threading,time
import os
from datetime import datetime,timezone
from pathlib import Path
import dual_ma_ai.dashboard as dashboard
from dual_ma_ai.dashboard import Dashboard,make_server
from dual_ma_ai.monitor import Store,schedule
from dual_ma_ai.wallet import Wallet

if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='dual-ma-dashboard-health-') as temp:
     root=Path(temp);state=Store(root/'monitor');stop=threading.Event();info=schedule(datetime.now(timezone.utc))
     def heartbeat():
      while not stop.is_set():
       now=datetime.now(timezone.utc).isoformat()
       state.save(dict(running=True,health='DEGRADED',connection_health='CONNECTED',heartbeat=now,
          phase='idle',phase_started_at=now,last_successful_refresh=now,last_daily_refresh=now,
          last_market_update=None,next_scheduled_refresh=now,
          message='Synthetic health synchronization test; no market data or trading decisions.'))
       stop.wait(.5)
     state.save(dict(running=True,health='DEGRADED',connection_health='CONNECTED',heartbeat=datetime.now(timezone.utc).isoformat(),
      phase='startup',last_successful_refresh=datetime.now(timezone.utc).isoformat(),schedule=info,
      message='Synthetic health synchronization test; no market data or trading decisions.'))
     worker=threading.Thread(target=heartbeat,daemon=True);worker.start()
     wallet=Wallet(root/'wallet.sqlite3')
     if os.environ.get('DASHBOARD_FIXTURE_DELAY_WALLET'):
      original=dashboard.wallet_view
      def delayed_wallet_view(wallet):
       time.sleep(float(os.environ['DASHBOARD_FIXTURE_DELAY_WALLET']))
       return original(wallet)
      dashboard.wallet_view=delayed_wallet_view
     app=Dashboard(wallet,reports=root/'reports',cache=root/'alpha',alpaca_cache=root/'alpaca',monitor_root=state.root,read_only=True)
     try:
      with make_server(app,0) as server:
       print(server.server_port,flush=True)
       try:server.serve_forever()
       except KeyboardInterrupt:pass
     finally:stop.set();worker.join()
