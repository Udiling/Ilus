import sqlite3

DB_PATH = "cafe.db"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Проверим, какие таблицы есть
c.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = [row[0] for row in c.fetchall()]
print("Таблицы в БД:", tables)

# Проверим операторов
c.execute("SELECT * FROM operators")
rows = c.fetchall()
print(f"Операторов в БД: {len(rows)}")
for r in rows:
    print(r)

conn.close()
