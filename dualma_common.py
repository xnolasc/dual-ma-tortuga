"""
dualma_common.py
Configuracion compartida para el experimento Dual Moving Average.
Reutiliza el mismo patron de symbol_for() que Tortuga (sufijo B para
acciones tokenizadas, USDT normal para el resto).
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TICKERS = ["MU", "SNDK", "WDC", "BE"]

SIMBOLOS_ACCIONES = {"MU", "SNDK", "WDC", "BE"}

def symbol_for(ticker: str) -> str:
    if ticker in SIMBOLOS_ACCIONES:
        return f"{ticker}BUSDT"
    return f"{ticker}USDT"

COMMISSION_PCT = 0.001
MIN_NOTIONAL_USD = 10.0

PROMEDIO_CORTO_DIAS = 10
PROMEDIO_LARGO_DIAS = 30

CAPITAL_INICIAL_USD = 1000.0
PORCENTAJE_FIJO_MAX = 0.15

BINANCE_BASE = "https://api.binance.com"
TICKER_PRICE_ENDPOINT = "/api/v3/ticker/price"
KLINES_ENDPOINT = "/api/v3/klines"

LEDGER_TODO_PATH = os.path.join(BASE_DIR, "ledger_todo.json")
LEDGER_PORCENTAJE_PATH = os.path.join(BASE_DIR, "ledger_porcentaje.json")
LEDGER_ATR_PATH = os.path.join(BASE_DIR, "ledger_atr.json")
STATE_PATH = os.path.join(BASE_DIR, "dualma_state.json")
TRADES_LOG_PATH = os.path.join(BASE_DIR, "dualma_trades.json")
TRADER_LOG = os.path.join(BASE_DIR, "dualma_trader.log")
