"""Browser-test server with mocked Alpaca responses, isolated caches/wallet/report."""
import json
import tempfile
from datetime import date,datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit,parse_qs

from dual_ma_ai.alpaca_data import AlpacaCache,Client,Credentials,fetch
from dual_ma_ai.market_data import USCalendar,digest
from dual_ma_ai.backtest import compare,Execution
from dual_ma_ai.dashboard import Dashboard,make_server
from dual_ma_ai.wallet import Wallet

class Response:
    def __init__(self,payload):self.raw=json.dumps(payload).encode()
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,size):return self.raw[:size]

def main():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);cal=USCalendar(date(2015,1,1),date(2026,10,20))
        days=cal.sessions(date(2024,1,2),date(2026,10,9))
        raw,split=[],[]
        for index,day in enumerate(days):
            phase=index%180;value=100+(phase if phase<90 else 180-phase)
            factor=2 if index<200 else 1
            instant=datetime.combine(day,datetime.min.time(),ZoneInfo('America/New_York')).astimezone(timezone.utc).isoformat()
            raw.append(dict(t=instant,o=str(value*factor),h=str(value*factor),l=str(value*factor),c=str(value*factor),v=10000))
            split.append(dict(t=instant,o=str(value),h=str(value),l=str(value),c=str(value),v=10000*factor))
        def opener(req,timeout):
            query=parse_qs(urlsplit(req.full_url).query)
            return Response(dict(bars={'MU':raw if query['adjustment']==['raw'] else split},next_page_token=None))
        cache=AlpacaCache(root/'alpaca')
        client=Client(Credentials('mock-key','mock-secret'),opener=opener)
        path=fetch(client,cache,'MU',days[0],days[-1],'iex',now=datetime(2026,10,10,12,tzinfo=timezone.utc),calendar=cal)
        ds,_=cache.load(path,cal)
        report=compare({'MU':ds},days[-1],Execution(),calendar=cal,as_of=datetime(2026,10,10,12,tzinfo=timezone.utc))
        report.update(evaluation_end=str(days[-1]),synthetic=False,feed='iex')
        (root/'reports').mkdir();(root/'reports'/f'{digest(report)}.json').write_text(json.dumps(report))
        app=Dashboard(Wallet(root/'wallet.sqlite3'),reports=root/'reports',cache=root/'alpha',alpaca_cache=cache.root)
        with make_server(app,0) as server:
            print(server.server_port,flush=True)
            try:server.serve_forever()
            except KeyboardInterrupt:pass


if __name__ == '__main__':
    main()
