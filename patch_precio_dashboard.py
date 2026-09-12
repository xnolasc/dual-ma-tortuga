PATH = "dualma_dashboard.py"

with open(PATH) as f:
    content = f.read()

old1 = '''import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from dualma_common import LEDGER_TODO_PATH, LEDGER_PORCENTAJE_PATH, LEDGER_ATR_PATH, TICKERS'''
new1 = '''import json
import time
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer

from dualma_common import (
    LEDGER_TODO_PATH, LEDGER_PORCENTAJE_PATH, LEDGER_ATR_PATH, TICKERS,
    symbol_for, BINANCE_BASE, TICKER_PRICE_ENDPOINT,
)'''
assert content.count(old1) == 1, "no encontrado bloque 1"
content = content.replace(old1, new1)

old2 = '''def cargar_ledger(path):'''
new2 = '''def obtener_precios():
    precios = {}
    for t in TICKERS:
        symbol = symbol_for(t)
        try:
            r = requests.get(BINANCE_BASE + TICKER_PRICE_ENDPOINT, params={"symbol": symbol}, timeout=5)
            r.raise_for_status()
            precios[t] = float(r.json()["price"])
        except Exception:
            precios[t] = None
    return precios


def cargar_ledger(path):'''
assert content.count(old2) == 1, "no encontrado bloque 2"
content = content.replace(old2, new2)

old3 = '''def build_html():
    cols_html = ""
    for nombre, path in LEDGERS_INFO:'''
new3 = '''def build_html():
    precios = obtener_precios()
    cols_html = ""
    for nombre, path in LEDGERS_INFO:'''
assert content.count(old3) == 1, "no encontrado bloque 3"
content = content.replace(old3, new3)

old4 = '''            else:
                posiciones_html += "<div class='pos esperando'>" + t + ": ESPERANDO cruce</div>"'''
new4 = '''            else:
                precio_actual = precios.get(t)
                precio_str = ("$" + str(round(precio_actual, 2))) if precio_actual else "sin precio"
                posiciones_html += "<div class='pos esperando'>" + t + ": ESPERANDO cruce (" + precio_str + ")</div>"'''
assert content.count(old4) == 1, "no encontrado bloque 4"
content = content.replace(old4, new4)

with open(PATH, "w") as f:
    f.write(content)

print("[OK] precio agregado al dashboard")
