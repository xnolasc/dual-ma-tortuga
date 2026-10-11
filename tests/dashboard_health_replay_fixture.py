"""Temporary replay fixture built from a read-only monitor DB copy."""
import tempfile,threading
from datetime import datetime,timezone
from pathlib import Path
from dual_ma_ai.dashboard import Dashboard,make_server
from dual_ma_ai.monitor import Store,schedule
from dual_ma_ai.wallet import Wallet
from dual_ma_ai.alert_store import AlertStore
if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='dual-ma-dashboard-replay-') as temp:
     root=Path(temp);monitor=root/'monitor';monitor.mkdir();dst=monitor/'monitor.sqlite3'
     store=Store(monitor);now_dt=datetime.now(timezone.utc);now=now_dt.isoformat();info=schedule(now_dt)
     # Deterministic production-shaped payload: two blocked tickers have no signals;
     # seven validated tickers show informational NO CROSS rows.
     tickers=('SPCX','LITE','MRVL','TSM','CIEN','COHR','MU','NVDA','AAOI')
     daily=[]
     for ticker in tickers:
      if ticker in ('SPCX','AAOI'):
       daily.append(dict(ticker=ticker,status='BLOCKED',candle=None,signals=[],
        explanation='Daily data missing, stale, inconsistent or unvalidated; signals blocked.'))
      else:
       daily.append(dict(ticker=ticker,status='VALIDATED',candle='2026-10-09',
        signals=[dict(strategy=name,signal='NO CROSS',candle='2026-10-09',
         usable_at='2026-10-10T20:16:00+00:00',evaluated_at=now,
         explanation='No new crossover on the latest completed daily candle.')
         for name in ('SMA_50_200','SMA_20_50','EMA_20_50')],
        explanation='Validated completed daily candle; no new crossover.'))
     assert len(daily)==9 and all(not row['signals'] for row in daily if row['status']=='BLOCKED')
     store.save(dict(running=True,heartbeat=now,health='DEGRADED',connection_health='CONNECTED',phase='idle',phase_started_at=now,last_successful_refresh=now,last_daily_refresh=now,last_market_update=None,schedule=info,message='SYNTHETIC TEMPORARY SNAPSHOT replay; market prices are copied historic fixtures only.'))
     store.save(dict(daily=daily,quotes=[]))
     app=Dashboard(Wallet(root/'wallet.sqlite3'),reports=root/'reports',cache=root/'alpha',alpaca_cache=root/'alpaca',monitor_root=monitor,read_only=True,alert_store=AlertStore(root/'alerts.sqlite3'))
     # This test exercises monitor health rendering; keep unrelated research
     # endpoints deterministic instead of starting a cold research computation.
     app.cached_catalog=lambda:dict(reports=[],warnings=[])
     app.cached_signal_panel=lambda provider='alpaca',feed='iex':dict(rows=[],warnings=[],as_of=now,warning='Synthetic monitor-only fixture.')
     app.cached_coverage=lambda feed:dict(rows=[])
     stop=threading.Event()
     def heartbeat():
      while not stop.is_set():
       store.save(dict(heartbeat=datetime.now(timezone.utc).isoformat()))
       stop.wait(.5)
     worker=threading.Thread(target=heartbeat,daemon=True);worker.start()
     try:
      with make_server(app,0) as server:
       print(server.server_port,flush=True)
       try:server.serve_forever()
       except KeyboardInterrupt:pass
     finally:stop.set();worker.join()
