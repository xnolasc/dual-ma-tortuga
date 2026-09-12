PATH = "dualma_common.py"

with open(PATH) as f:
    content = f.read()

old = 'TICKER_PRICE_ENDPOINT = "/api/v3/ticklePrice" if False else "/api/v3/ticker/price"'
new = 'TICKER_PRICE_ENDPOINT = "/api/v3/ticker/price"'

count = content.count(old)
assert count == 1, "[FALLO] encontrado " + str(count) + " veces (se esperaba 1)."
content = content.replace(old, new)

with open(PATH, "w") as f:
    f.write(content)

print("[OK] endpoint limpiado")
