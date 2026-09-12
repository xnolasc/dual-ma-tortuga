"""
dualma_ledger.py
Logica de un ledger individual (no pool compartido -- cada ledger tiene
su propio capital, separado de los otros dos). Sin piramide: una sola
unidad por posicion, a diferencia del Turtle.
"""
import json
import os
import time

from dualma_common import COMMISSION_PCT, MIN_NOTIONAL_USD


def empty_ledger(capital_inicial):
    return {
        "capital_inicial": capital_inicial,
        "capital_disponible": capital_inicial,
        "posiciones": {},
        "historial_eventos": [],
        "comisiones_pagadas_total": 0.0,
    }


def load_ledger(path, capital_inicial):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    ledger = empty_ledger(capital_inicial)
    save_ledger(path, ledger)
    return ledger


def save_ledger(path, ledger):
    with open(path, "w") as f:
        json.dump(ledger, f, indent=2)


def sizing_todo(ledger, price):
    disponible = ledger["capital_disponible"]
    monto_bruto = disponible / (1 + COMMISSION_PCT)
    shares = monto_bruto / price
    if monto_bruto < MIN_NOTIONAL_USD:
        return 0.0
    return shares


def sizing_porcentaje(ledger, price, max_pct=0.15):
    limite = ledger["capital_inicial"] * max_pct
    monto_max_disponible = ledger["capital_disponible"] / (1 + COMMISSION_PCT)
    monto_bruto = min(limite, monto_max_disponible)
    if monto_bruto < MIN_NOTIONAL_USD:
        return 0.0
    return monto_bruto / price


def sizing_atr(ledger, price, n_atr, risk_pct=0.01):
    if n_atr <= 0:
        return 0.0
    riesgo_usd = ledger["capital_inicial"] * risk_pct
    shares_deseado = riesgo_usd / (2 * n_atr)
    monto_bruto_deseado = shares_deseado * price
    monto_max_disponible = ledger["capital_disponible"] / (1 + COMMISSION_PCT)
    monto_bruto = min(monto_bruto_deseado, monto_max_disponible)
    if monto_bruto < MIN_NOTIONAL_USD:
        return 0.0
    return monto_bruto / price


def comprar(ledger, ticker, shares, price, reason="cruce_arriba"):
    monto_bruto = shares * price
    comision = monto_bruto * COMMISSION_PCT
    costo_total = monto_bruto + comision

    if costo_total > ledger["capital_disponible"] + 0.01:
        return False

    ledger["capital_disponible"] = round(ledger["capital_disponible"] - costo_total, 2)
    ledger["comisiones_pagadas_total"] = round(ledger["comisiones_pagadas_total"] + comision, 2)
    ledger["posiciones"][ticker] = {
        "shares": shares, "entry_price": price,
        "costo_total": round(costo_total, 2), "entry_time": int(time.time()),
    }
    ledger["historial_eventos"].append({
        "evento": "compra", "ticker": ticker, "shares": shares, "price": price,
        "costo_total": round(costo_total, 2), "reason": reason,
        "capital_disponible_despues": ledger["capital_disponible"],
        "timestamp": int(time.time()),
    })
    return True


def vender(ledger, ticker, price, reason="cruce_abajo"):
    pos = ledger["posiciones"].pop(ticker, None)
    if pos is None:
        return None

    monto_bruto = pos["shares"] * price
    comision = monto_bruto * COMMISSION_PCT
    monto_neto = monto_bruto - comision
    pnl_neto = round(monto_neto - pos["costo_total"], 2)

    ledger["capital_disponible"] = round(ledger["capital_disponible"] + monto_neto, 2)
    ledger["comisiones_pagadas_total"] = round(ledger["comisiones_pagadas_total"] + comision, 2)
    ledger["historial_eventos"].append({
        "evento": "venta", "ticker": ticker, "shares": pos["shares"], "price": price,
        "entry_price": pos["entry_price"], "pnl_neto": pnl_neto, "reason": reason,
        "capital_disponible_despues": ledger["capital_disponible"],
        "timestamp": int(time.time()),
    })
    return pnl_neto


def wipeout_total(ledger):
    return ledger["capital_disponible"] < MIN_NOTIONAL_USD and len(ledger["posiciones"]) == 0
