import sqlite3

conn = sqlite3.connect('database_cantine.db')
cur = conn.cursor()

# 1. Normalizza tutto in minuscolo
cur.execute("UPDATE dato_sensore SET produttore = LOWER(produttore), sede = LOWER(sede)")
print(f"Normalizzate {cur.rowcount} righe in minuscolo")

# 2. Rinomina 'fratelliurbani' → 'urbani'
cur.execute("UPDATE dato_sensore SET produttore = 'urbani' WHERE produttore = 'fratelliurbani'")
print(f"Rinominate {cur.rowcount} righe FratelliUrbani → urbani")

conn.commit()

# Verifica risultato
cur.execute("SELECT DISTINCT produttore, sede FROM dato_sensore ORDER BY produttore, sede")
print("\n=== Dopo la pulizia ===")
for r in cur.fetchall():
    print(r)

conn.close()