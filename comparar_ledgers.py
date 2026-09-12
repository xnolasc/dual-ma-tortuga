"""
comparar_ledgers.py
Genera el reporte comparativo de los 3 ledgers con las 6 metricas.
Imprime en pantalla Y guarda un archivo reporte_YYYY-MM-DD.md
(Opcion B -- arma historial de reportes con el tiempo).
"""
import os
import time
import requests

from dualma_common import (
    TICKERS, symbol_for, BASE_DIR, TRADER_LOG,
    LEDGER_TODO_PATH, LEDGER_PORCENTAJE_PATH, LEDGER_ATR_PATH,
    BINANCE_BASE, TICKER_PRICE_ENDPOINT, MIN_NOTIONAL_USD,
)
from dualma_ledger import wipeout_total


def cargar_ledger(path):
    if not os.path.exists(path):
        return None
    import json
    with open(path) as f:
        return json.load(f)


def obtener_precios_actuales():
    precios = {}
    for t in TICKERS:
        symbol = symbol_for(t)
        try:
            r = requests.get(BINANCE_BASE + TICKER_PRICE_ENDPOINT, params={"symbol": symbol}, timeout=10)
            r.raise_for_status()
            precios[t] = float(r.json()["price"])
        except Exception:
            precios[t] = None
    return precios


def valor_posiciones_abiertas(ledger, precios):
    total = 0.0
    for t, pos in ledger["posiciones"].items():
        p = precios.get(t)
        if p is None:
            continue
        total += pos["shares"] * p
    return total


def calcular_operaciones(ledger):
    ventas = [e for e in ledger["historial_eventos"] if e["evento"] == "venta"]
    total = len(ventas)
    ganadoras = len([e for e in ventas if e["pnl_neto"] > 0])
    win_rate = (ganadoras / total * 100) if total > 0 else None
    return total, win_rate


def calcular_drawdown(ledger):
    curva = [ledger["capital_inicial"]] + [e["capital_disponible_despues"] for e in ledger["historial_eventos"]]
    pico = curva[0]
    max_dd = 0.0
    for v in curva:
        if v > pico:
            pico = v
        dd = (pico - v) / pico * 100 if pico > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def contar_ciclos_sin_capital(modo):
    if not os.path.exists(TRADER_LOG):
        return 0
    marcador = "[" + modo + "]: cruce arriba pero sin capital suficiente"
    count = 0
    with open(TRADER_LOG) as f:
        for line in f:
            if marcador in line:
                count += 1
    return count


def fecha_wipeout(ledger):
    if not wipeout_total(ledger) or not ledger["historial_eventos"]:
        return None
    ts = ledger["historial_eventos"][-1]["timestamp"]
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def generar_filas():
    precios = obtener_precios_actuales()
    ledgers_info = [
        ("Ledger 1: Todo", "todo", LEDGER_TODO_PATH),
        ("Ledger 2: % fijo", "porcentaje", LEDGER_PORCENTAJE_PATH),
        ("Ledger 3: ATR", "atr", LEDGER_ATR_PATH),
    ]
    filas = []
    for nombre, modo, path in ledgers_info:
        l = cargar_ledger(path)
        if l is None:
            filas.append({"nombre": nombre, "sin_datos": True})
            continue

        valor_pos = valor_posiciones_abiertas(l, precios)
        valor_total = l["capital_disponible"] + valor_pos
        retorno_pct = (valor_total - l["capital_inicial"]) / l["capital_inicial"] * 100
        total_ops, win_rate = calcular_operaciones(l)
        dd = calcular_drawdown(l)
        wipeout = wipeout_total(l)

        filas.append({
            "nombre": nombre, "sin_datos": False,
            "retorno_pct": retorno_pct, "valor_total": valor_total,
            "total_ops": total_ops, "win_rate": win_rate,
            "drawdown": dd, "comisiones": l["comisiones_pagadas_total"],
            "sin_capital": contar_ciclos_sin_capital(modo),
            "wipeout": wipeout,
            "fecha_wipeout": fecha_wipeout(l) if wipeout else None,
        })
    return filas


def formatear_tabla(filas):
    lineas = []
    lineas.append("| Metrica | " + " | ".join(f["nombre"] for f in filas) + " |")
    lineas.append("|---|" + "---|" * len(filas))

    def fila(label, key, fmt):
        vals = []
        for f in filas:
            if f.get("sin_datos"):
                vals.append("sin datos")
                continue
            v = f[key]
            vals.append(fmt(v) if v is not None else "N/A")
        return "| " + label + " | " + " | ".join(vals) + " |"

    lineas.append(fila("Retorno total", "retorno_pct", lambda v: "{:+.2f}%".format(v)))
    lineas.append(fila("Valor total", "valor_total", lambda v: "${:.2f}".format(v)))
    lineas.append(fila("Operaciones", "total_ops", lambda v: str(v)))
    lineas.append(fila("Win rate", "win_rate", lambda v: "{:.1f}%".format(v) if v is not None else "sin ops"))
    lineas.append(fila("Maxima caida", "drawdown", lambda v: "-{:.2f}%".format(v)))
    lineas.append(fila("Comisiones", "comisiones", lambda v: "${:.2f}".format(v)))
    lineas.append(fila("Ciclos sin capital", "sin_capital", lambda v: str(v)))
    vals_wipeout = []
    for f in filas:
        if f.get("sin_datos"):
            vals_wipeout.append("sin datos")
        elif f["wipeout"]:
            vals_wipeout.append("SI (" + str(f["fecha_wipeout"]) + ")")
        else:
            vals_wipeout.append("No")
    lineas.append("| Wipeout total | " + " | ".join(vals_wipeout) + " |")
    return "\n".join(lineas)


def main():
    filas = generar_filas()
    tabla = formatear_tabla(filas)
    fecha = time.strftime("%Y-%m-%d")

    cuerpo = (
        "# Reporte Dual Moving Average - " + fecha + "\n\n" +
        tabla + "\n\n" +
        "*Nota: la maxima caida es una aproximacion basada en los puntos de venta "
        "registrados (capital_disponible_despues de cada evento), no en el valor "
        "mark-to-market continuo de las posiciones abiertas.*\n"
    )

    print(cuerpo)

    ruta = os.path.join(BASE_DIR, "reporte_" + fecha + ".md")
    with open(ruta, "w") as f:
        f.write(cuerpo)
    print("\nGuardado en: " + ruta)


if __name__ == "__main__":
    main()
