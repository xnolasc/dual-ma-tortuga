"""Offline forensic replay of main's original Dual MA rules; never a live executor.

Float arithmetic and original accounting quirks are confined to LEGACY so exact
historical behavior can be audited. The modern strategy, backtester and Wallet
remain independent. No requests, broker, SQLite, or legacy-file I/O is imported.
"""
import argparse
import copy
import json
from dataclasses import asdict, dataclass
from pathlib import Path

REFERENCE_COMMIT = '73b154a21eebedabe68346130f46972d3119102d'
REFERENCE_SHA256 = {
    'dualma_trader.py': '225aa7a72921b3d23e359d4033c484e03a13f3f549b09584fd6888fa821c4c12',
    'dualma_common.py': '44b93e68519b11202c0e0e7ba42a2514710255d88b13c5671a71c4274f17b441',
    'dualma_ledger.py': '805c0b3f6d7fb9f52f4f67e1d9fa1b75b115d103b24e5ecfed034c40c2321a0c',
    'dualma_dashboard.py': 'dde6df38ddfc9a471e2a645a150ddf559d4a0a2605782332231955d23c739ce6',
    'README.md': '8e81ef714cd805098f455cd2c2da8cdf1846547eb8607cd80ef103639f54c017',
}


@dataclass(frozen=True)
class LegacyConfig:
    name: str = 'LEGACY'
    tickers: tuple = ('MU', 'SNDK', 'WDC', 'BE')
    sizing_modes: tuple = ('todo', 'porcentaje', 'atr')
    short: int = 10
    long: int = 30
    atr_period: int = 14
    initial_capital: float = 1000.0
    commission_pct: float = 0.001
    minimum_notional: float = 10.0
    fixed_max_pct: float = 0.15
    risk_pct: float = 0.01
    requested_bars: int = 45
    candle_interval: str = '1d'
    source: str = 'Binance public bStock tokenized OHLC snapshots; supplied offline'
    execution: str = 'immediate_latest_returned_close; completeness_not_checked'
    numeric_policy: str = 'Python float and round(value, 2), source-compatible'
    reference_commit: str = REFERENCE_COMMIT


LEGACY = LegacyConfig()
WARNINGS = (
    'LEGACY FORENSIC REPLAY ONLY: same-candle fills and unconfirmed candles are preserved; '
    'results are not a no-lookahead backtest.',
    'Python float, fractional shares, penny overdraft tolerance and rounded-ledger drift are '
    'preserved intentionally; never use this path for the SQLite wallet or modern accounting.',
    'Original tokenized MU/SNDK/WDC/BE universe is distinct from the new nine-ticker equity universe.',
    'Offline snapshots cannot reproduce missing polling cycles, provider revisions or unsynchronized live requests.',
)


def symbol_for(ticker):
    return f'{ticker}BUSDT' if ticker in LEGACY.tickers else f'{ticker}USDT'


def compute_sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def compute_atr(klines, period=LEGACY.atr_period):
    if len(klines) < period + 1:
        return None
    ranges = []
    for index in range(1, len(klines)):
        high, low, previous = klines[index]['high'], klines[index]['low'], klines[index - 1]['close']
        ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    return sum(ranges[-period:]) / period


def empty_ledger(capital=LEGACY.initial_capital):
    return {'capital_inicial': capital, 'capital_disponible': capital, 'posiciones': {},
            'historial_eventos': [], 'comisiones_pagadas_total': 0.0}


def sizing_todo(ledger, price):
    gross = ledger['capital_disponible'] / (1 + LEGACY.commission_pct)
    shares = gross / price
    return 0.0 if gross < LEGACY.minimum_notional else shares


def sizing_porcentaje(ledger, price, max_pct=LEGACY.fixed_max_pct):
    limit = ledger['capital_inicial'] * max_pct
    available = ledger['capital_disponible'] / (1 + LEGACY.commission_pct)
    gross = min(limit, available)
    return 0.0 if gross < LEGACY.minimum_notional else gross / price


def sizing_atr(ledger, price, n_atr, risk_pct=LEGACY.risk_pct):
    if n_atr <= 0:
        return 0.0
    risk = ledger['capital_inicial'] * risk_pct
    desired_shares = risk / (2 * n_atr)
    desired_gross = desired_shares * price
    available = ledger['capital_disponible'] / (1 + LEGACY.commission_pct)
    gross = min(desired_gross, available)
    return 0.0 if gross < LEGACY.minimum_notional else gross / price


def calculate_shares(mode, ledger, price, atr):
    if mode == 'todo':
        return sizing_todo(ledger, price)
    if mode == 'porcentaje':
        return sizing_porcentaje(ledger, price, LEGACY.fixed_max_pct)
    if mode == 'atr':
        return sizing_atr(ledger, price, atr)
    raise ValueError('modo desconocido: ' + mode)


def buy(ledger, ticker, shares, price, *, clock, reason='cruce_arriba'):
    gross = shares * price
    fee = gross * LEGACY.commission_pct
    total = gross + fee
    if total > ledger['capital_disponible'] + 0.01:
        return False
    ledger['capital_disponible'] = round(ledger['capital_disponible'] - total, 2)
    ledger['comisiones_pagadas_total'] = round(ledger['comisiones_pagadas_total'] + fee, 2)
    ledger['posiciones'][ticker] = {'shares': shares, 'entry_price': price,
                                  'costo_total': round(total, 2), 'entry_time': int(clock())}
    ledger['historial_eventos'].append({'evento': 'compra', 'ticker': ticker, 'shares': shares,
                                      'price': price, 'costo_total': round(total, 2), 'reason': reason,
                                      'capital_disponible_despues': ledger['capital_disponible'],
                                      'timestamp': int(clock())})
    return True


def sell(ledger, ticker, price, *, clock, reason='cruce_abajo'):
    position = ledger['posiciones'].pop(ticker, None)
    if position is None:
        return None
    gross = position['shares'] * price
    fee = gross * LEGACY.commission_pct
    net = gross - fee
    pnl = round(net - position['costo_total'], 2)
    ledger['capital_disponible'] = round(ledger['capital_disponible'] + net, 2)
    ledger['comisiones_pagadas_total'] = round(ledger['comisiones_pagadas_total'] + fee, 2)
    ledger['historial_eventos'].append({'evento': 'venta', 'ticker': ticker,
                                      'shares': position['shares'], 'price': price,
                                      'entry_price': position['entry_price'], 'pnl_neto': pnl,
                                      'reason': reason, 'capital_disponible_despues': ledger['capital_disponible'],
                                      'timestamp': int(clock())})
    return pnl


def wipeout_total(ledger):
    return ledger['capital_disponible'] < LEGACY.minimum_notional and len(ledger['posiciones']) == 0


def process_ticker(ledger, ticker, mode, klines, *, clock, log=lambda message: None):
    """Same decisions as procesar_ticker on the same returned OHLC list/state."""
    if not klines or len(klines) < LEGACY.long + 1:
        log(ticker + ': historial insuficiente, se salta')
        return
    closes = [k['close'] for k in klines]
    price = closes[-1]
    atr = compute_atr(klines)
    short_now, long_now = compute_sma(closes, LEGACY.short), compute_sma(closes, LEGACY.long)
    short_prior, long_prior = compute_sma(closes[:-1], LEGACY.short), compute_sma(closes[:-1], LEGACY.long)
    if None in (short_now, long_now, short_prior, long_prior):
        log(ticker + ': no se pudo calcular ambos promedios todavia')
        return
    if ticker not in ledger['posiciones']:
        if short_prior <= long_prior and short_now > long_now:
            shares = calculate_shares(mode, ledger, price, atr)
            if shares > 0:
                buy(ledger, ticker, shares, price, clock=clock)
                # Original also logs a BUY when comprar returns False; retained for audit.
                log(ticker + ' [' + mode + ']: COMPRA ' + str(round(shares, 6)) + ' @ ' + str(price))
            else:
                log(ticker + ' [' + mode + ']: cruce arriba pero sin capital suficiente')
    elif short_prior >= long_prior and short_now < long_now:
        pnl = sell(ledger, ticker, price, clock=clock)
        log(ticker + ' [' + mode + ']: VENTA @ ' + str(price) + ' (P&L neto: $' + str(pnl) + ')')


def normalize_klines(rows):
    """Replicate original provider conversion, discarding timestamp/completeness fields."""
    if rows is None:
        return None
    return [{'high': float(row['high']), 'low': float(row['low']), 'close': float(row['close'])} for row in rows]


def replay(cycles, *, config=LEGACY):
    """Replay captured polling cycles, not an exchange-calendar daily backtest.

    A cycle provides an explicit integer timestamp and klines keyed by sizing
    mode then ticker. This can represent the different responses fetched by each
    of the three sequential legacy ledgers. Missing responses behave as skips.
    """
    if config != LEGACY:
        raise ValueError('LEGACY is a frozen audited profile; use modern Strategy for configurable presets')
    ledgers = {mode: empty_ledger() for mode in config.sizing_modes}
    messages = []
    for index, cycle in enumerate(cycles):
        now = cycle['timestamp']
        if type(now) is not int or now < 0:
            raise ValueError('Each snapshot cycle needs an explicit nonnegative integer timestamp')
        snapshots = cycle['klines']
        unknown_modes = set(snapshots) - set(config.sizing_modes)
        if unknown_modes:
            raise ValueError('Unknown LEGACY sizing mode in snapshots')
        for mode in config.sizing_modes:
            rows = snapshots.get(mode, {})
            if set(rows) - set(config.tickers):
                raise ValueError('LEGACY uses the original MU/SNDK/WDC/BE universe only')
            for ticker in config.tickers:
                process_ticker(ledgers[mode], ticker, mode, normalize_klines(rows.get(ticker)),
                               clock=lambda: now, log=lambda message: messages.append({'cycle': index, 'message': message}))
    return {'mode': 'LEGACY_OFFLINE_REPLAY', 'config': asdict(config), 'warnings': list(WARNINGS),
            'reference_source_sha256': dict(REFERENCE_SHA256), 'ledgers': ledgers, 'messages': messages}


def main():
    parser = argparse.ArgumentParser(description='LEGACY offline snapshot replay only; no network, wallet or scheduling')
    parser.add_argument('snapshot_file', type=Path)
    args = parser.parse_args()
    try:
        document = json.loads(args.snapshot_file.read_text())
        result = replay(document['cycles'])
        print(json.dumps(result, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f'LEGACY replay rejected: {exc}\n')


if __name__ == '__main__':
    main()
