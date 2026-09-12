"""
nueva_ronda.py
Cierra la ronda actual: archiva los 3 ledgers + el ultimo log + reportes
sueltos en una carpeta con fecha, arranca 3 ledgers nuevos en blanco
con capital fresco, y anota en historial_rondas.md que se ajusto.

Uso: python3 nueva_ronda.py "descripcion de que se ajusto"
"""
import glob
import os
import shutil
import sys
import time

from dualma_common import (
    BASE_DIR, CAPITAL_INICIAL_USD, TRADER_LOG,
    LEDGER_TODO_PATH, LEDGER_PORCENTAJE_PATH, LEDGER_ATR_PATH,
)
from dualma_ledger import empty_ledger, save_ledger

HISTORIAL_PATH = os.path.join(BASE_DIR, "historial_rondas.md")
ARCHIVO_DIR = os.path.join(BASE_DIR, "archivo")


def siguiente_numero_ronda():
    if not os.path.exists(ARCHIVO_DIR):
        return 1
    existentes = [d for d in os.listdir(ARCHIVO_DIR) if d.startswith("ronda_")]
    numeros = []
    for d in existentes:
        try:
            numeros.append(int(d.split("_")[1]))
        except Exception:
            pass
    return (max(numeros) + 1) if numeros else 1


def main():
    nota = sys.argv[1] if len(sys.argv) > 1 else "(sin nota)"

    numero = siguiente_numero_ronda()
    fecha = time.strftime("%Y-%m-%d")
    carpeta_ronda = os.path.join(ARCHIVO_DIR, "ronda_{:02d}_{}".format(numero, fecha))
    os.makedirs(carpeta_ronda, exist_ok=True)

    archivos_a_mover = [LEDGER_TODO_PATH, LEDGER_PORCENTAJE_PATH, LEDGER_ATR_PATH]
    archivos_a_mover += glob.glob(os.path.join(BASE_DIR, "reporte_*.md"))
    if os.path.exists(TRADER_LOG):
        archivos_a_mover.append(TRADER_LOG)

    movidos = []
    for path in archivos_a_mover:
        if os.path.exists(path):
            destino = os.path.join(carpeta_ronda, os.path.basename(path))
            shutil.move(path, destino)
            movidos.append(os.path.basename(path))

    for path in [LEDGER_TODO_PATH, LEDGER_PORCENTAJE_PATH, LEDGER_ATR_PATH]:
        save_ledger(path, empty_ledger(CAPITAL_INICIAL_USD))

    linea = (
        "## Ronda " + str(numero) + " - cerrada el " + fecha + "\n\n"
        "- Nota: " + nota + "\n"
        "- Archivos archivados: " + (", ".join(movidos) if movidos else "ninguno") + "\n"
        "- Carpeta: " + carpeta_ronda + "\n\n"
    )
    modo = "a" if os.path.exists(HISTORIAL_PATH) else "w"
    with open(HISTORIAL_PATH, modo) as f:
        if modo == "w":
            f.write("# Historial de rondas - Dual Moving Average\n\n")
        f.write(linea)

    print("Ronda " + str(numero) + " archivada en: " + carpeta_ronda)
    print("Archivos movidos: " + str(movidos))
    print("Los 3 ledgers se recrearon con $" + str(CAPITAL_INICIAL_USD) + " cada uno.")
    print("Anotado en " + HISTORIAL_PATH)


if __name__ == "__main__":
    main()
