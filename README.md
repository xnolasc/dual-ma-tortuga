# Dual Moving Average - Experimento comparativo

Sistema alternativo y mas liviano al Turtle Trading (proyecto hermano
tortuga-tacana), disenado para probar que regla de tamano de posicion
funciona mejor con capital limitado. Corre en paralelo, sin conexion
con el sistema Turtle.

## Español

### Que es esto

Compara 3 reglas de "cuanto comprar", con la misma senal de entrada y
salida (cruce de dos promedios moviles de 10 y 30 dias) sobre los
mismos 4 mercados: MU, SNDK, WDC, BE (todos tokenizados via bStock en
Binance).

### Los 3 ledgers

- Ledger 1 (Todo): compra con todo el capital disponible.
- Ledger 2 (% fijo): maximo 15% del capital total por compra.
- Ledger 3 (ATR): capital x %riesgo / (2xN), misma formula del Turtle.

Capital inicial $1,000 cada uno, fijo, sin recargas. Nunca comparten
plata entre si.

### Por que 3 ledgers

El Turtle original esta disenado para capital grande repartido entre
muchos mercados. Con capital chico, un pool de $4,200 contra 35
tickers se satura con 3-4 posiciones, dejando el resto esperando.
Este experimento responde, con datos reales, que regla de tamano
aprovecha mejor la plata disponible cuando el capital es limitado.

### Instalacion

```bash
git clone https://github.com/xnolasc/dual-ma-tortuga.git
cd dual-ma-tortuga
python3 -m venv venv
source venv/bin/activate
pip install requests
```

### Como correr el trader

```bash
python3 dualma_trader.py
```

### Como correr el dashboard

```bash
python3 dualma_dashboard.py
```
Abrir http://localhost:8894

### Como correr el reporte comparativo

```bash
python3 comparar_ledgers.py
```

El reporte imprime en pantalla y guarda un archivo reporte_YYYY-MM-DD.md
con las 6 metricas:

1. Retorno total (%)
2. Operaciones totales + win rate
3. Maxima caida (drawdown)
4. Comisiones pagadas
5. Ciclos sin capital disponible
6. Wipeout total (si/no + fecha)

### Como cerrar una ronda y empezar de nuevo

```bash
python3 nueva_ronda.py "descripcion de que se ajusto"
```

Archiva los 3 ledgers actuales con fecha, y arranca 3 ledgers nuevos
con capital fresco de $1,000 cada uno.

Todo corre en modo simulado (paper trading). El precio se lee en vivo
de Binance (dato publico), pero la plata y las operaciones son 100%
simuladas.

---

## English

### What is this

Compares 3 rules for "how much to buy", using the same entry/exit
signal (crossover of two moving averages, 10 and 30 days) across the
same 4 markets: MU, SNDK, WDC, BE (all tokenized via bStock on
Binance).

### The 3 ledgers

- Ledger 1 (All-in): buys with all available capital.
- Ledger 2 (Fixed %): maximum 15% of total capital per trade.
- Ledger 3 (ATR): capital x risk% / (2xN), same formula as the Turtle.

Initial capital $1,000 each, fixed, never topped up. They never share
funds.

### Why 3 ledgers

The original Turtle is designed for large capital spread across many
markets. With small capital, a $4,200 pool against 35 tickers gets
saturated with 3-4 positions, leaving the rest waiting. This
experiment answers, with real data, which sizing rule makes the best
use of available money when capital is limited.

### Installation

```bash
git clone https://github.com/xnolasc/dual-ma-tortuga.git
cd dual-ma-tortuga
python3 -m venv venv
source venv/bin/activate
pip install requests
```

### Running the trader

```bash
python3 dualma_trader.py
```

### Running the dashboard

```bash
python3 dualma_dashboard.py
```
Open http://localhost:8894

### Running the comparative report

```bash
python3 comparar_ledgers.py
```

Prints to screen and saves a reporte_YYYY-MM-DD.md file with the 6
metrics:

1. Total return (%)
2. Total trades + win rate
3. Max drawdown
4. Commissions paid
5. Cycles blocked by lack of capital
6. Total wipeout (yes/no + date)

### Closing a round and starting fresh

```bash
python3 nueva_ronda.py "description of what changed"
```

Archives the current 3 ledgers with a date, and starts 3 fresh
ledgers with $1,000 initial capital each.

Everything runs in simulated mode (paper trading). Prices are read
live from Binance (public data), but the money and trades are 100%
simulated.

---

Companion project to tortuga-tacana.
