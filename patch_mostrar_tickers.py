PATH = "dualma_dashboard.py"

with open(PATH) as f:
    content = f.read()

old = '''from dualma_common import LEDGER_TODO_PATH, LEDGER_PORCENTAJE_PATH, LEDGER_ATR_PATH'''
new = '''from dualma_common import LEDGER_TODO_PATH, LEDGER_PORCENTAJE_PATH, LEDGER_ATR_PATH, TICKERS'''
assert content.count(old) == 1
content = content.replace(old, new)

old2 = '''        posiciones_html = ""
        if l["posiciones"]:
            for t, pos in l["posiciones"].items():
                posiciones_html += (
                    "<div class='pos'>" + t + ": " + str(round(pos["shares"], 4)) +
                    " @ $" + str(round(pos["entry_price"], 2)) + "</div>"
                )
        else:
            posiciones_html = "<div class='pos-empty'>sin posiciones abiertas</div>"'''
new2 = '''        posiciones_html = ""
        for t in TICKERS:
            pos = l["posiciones"].get(t)
            if pos:
                posiciones_html += (
                    "<div class='pos en-pos'>" + t + ": EN_POSICION " +
                    str(round(pos["shares"], 4)) + " @ $" + str(round(pos["entry_price"], 2)) + "</div>"
                )
            else:
                posiciones_html += "<div class='pos esperando'>" + t + ": ESPERANDO cruce</div>"'''
assert content.count(old2) == 1
content = content.replace(old2, new2)

old3 = '''            ".pos-empty{color:#666;font-size:13px;font-style:italic;}"'''
new3 = '''            ".pos-empty{color:#666;font-size:13px;font-style:italic;}"
            ".en-pos{color:#4ade80;}"
            ".esperando{color:#777;}"'''
assert content.count(old3) == 1
content = content.replace(old3, new3)

with open(PATH, "w") as f:
    f.write(content)

print("[OK] ahora lista los 4 tickers con su status siempre")
