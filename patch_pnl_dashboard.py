PATH = "dualma_dashboard.py"

with open(PATH) as f:
    content = f.read()

old = '''            if pos:
                posiciones_html += (
                    "<div class='pos en-pos'>" + t + ": EN_POSICION " +
                    str(round(pos["shares"], 4)) + " @ $" + str(round(pos["entry_price"], 2)) + "</div>"
                )'''
new = '''            if pos:
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
                )'''

count = content.count(old)
assert count == 1, "[FALLO] encontrado " + str(count) + " veces (se esperaba 1)."
content = content.replace(old, new)

old2 = '''            ".en-pos{color:#4ade80;}"'''
new2 = '''            ".en-pos{color:#4ade80;}"
            ".ganancia{color:#4ade80;font-weight:bold;}"
            ".perdida{color:#f87171;font-weight:bold;}"'''
assert content.count(old2) == 1
content = content.replace(old2, new2)

with open(PATH, "w") as f:
    f.write(content)

print("[OK] P&L flotante agregado")
