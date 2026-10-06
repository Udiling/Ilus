import sqlite3

db_path = r"C:\Offline Server\cafe.db"
con = sqlite3.connect(db_path)
cur = con.cursor()

print("Таблицы:")
for (name,) in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    print(" -", name)

print("\n--- counts ---")
tables = [
    "active_orders", "order_events", "payments",
    "tips", "discounts", "menu", "oper",
    "reservations", "auth_log"
]

for t in tables:
    try:
        count = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t}: {count}")
    except Exception as e:
        print(f"{t}: ERR - {e}")

con.close()
