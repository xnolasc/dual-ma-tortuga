"""Independent, offline verification of modern research; no wallet or execution authority.

Uses Decimal journal arithmetic and an independently implemented MA recurrence.
It does not call backtest/strategy calculations. Inputs are immutable report/cache
snapshots; output is a reconstructed daily journal and explicit discrepancies.
"""
from datetime import date, datetime
from decimal import Decimal, localcontext, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR

CENT = Decimal('.01')


def decimal(value):
    if hasattr(value, 'numerator'):
        return Decimal(value.numerator) / Decimal(value.denominator)
    return Decimal(str(value))


def money(value):
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), '.2f')


def independent_signals(bars, config):
    """Forward split-only prices; SMA seed for EMA, 24-place half-up recurrence."""
    with localcontext() as ctx:
        ctx.prec = 80
        prices, factor = [], Decimal(1)
        for i, bar in enumerate(bars):
            if bar.dividend:
                raise ValueError('This independent audit accepts price-return inputs only')
            if i:
                factor *= decimal(bar.split)
            prices.append(decimal(bar.close) * factor)
        averages = []
        for period in (config['short'], config['long']):
            values, previous = [], None
            for i, price in enumerate(prices):
                if i+1 < period:
                    values.append(None)
                    continue
                if config['kind'] == 'SMA' or i+1 == period:
                    value = sum(prices[i+1-period:i+1]) / period
                else:
                    alpha = Decimal(2) / (period+1)
                    value = alpha*price + (1-alpha)*previous
                previous = value.quantize(Decimal('1e-24'), rounding=ROUND_HALF_UP) if config['kind']=='EMA' else value
                values.append(previous)
            averages.append(values)
        short, long = averages
        crosses, spreads = [], []
        for i in range(config['long'],len(bars)):
            old, current = short[i-1]-long[i-1], short[i]-long[i]
            side = 'BUY' if old <= 0 < current else 'SELL' if old >= 0 > current else None
            spreads.append(dict(day=str(bars[i].day),short=str(short[i]),long=str(long[i]),spread=str(current)))
            if side:
                crosses.append(dict(day=str(bars[i].day),side=side,usable_at=bars[i].usable_at.isoformat()))
        return crosses, spreads


def independent_metrics(journal, trades, fills):
    with localcontext() as ctx:
        ctx.prec=60
        values=[Decimal(p['equity']) for p in journal]
        returns=[values[i]/values[i-1]-1 for i in range(1,len(values))]
        mean=sum(returns)/len(returns) if returns else Decimal(0)
        variance=(sum(r*r for r in returns)-len(returns)*mean*mean)/(len(returns)-1) if len(returns)>1 else Decimal(0)
        sharpe=mean*Decimal(252).sqrt()/variance.sqrt() if variance>0 else None
        peak=values[0];dd=Decimal(0)
        for value in values:
            peak=max(peak,value);dd=max(dd,(peak-value)/peak)
        days=(date.fromisoformat(journal[-1]['day'])-date.fromisoformat(journal[0]['day'])).days
        cagr=(values[-1]/values[0])**(Decimal('365.2425')/days)-1
        profits=[Decimal(t['pnl']) for t in trades]
        gain=sum((p for p in profits if p>0),Decimal(0));loss=-sum((p for p in profits if p<0),Decimal(0))
        out=lambda v:None if v is None else format(v.quantize(Decimal('1e-8')), 'f')
        return dict(total_return=out(values[-1]/values[0]-1),cagr=out(cagr),max_drawdown=out(dd),sharpe=out(sharpe),
                    win_rate=out(Decimal(sum(p>0 for p in profits))/len(profits)) if profits else None,
                    profit_factor=out(gain/loss) if loss else None,
                    profit_factor_status='finite' if loss else 'no_losses' if gain else 'no_closed_profit_or_loss',
                    number_of_trades=len(trades),number_of_fills=len(fills),initial_equity=money(values[0]),final_equity=money(values[-1]))


def audit_run(run, dataset):
    """MU/single-symbol journal rebuild. Reported curve/trades never drive state."""
    with localcontext() as ctx:
        ctx.prec=80
        return _audit_run(run,dataset)


def _audit_run(run,dataset):
    cfg=run['config'];settings=cfg['execution'];ticker=dataset.ticker
    if cfg['universe'] != [ticker]:
        raise ValueError('Single-symbol independent audit required')
    if any(b.dividend for b in dataset.bars):
        raise ValueError('Price-return audit excludes dividends')
    issues=[]
    check=lambda condition,message:issues.append(message) if not condition else None
    lookup={str(b.day):b for b in dataset.bars}
    days=[p['day'] for p in run['equity_curve']]
    window=[b for b in dataset.bars if cfg['start']<=str(b.day)<=cfg['end']]
    predecessors=[b for b in dataset.bars if str(b.day)<cfg['start']]
    check(bool(window) and bool(predecessors),'Missing actual window/baseline')
    check(days==[str(predecessors[-1].day)]+[str(b.day) for b in window],'Incomplete daily journal coverage')
    before=[b for b in dataset.bars if str(b.day)<days[1]]
    check(len(before)>=settings['warmup']+1,'Insufficient MA warmup')
    check(cfg['datasets'][ticker]==dataset.content_hash,'Input dataset identity mismatch')
    relevant=before[-settings['warmup']-1:]+[lookup[d] for d in days[1:]]
    signals,spreads=independent_signals(relevant,cfg['strategy'])
    signals=[s for s in signals if s['day']>=days[0]]
    spreads=[s for s in spreads if s['day']>=days[0]]
    by_signal={(s['day'],s['side']):s for s in signals}
    fills_by_day={}
    for fill in run['fills']:fills_by_day.setdefault(fill['fill_day'],[]).append(fill)
    capital=Decimal(settings['capital']);cash=capital;realized=Decimal(0);qty=0;basis=Decimal(0)
    entry=None;trades=[];journal=[];fill_journal=[];actions=[];keys=set();last_equity=capital
    fee=lambda notional:max(Decimal(settings['minimum_commission']),(notional*settings['commission_bps']/10000).quantize(CENT,rounding=ROUND_HALF_UP))
    for day in days:
        bar=lookup[day]
        if day!=days[0] and qty and bar.split!=1:
            old_qty=qty;new=Decimal(qty)*decimal(bar.split);qty=int(new);fraction=new-qty
            if fraction:
                removed=(basis*fraction/new).quantize(CENT,rounding=ROUND_HALF_UP)
                credit=(fraction*decimal(bar.open)).quantize(CENT,rounding=ROUND_HALF_UP)
                basis-=removed;cash+=credit;realized+=credit-removed
                actions.append(dict(day=day,kind='cash_in_lieu',cash=money(credit)))
            actions.append(dict(day=day,kind='split',ratio=str(bar.split)))
            if not qty:raise ValueError('Fully liquidating reverse splits require a separate audit profile')
        for fill in fills_by_day.get(day,[]):
            check(fill['key'] not in keys,'Duplicate execution key');keys.add(fill['key'])
            signal=by_signal.get((fill['signal_day'],fill['side']))
            check(signal is not None,'Fill without independently confirmed crossover')
            eligible=[d for d in days[1:] if d>fill['signal_day'] and lookup[d].volume>0]
            check(bool(eligible) and day==eligible[0],'Fill not on next eligible bar')
            check(datetime.fromisoformat(fill['signal_closed_at'])<bar.opened_at,'Fill precedes completed signal availability')
            if signal:check(fill['signal_closed_at']==signal['usable_at'],'Signal availability timestamp mismatch')
            side=fill['side'];direction=1 if side=='BUY' else -1
            price=(decimal(bar.open)*(1+Decimal(direction*settings['slippage_bps'])/10000)).quantize(CENT,rounding=ROUND_CEILING if side=='BUY' else ROUND_FLOOR)
            quantity=fill['quantity'];charge=fee(price*quantity)
            check(type(quantity) is int and quantity>0,'Invalid whole-share quantity')
            check(Decimal(fill['price'])==price,'Incorrect slippage/adverse rounding')
            check(Decimal(fill['commission'])==charge,'Incorrect commission')
            if side=='BUY':
                budget=min(cash,(last_equity*settings['allocation_bps']/10000).quantize(CENT,rounding=ROUND_FLOOR))
                expected=int(budget/price)
                while expected and expected*price+fee(expected*price)>budget:expected-=1
                check(quantity==expected,'Incorrect whole-share budget sizing')
                check(qty==0,'Buy into existing long position')
                basis=quantity*price+charge;cash-=basis;qty=quantity
                entry=dict(day=day,price=money(price),shares=quantity,fee=money(charge),basis=money(basis))
                delta=-basis;pnl=Decimal(0)
            else:
                check(quantity==qty,'Exit quantity differs from held position')
                proceeds=quantity*price-charge;pnl=proceeds-basis
                cash+=proceeds;realized+=pnl;qty=0;basis=Decimal(0);delta=proceeds
                trades.append(dict(entry_day=entry['day'],exit_day=day,entry_price=entry['price'],exit_price=money(price),shares=quantity,
                                   entry_fee=entry['fee'],exit_fee=money(charge),pnl=money(pnl)))
                entry=None
            fill_journal.append(dict(**fill,raw_open=str(decimal(bar.open)),notional=money(price*quantity),cash_delta=money(delta),
                                     cash_after=money(cash),basis_after=money(basis),realized_delta=money(pnl)))
        market=(Decimal(qty)*decimal(bar.close)).quantize(CENT,rounding=ROUND_HALF_UP)
        row=dict(day=day,cash=money(cash),market_value=money(market),equity=money(cash+market),realized_pnl=money(realized),
                 unrealized_pnl=money(market-basis),net_contributions=money(capital),positions={ticker:qty} if qty else {})
        check(cash>=0 and cash+basis==capital+realized,'Cash/cost accounting invariant')
        previous=journal[-1] if journal else None
        journal.append(row);last_equity=cash+market
        reported=run['equity_curve'][len(journal)-1]
        for name,value in row.items():check(reported.get(name)==value,f'{day}: {name} differs from independent journal')
        for name in ('cash','market_value','equity','realized_pnl','unrealized_pnl'):
            row[name+'_change']=money(Decimal(row[name])-Decimal(previous[name])) if previous else '0.00'
    check([(t['entry_day'],t['exit_day'],t['pnl']) for t in trades]==[(t['entry_day'],t['exit_day'],t['pnl']) for t in run['closed_trades']], 'Closed-trade P&L mismatch')
    check(run['open_positions']==({ticker:dict(quantity=qty,cost_basis=money(basis))} if qty else {}),'Open basis/quantity mismatch')
    actual_actions=[{k:e[k] for k in ('day','kind','cash','ratio') if k in e} for e in run['events'] if e['kind'] in ('split','cash_in_lieu')]
    check(actions==actual_actions,'Corporate-action event mismatch')
    calculated=independent_metrics(journal,trades,run['fills'])
    for key,value in calculated.items():check(run['metrics'].get(key)==value,f'{key}: metric differs from independent calculation')
    opportunities=[]
    indexed={r['day']:i for i,r in enumerate(journal)}
    for signal in signals:
        eligible=[d for d in days[1:] if d>signal['day'] and lookup[d].volume>0]
        if not eligible:
            opportunities.append(dict(**signal,outcome='pending_after_window'))
            continue
        day=eligible[0];prior=journal[indexed[day]-1];bar=lookup[day]
        direction=1 if signal['side']=='BUY' else -1
        price=(decimal(bar.open)*(1+Decimal(direction*settings['slippage_bps'])/10000)).quantize(CENT,rounding=ROUND_CEILING if direction==1 else ROUND_FLOOR)
        budget=min(Decimal(prior['cash']),(Decimal(prior['equity'])*settings['allocation_bps']/10000).quantize(CENT,rounding=ROUND_FLOOR))
        shares=int(budget/price)
        while shares and shares*price+fee(shares*price)>budget:shares-=1
        held=prior['positions'].get(ticker,0)
        outcome='ignored_position_state' if (direction==1 and held) or (direction==-1 and not held) else 'insufficient_budget' if direction==1 and not shares else 'executed'
        matched=[f for f in run['fills'] if f['signal_day']==signal['day'] and f['side']==signal['side']]
        check(bool(matched)==(outcome=='executed'),f"{signal['day']}: eligible signal execution/skip mismatch")
        if outcome!='executed':
            check(any(e['day']==day and e['kind']==outcome and e.get('key','').endswith(':'+signal['day']+':'+signal['side']) for e in run['events']),f"{signal['day']}: missing skip reason")
        opportunities.append(dict(**signal,fill_day=day,raw_open=str(decimal(bar.open)),fill_price=money(price),budget=money(budget),
                                  one_share_cost=money(price+fee(price)),affordable_shares=shares,outcome=outcome))
    return dict(status='FAIL' if issues else 'PASS',issues=issues,metrics=calculated,signals=signals,spreads=spreads,
                opportunities=opportunities,journal=journal,fills=fill_journal,trades=trades,open_entry=entry,final=journal[-1],corporate_actions=actions,
                fees=money(sum((Decimal(f['commission']) for f in run['fills']),Decimal(0))))


def main():
    import argparse
    import csv
    import json
    from pathlib import Path
    from .alpaca_data import AlpacaCache
    from .market_data import strict_json, digest
    parser=argparse.ArgumentParser(description='Offline independent MU price-return financial audit; no wallet access')
    parser.add_argument('report',type=Path)
    parser.add_argument('--feed',required=True,choices=('iex','sip'))
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--csv-dir',type=Path)
    args=parser.parse_args()
    report=strict_json(args.report.read_bytes())
    if args.report.stem!=digest(report) or report.get('provider')!='alpaca' or report.get('feed')!=args.feed or report.get('synthetic'):
        parser.error('Require a content-verified actual Alpaca comparison from the explicit feed')
    cache=AlpacaCache();out=[];seed_checks=[]
    for comparison in report['comparisons']:
        for run in comparison['runs']:
            if run['config']['universe']!=['MU']:parser.error('Audit profile requires MU only')
            target=run['config']['datasets']['MU']
            dataset=next((cache.load(p)[0] for p in cache.root.glob(f'MU-{args.feed}-*.json') if cache.load(p)[0].content_hash==target),None)
            if dataset is None:parser.error('Exact referenced input snapshot unavailable')
            audited=audit_run(run,dataset)
            audited.update(years=comparison['years'],strategy=run['config']['strategy'],run_id=run['run_id'])
            out.append(audited)
            full=independent_signals(dataset.bars,run['config']['strategy'])[0]
            selected=[s for s in full if audited['journal'][0]['day']<=s['day']<=audited['journal'][-1]['day']]
            seed_checks.append(dict(years=comparison['years'],strategy=run['config']['strategy'],full_history_vs_window_seed_signals_match=selected==audited['signals']))
            if args.csv_dir:
                args.csv_dir.mkdir(parents=True,exist_ok=True)
                with (args.csv_dir/f"{run['run_id']}-journal.csv").open('w',newline='') as stream:
                    writer=csv.DictWriter(stream,fieldnames=tuple(audited['journal'][0]));writer.writeheader();writer.writerows(audited['journal'])
    result=dict(input_report=str(args.report),input_report_sha256=digest(report),feed=args.feed,
                unavailable_horizons=[dict(years=c['years'],reason=c.get('reason')) for c in report['comparisons'] if c['status']!='available'],
                full_history_seed_cross_checks=seed_checks,runs=out)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(dict(status='PASS' if out and all(a['status']=='PASS' for a in out) else 'FAIL',runs=len(out),daily_rows=sum(len(a['journal']) for a in out),output=str(args.output))))
    if not out or any(a['status']!='PASS' for a in out):raise SystemExit(1)


if __name__=='__main__':
    main()
