PATH = "comparar_ledgers.py"

with open(PATH) as f:
    content = f.read()

old = '''    lineas.append(fila("Wipeout total", "wipeout", lambda v: "SI (" + str(next((f['fecha_wipeout'] for f in filas if f['nombre']==f['nombre']),'')) + ")" if v else "No"))
    return "\\n".join(lineas)'''

new = '''    vals_wipeout = []
    for f in filas:
        if f.get("sin_datos"):
            vals_wipeout.append("sin datos")
        elif f["wipeout"]:
            vals_wipeout.append("SI (" + str(f["fecha_wipeout"]) + ")")
        else:
            vals_wipeout.append("No")
    lineas.append("| Wipeout total | " + " | ".join(vals_wipeout) + " |")
    return "\\n".join(lineas)'''

count = content.count(old)
assert count == 1, "[FALLO] encontrado " + str(count) + " veces (se esperaba 1)."
content = content.replace(old, new)

with open(PATH, "w") as f:
    f.write(content)

print("[OK] bug de wipeout corregido")
