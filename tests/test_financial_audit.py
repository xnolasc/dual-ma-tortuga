"""Independent oracle tests. Synthetic data, no credentials/network/wallet state."""
import copy
import unittest
from dataclasses import replace
from fractions import Fraction
from decimal import Decimal
from unittest.mock import patch

from dual_ma_ai.financial_audit import audit_run, independent_metrics, independent_signals
from dual_ma_ai.backtest import Execution, run_backtest
from dual_ma_ai.strategy import Strategy
from test_research import dataset, CAL, NOW


class FinancialAuditTests(unittest.TestCase):
    def run_case(self, prices, start=5, execution=None, strategy=None):
        ds=dataset(prices)
        run=run_backtest({'MU':ds},strategy or Strategy('SMA',1,2),ds.bars[start].day,ds.bars[-1].day,
                         execution or Execution(warmup=2),calendar=CAL,as_of=NOW)
        audit=audit_run(run,ds)
        self.assertEqual(audit['issues'],[])
        return ds,run,audit

    def test_bullish_regime_does_not_imply_initial_buy(self):
        _,run,audit=self.run_case([10]*5+[12,14,16,18,20],start=7)
        self.assertEqual(run['fills'],[])
        self.assertEqual(audit['signals'],[])
        self.assertEqual(audit['metrics']['total_return'],'0.00000000')
        self.assertIsNone(audit['metrics']['sharpe'])

    def test_zero_fills_can_have_buy_signals_above_allocation(self):
        _,run,audit=self.run_case([200]*5+[400,450,500,550])
        self.assertEqual(run['fills'],[])
        self.assertEqual(audit['opportunities'][0]['outcome'],'insufficient_budget')
        self.assertEqual(audit['opportunities'][0]['budget'],'200.00')
        self.assertEqual(audit['opportunities'][0]['one_share_cost'],'450.68')

    def test_one_share_fee_can_make_exact_price_budget_unaffordable(self):
        _,run,audit=self.run_case([100]*5+[150,200,210])
        self.assertEqual(run['fills'],[])
        self.assertEqual(audit['opportunities'][0]['affordable_shares'],0)

    def test_rejected_buy_is_not_retried_without_a_new_crossover(self):
        _,run,audit=self.run_case([200]*5+[400,450,180,150])
        self.assertEqual(run['fills'],[])
        self.assertEqual([o['outcome'] for o in audit['opportunities']],['insufficient_budget','ignored_position_state'])

    def test_entry_commission_and_slippage_are_in_unrealized_basis(self):
        _,run,audit=self.run_case([10]*5+[12,14,16,30])
        entry=audit['journal'][2]
        self.assertEqual(entry['market_value'],'196.00')
        self.assertEqual(entry['realized_pnl'],'0.00')
        self.assertEqual(entry['unrealized_pnl'],'-0.34')
        self.assertEqual(sum(Decimal(p['equity_change']) for p in audit['journal']),Decimal(audit['final']['equity'])-Decimal('2000'))

    def test_boundary_crossover_executes_on_first_window_bar(self):
        ds,run,audit=self.run_case([10]*5+[12,14,16,18],start=6)
        self.assertEqual(run['fills'][0]['signal_day'],str(ds.bars[5].day))
        self.assertEqual(run['fills'][0]['fill_day'],str(ds.bars[6].day))
        self.assertEqual(audit['status'],'PASS')

    def test_one_open_fill_zero_closed_trades_can_have_positive_return(self):
        _,run,audit=self.run_case([10]*5+[12,14,16,30])
        self.assertEqual(audit['metrics']['number_of_fills'],1)
        self.assertEqual(audit['metrics']['number_of_trades'],0)
        self.assertEqual(audit['final']['realized_pnl'],'0.00')
        self.assertGreater(Decimal(audit['final']['unrealized_pnl']),0)
        self.assertIsNone(audit['metrics']['profit_factor'])

    def test_losing_closed_trades_and_winning_open_position_reconcile(self):
        _,run,audit=self.run_case([10]*5+[12,14,10,8,12,14,40,50])
        self.assertLess(Decimal(audit['final']['realized_pnl']),0)
        self.assertGreater(Decimal(audit['final']['unrealized_pnl']),0)
        self.assertGreater(Decimal(audit['metrics']['total_return']),0)
        self.assertEqual(audit['metrics']['profit_factor'],'0.00000000')
        self.assertEqual(audit['metrics']['win_rate'],'0.00000000')

    def test_mark_is_raw_close_not_sell_slippage_or_liquidation_fee(self):
        ds,run,audit=self.run_case([10]*5+[12,14,16,30])
        shares=audit['final']['positions']['MU']
        self.assertEqual(audit['final']['market_value'],f'{shares*30:.2f}')
        self.assertGreater(sum(Decimal(f['commission']) for f in run['fills']),0)

    def test_price_fee_curve_and_trade_tampering_are_detected(self):
        ds,run,_=self.run_case([10]*5+[12,14,10,8,12,14,40,50])
        for mutation in ('price','commission','curve','trade'):
            bad=copy.deepcopy(run)
            if mutation in ('price','commission'):bad['fills'][0][mutation]='0.01'
            elif mutation=='curve':bad['equity_curve'][-1]['equity']='9999.00'
            else:bad['closed_trades'][0]['pnl']='999.00'
            with self.subTest(mutation=mutation):self.assertEqual(audit_run(bad,ds)['status'],'FAIL')

    def test_missing_execution_is_detected_independently(self):
        ds,run,_=self.run_case([10]*5+[12,14,16,30])
        bad=copy.deepcopy(run);bad['fills']=[]
        self.assertEqual(audit_run(bad,ds)['status'],'FAIL')

    def test_split_basis_and_quantity_preserve_equity(self):
        ds=dataset([10]*5+[12,14,16,8,9])
        bars=list(ds.bars);bars[8]=replace(bars[8],split=Fraction(2));ds=replace(ds,bars=tuple(bars))
        run=run_backtest({'MU':ds},Strategy('SMA',1,2),ds.bars[5].day,ds.bars[-1].day,Execution(warmup=2),calendar=CAL,as_of=NOW)
        audit=audit_run(run,ds)
        self.assertEqual(audit['issues'],[])
        self.assertEqual(audit['journal'][8-4]['positions']['MU'],run['fills'][0]['quantity']*2)

    def test_independent_oracle_does_not_call_engine_helpers(self):
        ds,run,_=self.run_case([10]*5+[12,14,16,30])
        with patch('dual_ma_ai.backtest.metrics',side_effect=AssertionError('Not independent')),patch('dual_ma_ai.strategy.signals',side_effect=AssertionError('Not independent')):
            self.assertEqual(audit_run(run,ds)['status'],'PASS')

    def test_ema_future_split_does_not_change_prefix(self):
        ds=dataset([10]*10+[12,14,16,18])
        config=dict(kind='EMA',short=2,long=3)
        before=independent_signals(ds.bars[:12],config)[0]
        changed=ds.bars[:12]+tuple(replace(b,open=b.open/2,high=b.high/2,low=b.low/2,close=b.close/2,split=Fraction(2) if i==0 else Fraction(1)) for i,b in enumerate(ds.bars[12:]))
        self.assertEqual(before,[s for s in independent_signals(changed,config)[0] if s['day']<=str(ds.bars[11].day)])
