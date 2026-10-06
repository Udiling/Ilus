# C:\Offline Server\create_db.py
import sqlite3
import os

DB_PATH = "cafe.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Таблица menu
    c.execute("""
        CREATE TABLE IF NOT EXISTS menu (
            name_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            unit TEXT,
            price REAL NOT NULL,
            description TEXT,
            category TEXT
        )
    """)

    # Таблица oper
    c.execute("""
        CREATE TABLE IF NOT EXISTS oper (
            oper_id INTEGER PRIMARY KEY AUTOINCREMENT,
            oper TEXT NOT NULL,
            name TEXT,
            lastname TEXT,
            qr_data TEXT
        )
    """)

    conn.commit()
    conn.close()

    print(f"[OK] База данных готова: {os.path.abspath(DB_PATH)}")
    print("[OK] Созданы таблицы: menu, oper")

if __name__ == "__main__":
    init_db()
