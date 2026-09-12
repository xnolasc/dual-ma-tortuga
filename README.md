# 📈 Dual Moving Average — Experimento comparativo

> Sistema alternativo y mas liviano al Turtle Trading, disenado
> especificamente para probar que regla de tamano de posicion funciona
> mejor con **capital limitado**. Corre en paralelo, sin ninguna
> conexion con el sistema Turtle.

---

## 🇪🇸 Español

### ¿Que es esto?

Un experimento de trading algoritmico que compara **3 reglas distintas
de "cuanto comprar"**, usando exactamente la misma senal de entrada y
salida (el cruce de dos promedios moviles) sobre los mismos 4 mercados.
El objetivo es responder, con datos reales, cual regla de tamano de
posicion rinde mejor cuando el capital disponible es chico.

### La senal (igual para los 3 ledgers)

- **Promedio corto**: 10 dias.
- **Promedio largo**: 30 dias.
- **Compra**: cuando el promedio corto cruza por arriba del largo.
- **Vende**: cuando el promedio corto cruza por debajo del largo.
- Sin piramide -- una sola unidad por posicion.
- Revision diaria (los promedios son de dias, no tiene sentido revisar
  mas seguido).

### Los 3 ledgers

| | Ledger 1: Todo | Ledger 2: % fijo | Ledger 3: ATR |
|---|---|---|---|
| Capital inicial | $1,000 | $1,000 | $1,000 |
| Regla | Todo el capital libre | Max 15% del capital total | capital x %riesgo / (2xN) |
| Riesgo | Alto | Moderado | Ajustado a volatilidad |

Cada ledger tiene su propio capital, separado -- **nunca comparten
plata entre si**. El capital inicial es fijo: nunca se recarga cuando
se agota, ni se retira cuando gana. Si un ledger llega a $0 disponible
y $0 en posiciones abiertas ("wipeout total"), se queda asi hasta el
cierre de la ronda de evaluacion -- ese es un resultado valido del
experimento, no un error.

### Mercados usados (acciones tokenizadas en Binance)

| Ticker | Empresa | bStock |
|---|---|---|
| MU | Micron | MUB |
| SNDK | Sandisk | SNDKB |
| WDC | Western Digital | WDCB |
| BE | Bloom Energy | BEB |

### Reporte -- las 6 metricas

Cada vez que se corre comparar_ledgers.py, se imprime en pantalla y
se guarda un archivo reporte_YYYY-MM-DD.md, con:

1. Retorno total (%)
2. Operaciones totales + win rate
3. Maxima caida (drawdown, aproximada)
4. Comisiones pagadas
5. Ciclos sin capital disponible
6. Wipeout total (si/no + fecha)

### Rondas de evaluacion

Al cerrar un periodo de evaluacion, nueva_ronda.py archiva los 3
ledgers y el ultimo reporte en una carpeta con fecha, y arranca 3
ledgers nuevos en blanco con capital fresco -- permitiendo comparar la
evolucion del experimento de una ronda a la siguiente.

---

## Instalacion

### Requisitos previos
- Python 3.9 o superior
- macOS, Linux o Windows

### 1. Clona el repositorio
```bash
git clone https://github.com/xnolasc/dual-ma-tortuga.git
cd dual-ma-tortuga
```

### 2. Crea y activa un entorno virtual
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Instala las dependencias
```bash
pip install requests
```

### 4. Corre el trader (modo prueba manual)
```bash
python3 dualma_trader.py
```

### 5. Levanta el dashboard local
```bash
python3 dualma_dashboard.py
```
Abri http://localhost:8894 en el navegador.

### 6. Genera el reporte comparativo cuando quieras
```bash
python3 comparar_ledgers.py
```

### 7. Cierra una ronda de evaluacion
```bash
python3 nueva_ronda.py "descripcion de que se ajusto"
```

**Todo corre en modo simulado (paper trading).** No se ejecuta ninguna
orden real en Binance -- el precio se lee en vivo (dato publico, sin
necesitar API Key), pero la plata y las operaciones son 100% simuladas.

---

## 🇬🇧 English

### What is this?

An algorithmic trading experiment comparing 3 different position-sizing
rules, using the exact same entry/exit signal (a two-moving-average
crossover) across the same 4 markets. The goal is to answer, with real
data, which sizing rule performs best when available capital is small.

### The signal (same for all 3 ledgers)

- Short average: 10 days.
- Long average: 30 days.
- Buy: when the short average crosses above the long one.
- Sell: when the short average crosses below the long one.
- No pyramiding -- a single unit per position.
- Daily check.

### The 3 ledgers

| | Ledger 1: All-in | Ledger 2: Fixed % | Ledger 3: ATR |
|---|---|---|---|
| Initial capital | $1,000 | $1,000 | $1,000 |
| Rule | All available capital | Max 15% of total capital | capital x risk% / (2xN) |
| Risk | High | Moderate | Volatility-adjusted |

Each ledger has its own separate capital -- they never share funds.
Initial capital is fixed and never topped up or withdrawn mid-round.

### Markets used

| Ticker | Company | bStock |
|---|---|---|
| MU | Micron | MUB |
| SNDK | Sandisk | SNDKB |
| WDC | Western Digital | WDCB |
| BE | Bloom Energy | BEB |

### Installation

```bash
git clone https://github.com/xnolasc/dual-ma-tortuga.git
cd dual-ma-tortuga
python3 -m venv venv
source venv/bin/activate
pip install requests
python3 dualma_trader.py
python3 dualma_dashboard.py
python3 comparar_ledgers.py
python3 nueva_ronda.py "description of what changed"
```

**Everything runs in simulated mode (paper trading).** No real order is
ever placed on Binance.

---

*Companion project to tortuga-tacana.*
