# C:\Offline Server\fix_status_ts.py
import sqlite3, os, shutil, hashlib

DB = "cafe.db"
BAK_DIR = "backup"
os.makedirs(BAK_DIR, exist_ok=True)

# 1) Резервная копия
stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
bak = os.path.join(BAK_DIR, f"cafe-before-fix-{stamp}.db")
shutil.copy2(DB, bak)

# 2) Открываем БД и правим структуру
db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row
cur = db.cursor()

def has_col(table, col):
    cols = {r["name"] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}
    return col in cols

try:
    cur.execute("BEGIN")
    # Создаём новую таблицу с нужной колонкой status_ts
    cur.execute("""
    CREATE TABLE IF NOT EXISTS __ao_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL,
        oper_id TEXT,
        table_id TEXT,
        name_id TEXT,
        name TEXT,
        qty INTEGER DEFAULT 1,
        price REAL,
        status TEXT DEFAULT 'new',
        stage INTEGER DEFAULT 1,
        production_line INTEGER DEFAULT 1,
        pickup_point TEXT DEFAULT 'bar',
        discount_pct REAL DEFAULT 0.0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        status_ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")

    if has_col("active_orders", "status_ts"):
        # Перенос как есть
        cur.execute("""
        INSERT INTO __ao_new (
            id, order_id, oper_id, table_id, name_id, name, qty, price, status,
            stage, production_line, pickup_point, discount_pct, created_at, status_ts
        )
        SELECT
            id, order_id, oper_id, table_id, name_id, name, qty, price, status,
            stage, production_line, pickup_point, discount_pct, created_at, status_ts
        FROM active_orders
        """)
    else:
        # Если колонки не было — подставим created_at
        cur.execute("""
        INSERT INTO __ao_new (
            id, order_id, oper_id, table_id, name_id, name, qty, price, status,
            stage, production_line, pickup_point, discount_pct, created_at, status_ts
        )
        SELECT
            id, order_id, oper_id, table_id, name_id, name, qty, price, status,
            stage, production_line, pickup_point, discount_pct, created_at, created_at
        FROM active_orders
        """)

    # Меняем таблицы местами
    cur.execute("DROP TABLE active_orders")
    cur.execute("ALTER TABLE __ao_new RENAME TO active_orders")

    # Триггер для авто-обновления status_ts
    cur.execute("DROP TRIGGER IF EXISTS trg_status_ts")
    cur.execute("""
    CREATE TRIGGER IF NOT EXISTS trg_status_ts
    AFTER UPDATE OF status ON active_orders
    BEGIN
        UPDATE active_orders SET status_ts = datetime('now') WHERE id = NEW.id;
    END;
    """)

    # Индексы
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON active_orders(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_stage  ON active_orders(stage)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_ts     ON active_orders(status_ts)")

    db.commit()
    print("[OK] active_orders перестроена: status_ts добавлен, триггер создан.")
except Exception as e:
    db.rollback()
    print("[ERROR] Исправление не удалось:", e)
finally:
    db.close()
