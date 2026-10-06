# C:\Offline Server\db\manager.py
import sqlite3
import os
import shutil
from datetime import date, datetime
from utils.references import generate_ref

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'cafe.db')
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute('PRAGMA journal_mode=WAL')
LOG_PATH = "load.log"

ARCH_MENU_DIR = os.path.join(os.getcwd(), "arch", "menu")
ARCH_OPER_DIR = os.path.join(os.getcwd(), "arch", "oper")

		
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_active_menu():
    cur = db.execute('SELECT name_id, name, unit, price, description, category FROM menu')
    return [dict(r) for r in cur.fetchall()]
	
def replace_menu(data):
    # data — список словарей, полученных из txt-файла
    db.executemany(
        'INSERT OR REPLACE INTO menu (name_id, name, unit, price, description, category) VALUES (?, ?, ?, ?, ?, ?)',
        [
            (
                str(r.get('name_id', '')),
                str(r.get('name', '')),
                str(r.get('unit', '')),
                float(r.get('price', 0)),
                str(r.get('description', '')),
                str(r.get('category', ''))
            ) for r in data
        ]
    )
    db.commit()
	
def _parse_price(price_val):
    if isinstance(price_val, (int, float)):
        return float(price_val)
    if isinstance(price_val, str):
        clean_str = str(price_val).replace(',', '.').strip()
        if not clean_str:
            return None
        try:
            return float(clean_str)
        except ValueError:
            return None
    return None

def init_db():
    """Создаёт актуальную структуру таблиц для оффлайн-кафе."""
    conn = None
    try:
        conn = get_conn()
        c = conn.cursor()

        # 1. Справочник меню
        c.execute("""
            CREATE TABLE IF NOT EXISTS menu (
                name_id TEXT PRIMARY KEY NOT NULL,
                name TEXT NOT NULL,
                unit TEXT,
                price REAL NOT NULL,
                description TEXT,
                category TEXT,
                is_active INTEGER DEFAULT 1
            )
        """)

        # 2. Справочник операторов
        c.execute("""
            CREATE TABLE IF NOT EXISTS oper (
                oper_id TEXT PRIMARY KEY NOT NULL,
                oper TEXT,
                name TEXT,
                lastname TEXT,
                qr_data TEXT
            )
        """)

        # 3. Активные заказы (гранулярная логика и конвейер)
        c.execute("""
            CREATE TABLE IF NOT EXISTS active_orders (
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
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
		
        # 4. Журнал транзакций
        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts DATETIME NOT NULL,
                operator_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                details TEXT,
                ref_id TEXT NOT NULL
            )
        """)

        # Seed-данные: создаём гостевого оператора, если таблица пуста
        c.execute("INSERT OR IGNORE INTO oper (oper_id, oper, name, qr_data) VALUES (?, ?, ?, ?)",
                  ("guest", "Гость", "Системный", "guest_qr"))

        conn.commit()
        print("[OK] База данных успешно инициализирована.")
    except Exception as e:
        if conn: conn.rollback()
        print(f"[ERROR] Ошибка при инициализации БД: {e}")
    finally:
        if conn: conn.close()
    # ВАЖНО: запускаем миграцию над глобальным соединением db (оно открыто при импорте)
    try:
        migrate_v1()
        migrate_v2()
        migrate_v3()
        migrate_v4()
        migrate_v5()
    except Exception as e:
        print(f"[WARN] migrate: {e}")

def _log_message(message: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}\n"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(log_entry)
    print(log_entry.strip())

def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def _archive_file(filepath: str, archive_dir: str):
    _ensure_dir(archive_dir)
    filename = os.path.basename(filepath)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    archive_name = f"{os.path.splitext(filename)[0]}_{timestamp}{os.path.splitext(filename)[1]}"
    archive_path = os.path.join(archive_dir, archive_name)

    try:
        shutil.copy2(filepath, archive_path)
        _log_message(f"Файл заархивирован: {archive_path}")
    except Exception as e:
        _log_message(f"Ошибка архивации файла: {e}")

def load_menu_from_file():
    today = date.today()
    date_str = today.strftime('%Y-%m-%d')
    filepath = os.path.join(os.getcwd(), f"menu-{date_str}.txt")

    _log_message(f"--- Начало загрузки меню за {date_str} ---")
    if not os.path.exists(filepath):
        _log_message(f"Файл меню не найден: {filepath}. Создайте его вручную.")
        return

    rows_to_insert = []
    errors = []

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        _log_message(f"Ошибка чтения файла меню: {e}")
        return

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    total_lines = len(lines)
    _log_message(f"В файле меню найдено строк: {total_lines}")

    for line_idx, line in enumerate(lines, start=1):
        parts = line.split('*')
        if len(parts) < 4:
            errors.append(f"Строка {line_idx}: неверный формат. Нужно минимум 4 поля через '*'. Пропущено.")
            continue

        code = parts[0].strip()
        name = parts[1].strip()
        unit = parts[2].strip()
        price_raw = parts[3].strip()
        description = parts[4].strip() if len(parts) > 4 else ""
        category = parts[5].strip() if len(parts) > 5 else "Разное"

        if not code:
            errors.append(f"Строка {line_idx}: пустой код блюда. Пропущено.")
            continue
        if not name:
            errors.append(f"Строка {line_idx}: пустое название блюда. Пропущено.")
            continue

        price = _parse_price(price_raw)
        if price is None:
            errors.append(f"Строка {line_idx}: неверная цена '{price_raw}'. Пропущено.")
            continue
        if price < 0:
            errors.append(f"Строка {line_idx}: цена не может быть отрицательной. Пропущено.")
            continue

        rows_to_insert.append((code, category, name, unit, price, description))

    valid_count = len(rows_to_insert)
    error_count = len(errors)

    if errors:
        for err in errors:
            _log_message(err)

    _log_message(f"Валидных позиций меню для загрузки: {valid_count}, ошибок/пропущено: {error_count}")
    if not rows_to_insert:
        _log_message("Нет валидных строк для загрузки меню. База не изменена.")
        return

    _archive_file(filepath, ARCH_MENU_DIR)

    conn = None
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("BEGIN TRANSACTION")

        c.execute("SELECT COUNT(*) FROM menu_items")
        old_count_row = c.fetchone()
        old_count = old_count_row[0] if old_count_row else 0

        c.execute("DELETE FROM menu_items")

        c.executemany(
            """
            INSERT INTO menu_items (code, category, name, unit, price_per_unit, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            [(code, cat, name, unit, price) for code, cat, name, unit, price, _ in rows_to_insert]
        )
        conn.commit()

        _log_message(f"База меню обновлена: удалено старых позиций: {old_count}, загружено новых: {valid_count}")
    except sqlite3.IntegrityError as e:
        if conn:
            conn.rollback()
        _log_message(f"Ошибка целостности БД (дубликат кода): {e}")
    except Exception as e:
        if conn:
            conn.rollback()
        _log_message(f"Произошла ошибка при записи в БД меню: {e}")
    finally:
        if conn:
            conn.close()

def get_active_oper():
    cur = db.execute('SELECT oper_id, oper, name, lastname, qr_data FROM oper')
    return [dict(r) for r in cur.fetchall()]
	
def replace_oper(data):
    try:
        db.executemany(
            'INSERT OR REPLACE INTO oper (oper_id, oper, name, lastname, qr_data) VALUES (?, ?, ?, ?, ?)',
            [
                (
                    str(r.get('oper_id', '')),
                    str(r.get('oper', '')),
                    str(r.get('name', '')),
                    str(r.get('lastname', '')),
                    str(r.get('qr_data', ''))
                ) for r in data
            ]
        )
        db.commit()
        _log_message("База операторов успешно обновлена.")

    except sqlite3.IntegrityError as e:
        if 'db' in globals() and hasattr(db, 'rollback'):
            db.rollback()
        _log_message(f"Ошибка целостности БД (дубликат/конфликт записей): {e}")

    except Exception as e:
        if 'db' in globals() and hasattr(db, 'rollback'):
            db.rollback()
        _log_message(f"Ошибка при записи в БД операторов: {e}")

    finally:
        pass
		
def close_db():
    """Корректное закрытие соединения при завершении работы сервера."""
    global db
    if db:
        try:
            db.close()
        except Exception:
            pass
			
def migrate_v1():
    c = db.cursor()
    # 1) Метка времени изменения статуса + триггер
    try:
        c.execute("ALTER TABLE active_orders ADD COLUMN status_ts DATETIME DEFAULT CURRENT_TIMESTAMP")
    except Exception:
        pass
    try:
        c.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_status_ts
        AFTER UPDATE OF status ON active_orders
        BEGIN
            UPDATE active_orders SET status_ts = datetime('now') WHERE id = NEW.id;
        END;
        """)
    except Exception:
        pass

    # 2) Платежи
    c.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL,
        method TEXT NOT NULL, -- cash|card|transfer
        amount REAL NOT NULL,
        oper_id TEXT,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 3) Чаевые
    c.execute("""
    CREATE TABLE IF NOT EXISTS tips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL,
        waiter_id TEXT NOT NULL,
        amount REAL NOT NULL,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 4) Скидки
    c.execute("""
    CREATE TABLE IF NOT EXISTS discounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL,
        percent REAL NOT NULL,
        set_by_oper_id TEXT NOT NULL,
        reason TEXT,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 5) Журнал событий заказа/позиций
    c.execute("""
    CREATE TABLE IF NOT EXISTS order_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT,
        item_id INTEGER,
        event_type TEXT NOT NULL, -- create|status_change|cancel|edit|split|discount
        old_val TEXT,
        new_val TEXT,
        oper_id TEXT,
        pin_ok INTEGER DEFAULT 0,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 6) Кассовый журнал (открытие/закрытие)
    c.execute("""
    CREATE TABLE IF NOT EXISTS cashier_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        opened_by TEXT,
        open_amount REAL,
        opened_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        closed_by TEXT,
        close_amount REAL,
        closed_at DATETIME,
        notes TEXT
    )
    """)

    # 7) Журнал авторизаций (вход/выход)
    c.execute("""
    CREATE TABLE IF NOT EXISTS auth_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        oper_id TEXT,
        action TEXT, -- login|logout|scan_qr
        ip TEXT,
        ua TEXT,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 8) Индексы для ускорения
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON active_orders(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_stage ON active_orders(stage)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_ts ON active_orders(status_ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_order ON order_events(order_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tips_waiter ON tips(waiter_id)")

    db.commit()
    print("[OK] migrate_v1: расширенные таблицы и индексы применены.")	
	
def migrate_v2():
    c = db.cursor()
    # 1) menu: стоп‑лист и приоритет
    try:
        c.execute("ALTER TABLE menu ADD COLUMN stoplisted INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE menu ADD COLUMN priority   INTEGER DEFAULT 0")
    except Exception:
        pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_menu_stop ON menu(stoplisted)")

    # 2) Справочники причин / быстрых причин
    c.execute("""
    CREATE TABLE IF NOT EXISTS cancel_reasons (
        code TEXT PRIMARY KEY,
        title TEXT NOT NULL
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS quick_reasons (
        kind TEXT NOT NULL,         -- cancel|edit|discount
        code TEXT NOT NULL,
        title TEXT NOT NULL,
        PRIMARY KEY(kind, code)
    )""")

    # 3) Лента таймлайна статусов для позиции
    c.execute("""
    CREATE TABLE IF NOT EXISTS order_timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    # триггеры на вставку/апдейт статуса
    c.execute("DROP TRIGGER IF EXISTS trg_timeline_ins")
    c.execute("""
    CREATE TRIGGER IF NOT EXISTS trg_timeline_ins
    AFTER INSERT ON active_orders
    BEGIN
        INSERT INTO order_timeline(item_id, status, ts) VALUES(NEW.id, NEW.status, COALESCE(NEW.status_ts, NEW.created_at));
    END;""")
    c.execute("DROP TRIGGER IF EXISTS trg_timeline_upd_status")
    c.execute("""
    CREATE TRIGGER IF NOT EXISTS trg_timeline_upd_status
    AFTER UPDATE OF status ON active_orders
    BEGIN
        INSERT INTO order_timeline(item_id, status, ts) VALUES(NEW.id, NEW.status, datetime('now'));
    END;""")

    # 4) Маршрут блюд (станция/линия/этапы)
    c.execute("""
    CREATE TABLE IF NOT EXISTS route_matrix (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        min_id INTEGER,
        max_id INTEGER,
        station TEXT NOT NULL,      -- chiry|il_drive|hot|bar_all|salads|banquet
        line INTEGER DEFAULT 1,     -- 1|2
        stage_count INTEGER DEFAULT 1
    )""")

    # 5) Снапшоты дня (агрегаты)
    c.execute("""
    CREATE TABLE IF NOT EXISTS day_snapshot (
        day TEXT PRIMARY KEY,       -- YYYY-MM-DD
        items_gross REAL,
        paid REAL,
        tips REAL,
        cancels_count INTEGER,
        chiry_wait_avg REAL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")

    # 6) Очередь печати/уведомлений
    c.execute("""
    CREATE TABLE IF NOT EXISTS print_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target TEXT NOT NULL,       -- bar|kitchen|client|custom
        payload TEXT NOT NULL,      -- JSON
        status TEXT DEFAULT 'new',  -- new|sent|failed
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_print_status ON print_queue(status)")

    # 7) Аудит-цепочка (tamper-evident)
    c.execute("""
    CREATE TABLE IF NOT EXISTS audit_chain (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prev_hash TEXT,
        payload TEXT NOT NULL,
        hash TEXT NOT NULL,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")

    # 8) KPI по часам (складируем метрики)
    c.execute("""
    CREATE TABLE IF NOT EXISTS kpi_hourly (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT, hour INTEGER, station TEXT, oper_id TEXT,
        produced INTEGER, cancelled INTEGER, avg_lead_min REAL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")

    # 9) Резервные бэкапы БД — реестр
    c.execute("""
    CREATE TABLE IF NOT EXISTS backups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file TEXT NOT NULL,
        sha256 TEXT,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")

    # 10) View сверки кассы (ожидаемое/факт)
    c.execute("""
    CREATE VIEW IF NOT EXISTS v_cash_reconcile AS
    SELECT
      d.day,
      d.paid AS expected_cashflow,
      c.open_amount, c.close_amount,
      (c.close_amount - c.open_amount) AS delta_fact,
      (d.paid - (c.close_amount - c.open_amount)) AS mismatch
    FROM day_snapshot d
    LEFT JOIN cashier_log c
      ON substr(c.opened_at,1,10) = d.day
    ORDER BY d.day DESC
    """)

    db.commit()
    print("[OK] migrate_v2: расширения БД применены.")


def migrate_v3():
    c = db.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS table_signals (
        table_id TEXT PRIMARY KEY,
        calling INTEGER DEFAULT 0,
        paying INTEGER DEFAULT 0,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        table_id TEXT NOT NULL,
        res_date TEXT NOT NULL,
        res_time TEXT NOT NULL,
        duration_min INTEGER DEFAULT 60,
        phone TEXT,
        guest_name TEXT,
        created_by TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_reservations_day ON reservations(res_date, res_time)")
    db.commit()
    print("[OK] migrate_v3: столы и брони.")


def migrate_v4():
    """Оценка обслуживания гостем (1-5) + комментарий после расчёта."""
    c = db.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS service_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL UNIQUE,
        table_id TEXT,
        rating INTEGER NOT NULL,
        comment TEXT,
        oper_id TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_feedback_day ON service_feedback(created_at)")
    db.commit()
    print("[OK] migrate_v4: оценка обслуживания.")


def _column_exists(c, table, column):
    c.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in c.fetchall())


def migrate_v5():
    """Предзаказы при бронировании: гости, закрытое мероприятие, аванс, состав заказа."""
    c = db.cursor()
    migrate_v3()
    new_cols = {
        "guests_count": "INTEGER DEFAULT 0",
        "is_closed_event": "INTEGER DEFAULT 0",
        "advance_amount": "REAL DEFAULT 0",
        "advance_method": "TEXT",
        "order_json": "TEXT",
        "parent_res_id": "INTEGER",
        "status": "TEXT DEFAULT 'Активна'",
        "cancel_notify_time": "TEXT",
    }
    for col, typ in new_cols.items():
        if not _column_exists(c, "reservations", col):
            c.execute(f"ALTER TABLE reservations ADD COLUMN {col} {typ}")
    db.commit()
    print("[OK] migrate_v5: предзаказы при бронировании.")