"""
dualma_trader.py
Detecta el cruce de los promedios moviles (10/30 dias) y ejecuta
compra/venta en los 3 ledgers en paralelo, cada uno con su propia
regla de tamano de posicion.
"""
import time
import requests

from dualma_common import (
    TICKERS, symbol_for, PROMEDIO_CORTO_DIAS, PROMEDIO_LARGO_DIAS,
    LEDGER_TODO_PATH, LEDGER_PORCENTAJE_PATH, LEDGER_ATR_PATH,
    CAPITAL_INICIAL_USD, PORCENTAJE_FIJO_MAX, TRADER_LOG,
    BINANCE_BASE, KLINES_ENDPOINT,
)
from dualma_ledger import (
    load_ledger, save_ledger, sizing_todo, sizing_porcentaje, sizing_atr,
    comprar, vender,
)


def log(msg):
    line = "[" + time.strftime("%Y-%m-%d %H:%M:%S") + "] " + msg
    print(line)
    with open(TRADER_LOG, "a") as f:
        f.write(line + "\n")


def get_daily_klines(symbol, limit):
    try:
        r = requests.get(BINANCE_BASE + KLINES_ENDPOINT, params={
            "symbol": symbol, "interval": "1d", "limit": limit,
        }, timeout=10)
        r.raise_for_status()
        data = r.json()
        return [{"high": float(k[2]), "low": float(k[3]), "close": float(k[4])} for k in data]
    except Exception as e:
        log(symbol + ": error trayendo klines (" + str(e) + ")")
        return None


def compute_sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def compute_atr(klines, period=14):
    if len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        h, l, pc = klines[i]["high"], klines[i]["low"], klines[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


def calcular_shares(modo, ledger, price, n_atr):
    if modo == "todo":
        return sizing_todo(ledger, price)
    if modo == "porcentaje":
        return sizing_porcentaje(ledger, price, PORCENTAJE_FIJO_MAX)
    if modo == "atr":
        return sizing_atr(ledger, price, n_atr)
    raise ValueError("modo desconocido: " + modo)


def procesar_ticker(ledger, ticker, modo):
    symbol = symbol_for(ticker)
    needed = PROMEDIO_LARGO_DIAS + 15
    klines = get_daily_klines(symbol, needed)
    if not klines or len(klines) < PROMEDIO_LARGO_DIAS + 1:
        log(ticker + ": historial insuficiente, se salta")
        return

    closes = [k["close"] for k in klines]
    price = closes[-1]
    n_atr = compute_atr(klines)

    corto_hoy = compute_sma(closes, PROMEDIO_CORTO_DIAS)
    largo_hoy = compute_sma(closes, PROMEDIO_LARGO_DIAS)
    corto_ayer = compute_sma(closes[:-1], PROMEDIO_CORTO_DIAS)
    largo_ayer = compute_sma(closes[:-1], PROMEDIO_LARGO_DIAS)

    if None in (corto_hoy, largo_hoy, corto_ayer, largo_ayer):
        log(ticker + ": no se pudo calcular ambos promedios todavia")
        return

    en_posicion = ticker in ledger["posiciones"]

    if not en_posicion:
        cruzo_arriba = corto_ayer <= largo_ayer and corto_hoy > largo_hoy
        if cruzo_arriba:
            shares = calcular_shares(modo, ledger, price, n_atr)
            if shares > 0:
                comprar(ledger, ticker, shares, price)
                log(ticker + " [" + modo + "]: COMPRA " + str(round(shares, 6)) + " @ " + str(price))
            else:
                log(ticker + " [" + modo + "]: cruce arriba pero sin capital suficiente")
    else:
        cruzo_abajo = corto_ayer >= largo_ayer and corto_hoy < largo_hoy
        if cruzo_abajo:
            pnl = vender(ledger, ticker, price)
            log(ticker + " [" + modo + "]: VENTA @ " + str(price) + " (P&L neto: $" + str(pnl) + ")")


def correr_ledger(path, modo):
    ledger = load_ledger(path, CAPITAL_INICIAL_USD)
    for ticker in TICKERS:
        procesar_ticker(ledger, ticker, modo)
    save_ledger(path, ledger)


def main():
    log("--- ciclo dualma_trader ---")
    correr_ledger(LEDGER_TODO_PATH, "todo")
    correr_ledger(LEDGER_PORCENTAJE_PATH, "porcentaje")
    correr_ledger(LEDGER_ATR_PATH, "atr")
    log("--- fin del ciclo ---")


if __name__ == "__main__":
    main()
