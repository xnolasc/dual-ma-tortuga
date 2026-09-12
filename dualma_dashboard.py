"""
dualma_dashboard.py
Dashboard unico mostrando los 3 ledgers (Todo / % fijo / ATR) lado a
lado. Relee los archivos JSON en cada request -- no guarda nada viejo
en memoria entre refrescos.
"""
import json
import time
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer

from dualma_common import (
    LEDGER_TODO_PATH, LEDGER_PORCENTAJE_PATH, LEDGER_ATR_PATH, TICKERS,
    symbol_for, BINANCE_BASE, TICKER_PRICE_ENDPOINT,
)

DASHBOARD_PORT = 8894
REFRESH_SECONDS = 15

LEDGERS_INFO = [
    ("Todo", LEDGER_TODO_PATH),
    ("% fijo", LEDGER_PORCENTAJE_PATH),
    ("ATR", LEDGER_ATR_PATH),
]


def obtener_precios():
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


def cargar_ledger(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def build_html():
    precios = obtener_precios()
    cols_html = ""
    for nombre, path in LEDGERS_INFO:
        l = cargar_ledger(path)
        if l is None:
            cols_html += "<div class='col'><h2>" + nombre + "</h2><p>sin datos aun</p></div>"
            continue

        posiciones_html = ""
        for t in TICKERS:
            pos = l["posiciones"].get(t)
            if pos:
                precio_actual = precios.get(t)
                if precio_actual:
                    pnl_usd = (precio_actual - pos["entry_price"]) * pos["shares"]
                    pnl_pct = (precio_actual - pos["entry_price"]) / pos["entry_price"] * 100
                    signo = "+" if pnl_usd >= 0 else ""
                    pnl_clase = "ganancia" if pnl_usd >= 0 else "perdida"
                    pnl_str = (" | ahora $" + str(round(precio_actual, 2)) +
                               " | <span class='" + pnl_clase + "'>" + signo + "$" + str(round(pnl_usd, 2)) +
                               " (" + signo + str(round(pnl_pct, 2)) + "%)</span>")
                else:
                    pnl_str = ""
                posiciones_html += (
                    "<div class='pos en-pos'>" + t + ": EN_POSICION " +
                    str(round(pos["shares"], 4)) + " @ $" + str(round(pos["entry_price"], 2)) + pnl_str + "</div>"
                )
            else:
                precio_actual = precios.get(t)
                precio_str = ("$" + str(round(precio_actual, 2))) if precio_actual else "sin precio"
                posiciones_html += "<div class='pos esperando'>" + t + ": ESPERANDO cruce (" + precio_str + ")</div>"

        wipeout = l["capital_disponible"] < 10 and len(l["posiciones"]) == 0
        wipeout_html = "<div class='wipeout'>WIPEOUT TOTAL</div>" if wipeout else ""

        cols_html += (
            "<div class='col'><h2>" + nombre + "</h2>" +
            "<div class='capital'>$" + str(round(l["capital_disponible"], 2)) + "</div>" +
            "<div class='sub'>de $" + str(round(l["capital_inicial"], 2)) + " inicial</div>" +
            "<div class='sub'>comisiones: $" + str(round(l["comisiones_pagadas_total"], 2)) + "</div>" +
            "<div class='sub'>eventos: " + str(len(l["historial_eventos"])) + "</div>" +
            wipeout_html + "<hr/>" + posiciones_html + "</div>"
        )

    html = ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta http-equiv='refresh' content='" + str(REFRESH_SECONDS) + "'>"
            "<title>Dual MA - 3 ledgers</title><style>"
            "body{background:#0f1115;color:#e8e8e8;font-family:-apple-system,sans-serif;padding:20px;}"
            "h1{font-size:20px;}.meta{color:#888;font-size:13px;margin-bottom:20px;}"
            ".row{display:flex;gap:16px;}"
            ".col{background:#1a1d24;border-radius:10px;padding:16px;flex:1;}"
            ".capital{font-size:28px;font-weight:bold;margin:8px 0 2px;}"
            ".sub{color:#999;font-size:12px;}"
            ".pos{font-size:13px;padding:4px 0;border-bottom:1px solid #2a2d35;}"
            ".pos-empty{color:#666;font-size:13px;font-style:italic;}"
            ".en-pos{color:#4ade80;}"
            ".ganancia{color:#4ade80;font-weight:bold;}"
            ".perdida{color:#f87171;font-weight:bold;}"
            ".esperando{color:#777;}"
            ".wipeout{color:#ff5555;font-weight:bold;font-size:13px;margin-top:6px;}"
            "</style></head><body>"
            "<h1>Dual Moving Average - 3 ledgers (Todo / % fijo / ATR)</h1>"
            "<div class='meta'>Ultima actualizacion: " + time.strftime("%Y-%m-%d %H:%M:%S") +
            " - auto-refresh cada " + str(REFRESH_SECONDS) + "s</div>"
            "<div class='row'>" + cols_html + "</div></body></html>")
    return html


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(build_html().encode("utf-8"))

    def log_message(self, format, *args):
        pass


def main():
    server = HTTPServer(("0.0.0.0", DASHBOARD_PORT), Handler)
    print("Dashboard corriendo en http://localhost:" + str(DASHBOARD_PORT))
    server.serve_forever()


if __name__ == "__main__":
    main()
