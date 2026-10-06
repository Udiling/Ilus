from pathlib import Path
from fastapi import APIRouter, HTTPException, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
import os, json, shutil
from datetime import datetime, timedelta
from core.config import TEMPLATES_DIR, PINS, REPORTS_DIR
from db.manager import db, migrate_v3, migrate_v4, migrate_v5
from utils.references import generate_ref
from utils.access_rights import (
    SUPERUSERS, STATION_RANGES, ACCESS_MATRIX, ALLOWED_CANCEL_IDS,
    get_station_by_id, get_pickup_by_id, get_pickup_for_item, is_pickup_queue_view,
    get_chiry_line_stage,
    normalize_template,
    is_waiter_serve_view,
    can_access_form, allowed_forms, can_create_order, can_order_item,
    filter_menu_for_operator, operator_permissions, is_superuser,
    get_operator_discount_pct, max_manual_discount_pct, can_set_manual_discount,
    is_delivery, is_discount_operator,
)

router = APIRouter()

# --- ВРЕМЕННАЯ ДИАГНОСТИКА ---
from db.manager import migrate_v1

@router.get("/admin/check_schema")
def admin_check_schema(oper_id: str = Query(None)):
    _require_superuser(oper_id)
    def col_exists(table, col):
        try:
            row = db.execute(f"PRAGMA table_info({table})").fetchall()
            cols = {r['name'] for r in row}
            return col in cols
        except:
            return False
    def table_exists(name):
        r = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        return bool(r)

    report = {
        "tables": {
            "active_orders": table_exists("active_orders"),
            "order_events": table_exists("order_events"),
            "payments": table_exists("payments"),
            "tips": table_exists("tips"),
            "discounts": table_exists("discounts"),
        },
        "columns": {
            "active_orders.stage": col_exists("active_orders", "stage"),
            "active_orders.status_ts": col_exists("active_orders", "status_ts"),
        }
    }
    return JSONResponse(report)

@router.post("/admin/migrate")
def admin_migrate(oper_id: str = Form(...)):
    _require_superuser(oper_id)
    try:
        migrate_v1()
        return JSONResponse({"status": "ok", "message": "migrate_v1 applied"})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

# ------------------------------
# PIN и права на отмену/редактирование/скидки
# ------------------------------

# PIN-коды хранятся в pins.json в корне проекта (core.config.PINS)
ROOT_PIN = PINS["root"]
STAFF_PIN = PINS["staff"]   # для 31–40

# Файловый флаг ручного пуска 2-й линии Чиры
BASE_DIR = Path(__file__).parent.parent
LINE2_FLAG = os.path.join(BASE_DIR, "chiry_line2.flag")
CHIRY_PIN = PINS["chiry_line2"]  # PIN директора для запуска линии 2
MAX_TABLE_PIN = PINS["max_table"]  # PIN директора для изменения числа столов
MAX_TABLE_FILE = os.path.join(BASE_DIR, "max_table.txt")
DEFAULT_MAX_TABLE = 25
MIN_MAX_TABLE = 1
MAX_MAX_TABLE = 25  # 26–30 заняты самовыносом
RESERVE_MINUTES = 60

def _chiry_line2_active() -> bool:
    return os.path.exists(LINE2_FLAG)


def get_max_table() -> int:
    try:
        with open(MAX_TABLE_FILE, "r", encoding="utf-8") as f:
            n = int((f.read() or "").strip())
        if MIN_MAX_TABLE <= n <= MAX_MAX_TABLE:
            return n
    except Exception:
        pass
    return DEFAULT_MAX_TABLE


def _save_max_table(n: int) -> None:
    with open(MAX_TABLE_FILE, "w", encoding="utf-8") as f:
        f.write(str(n))


def _is_allowed_table_id(table_id: str) -> bool:
    raw = (table_id or "").strip()
    if not raw:
        return False
    if not raw.isdigit():
        return True
    n = int(raw)
    if 26 <= n <= 30:
        return True
    return 1 <= n <= get_max_table()


def _parse_res_dt(res_date: str, res_time: str) -> datetime:
    t = (res_time or "").strip()
    if len(t) == 5:
        t = t + ":00"
    return datetime.strptime(f"{res_date} {t}", "%Y-%m-%d %H:%M:%S")


def _reservation_overlap(a: datetime, b: datetime, minutes: int = RESERVE_MINUTES) -> bool:
    a_end = a + timedelta(minutes=minutes)
    b_end = b + timedelta(minutes=minutes)
    return a < b_end and b < a_end


def _booked_table_ids(res_date: str, res_time: str, exclude_id: int | None = None) -> set[str]:
    start = _parse_res_dt(res_date, res_time)
    day_from = (start - timedelta(days=1)).strftime("%Y-%m-%d")
    day_to = (start + timedelta(days=1)).strftime("%Y-%m-%d")
    rows = db.execute(
        "SELECT id, table_id, res_date, res_time, status FROM reservations WHERE res_date BETWEEN ? AND ?",
        (day_from, day_to),
    ).fetchall()
    busy: set[str] = set()
    for r in rows:
        if exclude_id is not None and int(r["id"]) == int(exclude_id):
            continue
        if (r["status"] or "Активна") == "Отменен":
            continue
        other = _parse_res_dt(r["res_date"], r["res_time"])
        if _reservation_overlap(start, other):
            busy.add(str(r["table_id"]))
    return busy


def _set_table_signal(table_id: str, kind: str):
    tid = str(table_id).strip()
    if kind == "call":
        db.execute("""
            INSERT INTO table_signals (table_id, calling, paying, updated_at)
            VALUES (?, 1, 0, datetime('now'))
            ON CONFLICT(table_id) DO UPDATE SET calling=1, updated_at=datetime('now')
        """, (tid,))
    elif kind == "pay":
        db.execute("""
            INSERT INTO table_signals (table_id, calling, paying, updated_at)
            VALUES (?, 0, 1, datetime('now'))
            ON CONFLICT(table_id) DO UPDATE SET paying=1, updated_at=datetime('now')
        """, (tid,))


def _clear_table_signals(table_id: str, kind: str = "all"):
    tid = str(table_id).strip()
    if kind == "call":
        db.execute("UPDATE table_signals SET calling=0, updated_at=datetime('now') WHERE table_id=?", (tid,))
    elif kind == "pay":
        db.execute("UPDATE table_signals SET paying=0, updated_at=datetime('now') WHERE table_id=?", (tid,))
    else:
        db.execute("UPDATE table_signals SET calling=0, paying=0, updated_at=datetime('now') WHERE table_id=?", (tid,))


def _assign_chiry_production_line() -> int:
    """Назначить линию Чиры: 1 — всегда; при активной 2-й — баланс ~50/50 по заказам."""
    if not _chiry_line2_active():
        return 1
    row = db.execute("""
        SELECT production_line, COUNT(DISTINCT order_id) AS cnt
        FROM active_orders
        WHERE CAST(name_id AS INTEGER) BETWEEN 401 AND 450
          AND status != 'served'
        GROUP BY production_line
    """).fetchall()
    counts = {1: 0, 2: 0}
    for r in row:
        counts[int(r[0])] = int(r[1])
    return 1 if counts[1] <= counts[2] else 2

def _verify_pin(oper_id: str, pin: str) -> bool:
    if not oper_id or not pin:
        return False
    op = oper_id.strip()
    if op in SUPERUSERS:
        return pin == ROOT_PIN
    if op in ALLOWED_CANCEL_IDS:
        return pin == STAFF_PIN
    return False


def _new_order_id() -> str:
    """Уникальный номер заказа: ORD-ЧЧММСС, при коллизии — суффикс -2, -3, ..."""
    base = datetime.now().strftime('%H%M%S')
    oid = f"ORD-{base}"
    n = 1
    while db.execute("SELECT 1 FROM active_orders WHERE order_id = ?", (oid,)).fetchone():
        n += 1
        oid = f"ORD-{base}-{n}"
    return oid


def _copy_discount_to_order(src_order_id: str, dst_order_id: str):
    """При сплите перенести актуальную скидку на новый заказ (итоги считаются по discounts)."""
    row = db.execute(
        "SELECT percent, set_by_oper_id FROM discounts WHERE order_id = ? ORDER BY ts DESC LIMIT 1",
        (src_order_id,)
    ).fetchone()
    if row:
        db.execute(
            "INSERT INTO discounts (order_id, percent, set_by_oper_id) VALUES (?, ?, ?)",
            (dst_order_id, float(row["percent"]), row["set_by_oper_id"])
        )


def _require_superuser(oper_id) -> str:
    """Админ-эндпоинты: только суперпользователи (ID 0/52/77)."""
    op = (oper_id or "").strip() if oper_id else ""
    if not is_superuser(op):
        raise HTTPException(403, detail="Доступно только суперпользователю (ID 0/52/77)")
    return op

def _ensure_not_served(item_row: dict):
    if item_row.get('status') == 'served':
        raise HTTPException(400, detail="Позиция уже выдана (served) — операция запрещена")

# ------------------------------
# UI и справочники
# ------------------------------

@router.get("/ui/{template_id}")
def get_ui(template_id: str, oper_id: str = Query(None)):
    tpl_key = normalize_template(template_id)
    tpl_name = tpl_key + ".html"
    tpl_path = TEMPLATES_DIR / tpl_name
    if not tpl_path.exists():
        tpl_path = TEMPLATES_DIR / "default.html"
    if not tpl_path.exists():
        raise HTTPException(404, detail="Шаблон не найден")
    op = (oper_id or "").strip()
    if op and not can_access_form(op, tpl_key):
        raise HTTPException(403, detail=f"Оператор {op} не имеет доступа к форме {tpl_key}")
    with open(tpl_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@router.get("/access/info")
def access_info(oper_id: str = Query(...)):
    row = db.execute(
        "SELECT oper_id, oper, name, lastname FROM oper WHERE oper_id = ?", (oper_id.strip(),)
    ).fetchone()
    if not row:
        raise HTTPException(404, detail="Оператор не найден")
    op = dict(row)
    perms = operator_permissions(op["oper_id"], op.get("lastname"))
    perms["oper"] = op.get("oper")
    perms["name"] = op.get("name")
    return JSONResponse(perms)

@router.get("/menu")
def get_full_menu(oper_id: str = Query(None)):
    rows = db.execute(
        "SELECT name_id, name, unit, price, description, category FROM menu WHERE is_active = 1 ORDER BY CAST(name_id AS INTEGER)"
    ).fetchall()
    items = [dict(r) for r in rows]
    op = (oper_id or "").strip()
    if op:
        items = filter_menu_for_operator(items, op)
    return JSONResponse(items)

# ------------------------------
# QR-скан
# ------------------------------

def _sanitize_qr_scan(raw: str) -> str:
    s = (raw or "").strip()
    while s and s[-1] in "!.,;:":
        s = s[:-1]
    return s


@router.get("/scan_qr")
def scan_qr_endpoint(scan_data: str = Query(...)):
    scan_data = _sanitize_qr_scan(scan_data)
    ref = generate_ref("SCAN", scan_data)
    parts = scan_data.split("|")
    if len(parts) < 4 or parts[0] != "QR":
        return JSONResponse({"success": False, "error": "INVALID_FORMAT", "ref": ref})
    oper_id, role, template_id = parts[1], parts[2], parts[3]
    tpl_key = normalize_template(template_id)
    row = db.execute("SELECT oper_id, oper, name, lastname, qr_data FROM oper WHERE oper_id = ?", (oper_id,)).fetchone()
    if not row:
        return JSONResponse({"success": False, "error": "OPERATOR_NOT_FOUND", "ref": ref})
    operator = dict(row)
    if not can_access_form(oper_id, tpl_key):
        return JSONResponse({
            "success": False,
            "error": "FORM_ACCESS_DENIED",
            "detail": f"Оператор {oper_id} не может открыть форму {tpl_key}",
            "allowed_forms": allowed_forms(oper_id),
            "ref": ref,
        })
    perms = operator_permissions(oper_id, operator.get("lastname"))
    return JSONResponse({
        "success": True,
        "operator": operator,
        "ui": tpl_key,
        "permissions": perms,
        "ref": ref,
    })

# ------------------------------
# Заказы: создание / получение / статусы
# ------------------------------

@router.post("/orders/create")
def create_order(oper_id: str = Form(...), table_id: str = Form(...), items_json: str = Form(...)):
    op = (oper_id or "").strip()
    if not can_create_order(op):
        raise HTTPException(403, detail=f"Оператор {op} не может создавать заказы")

    try:
        items = json.loads(items_json)
    except Exception:
        raise HTTPException(400, detail="items_json должен быть валидным JSON")
    if not items:
        raise HTTPException(400, detail="Пустой заказ")
    if not _is_allowed_table_id(table_id):
        raise HTTPException(400, detail=f"Стол {table_id} вне диапазона 1–{get_max_table()}")

    for i in items:
        ok, msg = can_order_item(op, i.get("name_id"))
        if not ok:
            raise HTTPException(403, detail=msg)

    op_row = db.execute("SELECT lastname FROM oper WHERE oper_id = ?", (op,)).fetchone()
    auto_discount = get_operator_discount_pct(op, op_row["lastname"] if op_row else None)

    default_pickup = get_pickup_by_id(op)
    # Линии для Чиров (401–450): весь заказ на одну линию; при 2-й линии — баланс ~50/50.
    is_chiry = any(401 <= int(i.get('name_id', 0)) <= 450 for i in items)
    prod_line = _assign_chiry_production_line() if is_chiry else 1

    order_id = _new_order_id()
    sql = """
        INSERT INTO active_orders (
            order_id, oper_id, table_id, name_id, name, qty, price,
            production_line, pickup_point, status, stage, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', 1, datetime('now'))
    """
    data = [
        (
            order_id, op, table_id,
            str(i.get('name_id')), str(i.get('name')), int(i.get('qty', 1)),
            float(i.get('price', 0.0)),
            prod_line, get_pickup_for_item(op, i.get('name_id'))
        )
        for i in items
    ]
    try:
        db.executemany(sql, data)
        if auto_discount > 0:
            db.execute(
                "INSERT INTO discounts (order_id, percent, set_by_oper_id) VALUES (?, ?, ?)",
                (order_id, auto_discount, op)
            )
        db.commit()
        pickups = {row[-1] for row in data}
        return JSONResponse({
            "order_id": order_id,
            "pickup": default_pickup,
            "pickups": sorted(pickups),
            "line": prod_line,
            "auto_discount_pct": auto_discount if auto_discount > 0 else None,
        })
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка создания заказа: {e}")

@router.get("/orders/active")
def get_active_orders(
    station: str = Query(None),
    oper_id: str = Query(None),
    line: str = Query("all"),
    view: str = Query(None),
):
    op = (oper_id or "").strip()
    allowed = get_station_by_id(op)
    pickup = get_pickup_by_id(op)

    q = "SELECT * FROM active_orders WHERE status != 'served'"
    params = []

    if is_waiter_serve_view(op, view):
        # Официант: все готовые к подаче, включая Чиры после 3-го этапа
        q += """ AND (
            status = 'ready'
            OR (
                CAST(name_id AS INTEGER) BETWEEN 401 AND 450
                AND stage >= 3
                AND status IN ('ready', 'working')
            )
        )"""
    elif is_pickup_queue_view(op, station):
        q += " AND status = 'ready'"
        if not is_superuser(op):
            q += " AND pickup_point = ?"
            params.append(pickup)
    else:
        chiry_ls = get_chiry_line_stage(op)
        eff_line = line
        if chiry_ls and station == "chiry":
            eff_line = str(chiry_ls[0])
        if eff_line and eff_line != "all":
            q += " AND production_line = ?"
            params.append(int(eff_line))
        if chiry_ls and station == "chiry":
            q += " AND stage = ?"
            params.append(chiry_ls[1])
        # фильтр по станции
        if station:
            if allowed != 'ALL' and allowed != station and not (allowed == 'bar' and station == 'bar_all'):
                return JSONResponse([])
            if station in STATION_RANGES:
                mn, mx = STATION_RANGES[station]
                q += " AND CAST(name_id AS INTEGER) BETWEEN ? AND ?"
                params.extend([mn, mx])

    q += " ORDER BY created_at"
    rows = db.execute(q, params).fetchall()
    return JSONResponse([dict(r) for r in rows])

@router.get("/waiter/tables")
def waiter_table_status(oper_id: str = Query(...)):
    """Столы 1..Max_Table: занятость, %, вызов, оплата, готовность к расчёту."""
    migrate_v3()
    op = (oper_id or "").strip()
    if not is_superuser(op) and not can_access_form(op, "waiter_pos"):
        raise HTTPException(403, detail="Нет доступа к пульту официанта")

    max_n = get_max_table()
    rows = db.execute("""
        SELECT order_id, table_id, qty, status
        FROM active_orders
        WHERE status NOT IN ('cancelled', 'closed')
        ORDER BY CAST(table_id AS INTEGER), order_id
    """).fetchall()

    tables: dict[str, dict] = {}
    for r in rows:
        tid = str(r["table_id"] or "").strip() or "—"
        if tid not in tables:
            tables[tid] = {"table_id": tid, "total_qty": 0.0, "served_qty": 0.0, "order_ids": set()}
        qty = float(r["qty"] or 0)
        tables[tid]["total_qty"] += qty
        if r["status"] == "served":
            tables[tid]["served_qty"] += qty
        tables[tid]["order_ids"].add(r["order_id"])

    sig_rows = db.execute("SELECT table_id, calling, paying FROM table_signals").fetchall()
    signals = {str(r["table_id"]): r for r in sig_rows}

    result = []
    for i in range(1, max_n + 1):
        tid = str(i)
        t = tables.get(tid, {"table_id": tid, "total_qty": 0.0, "served_qty": 0.0, "order_ids": set()})
        total = t["total_qty"]
        served = t["served_qty"]
        pct = int(round(100 * served / total)) if total > 0 else 0
        fully = total > 0 and served >= total - 1e-9
        order_ids = sorted(t["order_ids"])
        due_total = sum(_get_order_totals(oid)["due"] for oid in order_ids) if order_ids else 0.0
        sig = signals.get(tid)
        result.append({
            "table_id": tid,
            "occupied": total > 0,
            "order_ids": order_ids,
            "receipt_count": len(order_ids),
            "total_qty": int(total),
            "served_qty": int(served),
            "issued_pct": pct,
            "fully_served": fully,
            "calling": bool(sig and int(sig["calling"] or 0)),
            "paying": bool(sig and int(sig["paying"] or 0)),
            "due_total": round(due_total, 2),
        })
    return JSONResponse(result)

@router.post("/waiter/ack-signal")
def waiter_ack_signal(
    table_id: str = Form(...),
    kind: str = Form(default="all"),
    oper_id: str = Form(default=""),
):
    op = (oper_id or "").strip()
    if op and not is_superuser(op) and not can_access_form(op, "waiter_pos"):
        raise HTTPException(403, detail="Нет доступа")
    k = (kind or "all").strip().lower()
    if k not in {"call", "pay", "all"}:
        raise HTTPException(400, detail="kind: call|pay|all")
    migrate_v3()
    _clear_table_signals(table_id, k)
    db.commit()
    return JSONResponse({"status": "ok", "table_id": table_id.strip(), "kind": k})

@router.post("/orders/status")
def update_order_status(
    item_id: int = Form(...),
    new_status: str = Form(...),
    oper_id: str = Form(default="system"),
    reason: str = Form(None)  # ← добавили
):
    row = db.execute("SELECT * FROM active_orders WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail="Позиция не найдена")
    item = dict(row)
    name_id = int(str(item.get("name_id","0"))) if str(item.get("name_id","0")).isdigit() else 0
    stage = int(item.get("stage", 1))
    op = (oper_id or "").strip()
    allowed = get_station_by_id(op)

    is_chiry = 401 <= name_id <= 450
    is_il    = 601 <= name_id <= 700

    try:
        if is_chiry or is_il:
            if op in SUPERUSERS or allowed == 'ALL':
                if new_status == 'served':
                    stage = 3
                elif is_chiry and stage == 1 and new_status == 'working':
                    stage = 2
                elif is_chiry and stage == 2 and new_status == 'ready':
                    stage, new_status = 3, 'ready'
            else:
                if is_chiry:
                    if op in {'65','66','67'} and stage == 1 and new_status == 'working':
                        stage, new_status = 2, 'working'
                    elif op in {'68','69','70'} and stage == 2 and new_status == 'ready':
                        stage, new_status = 3, 'ready'
                    elif op in {'71','72','73'} and stage == 1 and new_status == 'working':
                        stage, new_status = 2, 'working'
                    elif op in {'74','75','76'} and stage == 2 and new_status == 'ready':
                        stage, new_status = 3, 'ready'
                    elif new_status == 'served' and stage >= 3:
                        pass
                    else:
                        raise HTTPException(403, detail="⛔ Неверный этап/оператор для Чиров")
                elif is_il:
                    if stage == 1 and new_status == 'working':
                        stage = 2
                    elif stage == 2 and new_status == 'ready':
                        stage = 3
                    elif new_status == 'served' and stage >= 3:
                        pass
                    else:
                        raise HTTPException(403, detail="⛔ Неверный этап/оператор для ОкноГриль")
        else:
            if new_status == 'served':
                stage = 1

        # Чиры на 3-м этапе — готовы к подаче официантом
        if is_chiry and stage >= 3 and new_status not in ('served', 'cancelled', 'closed'):
            new_status = 'ready'

        db.execute("UPDATE active_orders SET status=?, stage=? WHERE id=?", (new_status, stage, item_id))
        db.commit()
        # Безопасная запись события (с «reason», если передан)
        try:
            ev = {"status": new_status, "stage": stage}
            if reason: ev["reason"] = reason
            db.execute("""
                INSERT INTO order_events(order_id, item_id, event_type, old_val, new_val, oper_id, pin_ok)
                VALUES(?, ?, 'status_change', NULL, ?, ?, 1)
            """, (item["order_id"], item_id, json.dumps(ev, ensure_ascii=False), op))
            db.commit()
        except Exception as ev_err:
            print(f"[WARN] order_events insert failed: {ev_err}")
        return JSONResponse({"status": new_status, "stage": stage, "item_id": item_id})
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка смены статуса: {e}")

# ------------------------------
# Отмена/редактирование/сплит (с PIN)
# ------------------------------

# PINы/хелперы объявлены выше: ROOT_PIN/STAFF_PIN/_verify_pin/_ensure_not_served

@router.post("/orders/cancel_item")
def cancel_item(item_id: int = Form(...), oper_id: str = Form(...), pin: str = Form(...), reason: str = Form(None)):
    if oper_id not in ALLOWED_CANCEL_IDS and oper_id not in SUPERUSERS:
        raise HTTPException(403, detail="Нет прав на отмену позиции")
    if not _verify_pin(oper_id, pin):
        raise HTTPException(403, detail="Неверный PIN")
    row = db.execute("SELECT * FROM active_orders WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail="Позиция не найдена")
    item = dict(row)
    _ensure_not_served(item)

    try:
        db.execute("BEGIN")
        db.execute("UPDATE active_orders SET status = 'cancelled' WHERE id = ?", (item_id,))
        db.execute("""
            INSERT INTO order_events (order_id, item_id, event_type, old_val, new_val, oper_id, pin_ok)
            VALUES (?, ?, 'cancel', ?, ?, ?, 1)
        """, (
            item['order_id'], item_id,
            json.dumps({"status": item["status"]}, ensure_ascii=False),
            json.dumps({"status": "cancelled", "reason": reason or ""}, ensure_ascii=False),
            oper_id
        ))
        db.commit()
        return JSONResponse({"status": "cancelled", "item_id": item_id})
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка отмены позиции: {e}")

@router.post("/orders/cancel_order")
def cancel_order(order_id: str = Form(...), oper_id: str = Form(...), pin: str = Form(...), reason: str = Form(None)):
    if oper_id not in ALLOWED_CANCEL_IDS and oper_id not in SUPERUSERS:
        raise HTTPException(403, detail="Нет прав на отмену заказа")
    if not _verify_pin(oper_id, pin):
        raise HTTPException(403, detail="Неверный PIN")

    rows = db.execute("SELECT id, status FROM active_orders WHERE order_id = ?", (order_id,)).fetchall()
    if not rows:
        raise HTTPException(404, detail="Заказ не найден")
    if any(r["status"] == "served" for r in rows):
        raise HTTPException(400, detail="Часть позиций уже выдана — отмена заказа запрещена")

    try:
        db.execute("BEGIN")
        db.execute("UPDATE active_orders SET status = 'cancelled' WHERE order_id = ?", (order_id,))
        db.execute("""
            INSERT INTO order_events (order_id, item_id, event_type, old_val, new_val, oper_id, pin_ok)
            VALUES (?, NULL, 'cancel', ?, ?, ?, 1)
        """, (
            order_id,
            json.dumps({"items": len(rows)}, ensure_ascii=False),
            json.dumps({"status": "cancelled", "reason": reason or ""}, ensure_ascii=False),
            oper_id
        ))
        db.commit()
        return JSONResponse({"status": "cancelled", "order_id": order_id, "items": len(rows)})
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка отмены заказа: {e}")

@router.post("/orders/edit_item")
def edit_item(
    item_id: int = Form(...),
    new_qty: int = Form(...),
    oper_id: str = Form(...),
    pin: str = Form(...)
):
    if new_qty <= 0:
        raise HTTPException(400, detail="Количество должно быть > 0")
    if oper_id not in ALLOWED_CANCEL_IDS and oper_id not in SUPERUSERS:
        raise HTTPException(403, detail="Нет прав на редактирование")
    if not _verify_pin(oper_id, pin):
        raise HTTPException(403, detail="Неверный PIN")

    row = db.execute("SELECT * FROM active_orders WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail="Позиция не найдена")
    it = dict(row)
    _ensure_not_served(it)

    try:
        db.execute("BEGIN")
        db.execute("UPDATE active_orders SET qty = ? WHERE id=?", (new_qty, item_id))
        db.execute("""
            INSERT INTO order_events (order_id, item_id, event_type, old_val, new_val, oper_id, pin_ok)
            VALUES (?, ?, 'edit', ?, ?, ?, 1)
        """, (
            it["order_id"], item_id,
            json.dumps({"qty": it["qty"]}, ensure_ascii=False),
            json.dumps({"qty": new_qty}, ensure_ascii=False),
            oper_id
        ))
        db.commit()
        return JSONResponse({"status": "ok", "item_id": item_id, "new_qty": new_qty})
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка редактирования: {e}")

@router.post("/orders/split_items")
def split_items(
    order_id: str = Form(...),
    item_ids_json: str = Form(...),   # JSON: [1,5,9]
    oper_id: str = Form(...),
    pin: str = Form(...)
):
    if oper_id not in ALLOWED_CANCEL_IDS and oper_id not in SUPERUSERS:
        raise HTTPException(403, detail="Нет прав на разделение")
    if not _verify_pin(oper_id, pin):
        raise HTTPException(403, detail="Неверный PIN")

    try:
        item_ids = json.loads(item_ids_json)
        if not isinstance(item_ids, list) or not item_ids:
            raise ValueError
    except Exception:
        raise HTTPException(400, detail="item_ids_json должен быть JSON-массивом ID")

    rows = db.execute("SELECT * FROM active_orders WHERE id IN ({seq}) AND order_id=?".format(
        seq=",".join(["?"] * len(item_ids))
    ), (*item_ids, order_id)).fetchall()
    if not rows:
        raise HTTPException(404, detail="Позиции не найдены")

    new_order_id = _new_order_id()
    try:
        db.execute("BEGIN")
        for r in rows:
            it = dict(r)
            if it["status"] == "served":
                raise HTTPException(400, detail=f"Позиция {it['id']} уже выдана")
            db.execute("UPDATE active_orders SET order_id=? WHERE id=?", (new_order_id, it["id"]))
        _copy_discount_to_order(order_id, new_order_id)
        db.execute("""
            INSERT INTO order_events(order_id, item_id, event_type, old_val, new_val, oper_id, pin_ok)
            VALUES(?, NULL, 'split', ?, ?, ?, 1)
        """, (
            order_id,
            json.dumps({"moved": item_ids}, ensure_ascii=False),
            json.dumps({"new_order_id": new_order_id}, ensure_ascii=False),
            oper_id
        ))
        db.commit()
        return JSONResponse({"status": "ok", "new_order_id": new_order_id, "moved": item_ids})
    except HTTPException:
        db.rollback(); raise
    except Exception as e:
        db.rollback(); raise HTTPException(500, detail=f"Ошибка split_items: {e}")

@router.post("/orders/split_qty")
def split_qty(
    item_id: int = Form(...),
    qty_to_move: int = Form(...),
    oper_id: str = Form(...),
    pin: str = Form(...)
):
    if oper_id not in ALLOWED_CANCEL_IDS and oper_id not in SUPERUSERS:
        raise HTTPException(403, detail="Нет прав")
    if not _verify_pin(oper_id, pin):
        raise HTTPException(403, detail="Неверный PIN")
    if qty_to_move <= 0:
        raise HTTPException(400, detail="qty_to_move должен быть > 0")

    row = db.execute("SELECT * FROM active_orders WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail="Позиция не найдена")
    it = dict(row)
    if it["status"] == "served":
        raise HTTPException(400, detail="Позиция уже выдана")
    if qty_to_move > int(it["qty"]):
        raise HTTPException(400, detail="qty_to_move больше, чем в позиции")

    new_order_id = _new_order_id()
    try:
        db.execute("BEGIN")
        db.execute("UPDATE active_orders SET qty = qty - ? WHERE id=?", (qty_to_move, item_id))
        db.execute("""
            INSERT INTO active_orders(
                order_id, oper_id, table_id, name_id, name, qty, price, status, stage,
                production_line, pickup_point, discount_pct, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            new_order_id, it["oper_id"], it["table_id"], it["name_id"], it["name"],
            qty_to_move, it["price"], it["status"], it.get("stage", 1),
            it.get("production_line", 1), it.get("pickup_point", "bar"), it.get("discount_pct", 0.0)
        ))
        _copy_discount_to_order(it["order_id"], new_order_id)
        db.execute("""
            INSERT INTO order_events(order_id, item_id, event_type, old_val, new_val, oper_id, pin_ok)
            VALUES(?, ?, 'split', ?, ?, ?, 1)
        """, (
            it["order_id"], item_id,
            json.dumps({"moved_qty": qty_to_move}, ensure_ascii=False),
            json.dumps({"new_order_id": new_order_id}, ensure_ascii=False),
            oper_id
        ))
        db.commit()
        return JSONResponse({"status": "ok", "new_order_id": new_order_id, "moved_qty": qty_to_move})
    except Exception as e:
        db.rollback(); raise HTTPException(500, detail=f"Ошибка split_qty: {e}")
# ------------------------------
# Подсчёт итогов заказа (subtotal/discount/paid/due/tips)
# ------------------------------
def _get_order_totals(order_id: str) -> dict:
    rows = db.execute(
        "SELECT qty, price, status FROM active_orders WHERE order_id = ?",
        (order_id,)
    ).fetchall()
    subtotal = 0.0
    for r in rows:
        q = float(r["qty"] or 0)
        p = float(r["price"] or 0.0)
        if r["status"] != "cancelled":
            subtotal += q * p

    # скидка (последняя по времени)
    disc_row = db.execute(
        "SELECT percent FROM discounts WHERE order_id = ? ORDER BY ts DESC LIMIT 1",
        (order_id,)
    ).fetchone()
    discount_pct = float(disc_row["percent"]) if disc_row else 0.0
    total_after_discount = max(subtotal * (1.0 - discount_pct / 100.0), 0.0)

    # оплачено
    paid = db.execute(
        "SELECT COALESCE(SUM(amount),0) AS paid_sum FROM payments WHERE order_id = ?",
        (order_id,)
    ).fetchone()[0]
    paid = float(paid or 0.0)

    # чаевые
    tip_total = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM tips WHERE order_id = ?",
        (order_id,)
    ).fetchone()[0]
    tip_total = float(tip_total or 0.0)

    due = max(total_after_discount - paid, 0.0)
    return {
        "subtotal": round(subtotal, 2),
        "discount_pct": discount_pct,
        "total": round(total_after_discount, 2),
        "paid": round(paid, 2),
        "due": round(due, 2),
        "tips": round(tip_total, 2),
    }

# ------------------------------
# Платежи / Чаевые / Скидки / Сводки / Закрытие заказа
# ------------------------------
@router.get("/orders/summary_by_id")
def order_summary_by_id(order_id: str = Query(...)):
    any_row = db.execute("SELECT 1 FROM active_orders WHERE order_id = ? LIMIT 1", (order_id,)).fetchone()
    if not any_row:
        raise HTTPException(404, detail="Заказ не найден")
    return JSONResponse(_get_order_totals(order_id))

@router.post("/payments/add")
def add_payment(
    order_id: str = Form(...),
    method: str = Form(...),   # cash|card|transfer
    amount: float = Form(...),
    oper_id: str = Form(None)
):
    method = (method or "").strip().lower()
    if method not in {"cash", "card", "transfer"}:
        raise HTTPException(400, detail="Метод оплаты: cash|card|transfer")
    try:
        amt = float(amount)
    except Exception:
        raise HTTPException(400, detail="Сумма должна быть числом")
    if amt <= 0:
        raise HTTPException(400, detail="Сумма должна быть > 0")

    any_row = db.execute("SELECT 1 FROM active_orders WHERE order_id = ? LIMIT 1", (order_id,)).fetchone()
    if not any_row:
        raise HTTPException(404, detail="Заказ не найден")

    totals = _get_order_totals(order_id)
    if amt > totals["due"] + 1e-6:
        raise HTTPException(400, detail=f"Сумма {amt} превышает остаток к оплате ({totals['due']})")
    try:
        db.execute(
            "INSERT INTO payments (order_id, method, amount, oper_id) VALUES (?, ?, ?, ?)",
            (order_id, method, amt, oper_id or "system")
        )
        db.execute(
            "INSERT INTO order_events (order_id, item_id, event_type, old_val, new_val, oper_id, pin_ok) VALUES (?, NULL, 'payment', ?, ?, ?, 1)",
            (
                order_id,
                json.dumps({"due_before": totals["due"]}, ensure_ascii=False),
                json.dumps({"paid": amt, "method": method}, ensure_ascii=False),
                oper_id or "system"
            )
        )
        db.commit()
        return JSONResponse({"status": "ok", "order_id": order_id, "method": method, "amount": round(amt, 2), "summary": _get_order_totals(order_id)})
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка записи оплаты: {e}")

@router.post("/tips/add")
def add_tip(order_id: str = Form(...), waiter_id: str = Form(...), amount: float = Form(...)):
    any_row = db.execute("SELECT 1 FROM active_orders WHERE order_id = ? LIMIT 1", (order_id,)).fetchone()
    if not any_row:
        raise HTTPException(404, detail="Заказ не найден")
    try:
        val = float(amount)
    except Exception:
        raise HTTPException(400, detail="Сумма чаевых должна быть числом")
    if val <= 0:
        raise HTTPException(400, detail="Сумма чаевых должна быть > 0")
    try:
        db.execute("INSERT INTO tips (order_id, waiter_id, amount) VALUES (?, ?, ?)", (order_id, waiter_id, val))
        db.execute(
            "INSERT INTO order_events (order_id, item_id, event_type, old_val, new_val, oper_id, pin_ok) VALUES (?, NULL, 'tip_add', NULL, ?, ?, 1)",
            (order_id, json.dumps({"tips": val, "waiter_id": waiter_id}, ensure_ascii=False), waiter_id)
        )
        db.commit()
        return JSONResponse({"status": "ok", "order_id": order_id, "waiter_id": waiter_id, "amount": round(val, 2), "summary": _get_order_totals(order_id)})
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка записи чаевых: {e}")

@router.post("/discounts/set")
def set_discount(
    order_id: str = Form(...),
    percent: float = Form(...),
    oper_id: str = Form(...),
    pin: str = Form(...)
):
    op = (oper_id or "").strip()
    if not can_set_manual_discount(op):
        raise HTTPException(403, detail="Нет прав на установку скидки")

    op_row = db.execute("SELECT lastname FROM oper WHERE oper_id = ?", (op,)).fetchone()
    max_pct = max_manual_discount_pct(op, op_row["lastname"] if op_row else None)

    needs_pin = op in ALLOWED_CANCEL_IDS or is_superuser(op)
    if needs_pin and not _verify_pin(op, pin):
        raise HTTPException(403, detail="Неверный PIN")

    any_row = db.execute("SELECT 1 FROM active_orders WHERE order_id = ? LIMIT 1", (order_id,)).fetchone()
    if not any_row:
        raise HTTPException(404, detail="Заказ не найден")

    try:
        p = float(percent)
    except Exception:
        raise HTTPException(400, detail="Процент скидки должен быть числом")
    if p < 0 or p > 100:
        raise HTTPException(400, detail="Процент скидки должен быть в диапазоне 0..100")
    if p > max_pct + 1e-6:
        raise HTTPException(403, detail=f"Максимальная скидка для оператора {op}: {max_pct}%")

    try:
        db.execute(
            "INSERT INTO discounts (order_id, percent, set_by_oper_id) VALUES (?, ?, ?)",
            (order_id, p, oper_id)
        )
        db.execute(
            "INSERT INTO order_events (order_id, item_id, event_type, old_val, new_val, oper_id, pin_ok) VALUES (?, NULL, 'discount', NULL, ?, ?, 1)",
            (order_id, json.dumps({"discount_pct": p}, ensure_ascii=False), oper_id)
        )
        db.commit()
        return JSONResponse({"status": "ok", "order_id": order_id, "discount_pct": p, "summary": _get_order_totals(order_id)})
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка установки скидки: {e}")

@router.post("/orders/settle")
def settle_order(order_id: str = Form(...)):
    # Проверка, что заказ есть
    any_row = db.execute("SELECT 1 FROM active_orders WHERE order_id = ? LIMIT 1", (order_id,)).fetchone()
    if not any_row:
        raise HTTPException(404, detail="Заказ не найден")

    totals = _get_order_totals(order_id)
    if totals["due"] > 1e-6:
        raise HTTPException(400, detail=f"Не полностью оплачено. Остаток к оплате: {totals['due']}")

    # Разбивка оплат по методам
    pay_rows = db.execute(
        "SELECT method, SUM(amount) AS sum FROM payments WHERE order_id = ? GROUP BY method",
        (order_id,)
    ).fetchall()
    payments = {str(r["method"]): float(r["sum"] or 0.0) for r in pay_rows}

    try:
        db.execute("BEGIN")
        # Закрываем все непогашенные позиции (кроме отменённых остаются cancelled)
        db.execute("""
            UPDATE active_orders
            SET status = CASE WHEN status = 'cancelled' THEN status ELSE 'closed' END
            WHERE order_id = ?
        """, (order_id,))
        # Событие settle
        db.execute("""
            INSERT INTO order_events (order_id, item_id, event_type, old_val, new_val, oper_id, pin_ok)
            VALUES (?, NULL, 'settle', ?, ?, ?, 1)
        """, (
            order_id,
            json.dumps({"due_before": 0.0}, ensure_ascii=False),
            json.dumps({"total": totals["total"], "paid": totals["paid"], "by_method": payments}, ensure_ascii=False),
            "system"
        ))
        db.commit()
        return JSONResponse({"status": "settled", "order_id": order_id, "payments": payments, "summary": _get_order_totals(order_id)})
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка закрытия заказа: {e}")

# ------------------------------
# Гость: чек стола, вызов официанта
# ------------------------------

def _guest_table_items(table_id: str) -> list[dict]:
    # Включаем closed: гость должен видеть все чеки, а due при частичных оплатах считается корректно
    rows = db.execute("""
        SELECT order_id, name_id, name, qty, price, status, created_at
        FROM active_orders
        WHERE table_id = ? AND status != 'cancelled'
        ORDER BY created_at, id
    """, (table_id.strip(),)).fetchall()
    return [dict(r) for r in rows]


@router.get("/guest/bill")
def guest_table_bill(table_id: str = Query(...)):
    tid = (table_id or "").strip()
    if not tid:
        raise HTTPException(400, detail="Укажите table_id")
    items = _guest_table_items(tid)
    order_ids = sorted({i["order_id"] for i in items})
    subtotal = 0.0
    for i in items:
        if i.get("status") == "cancelled":
            continue
        subtotal += float(i.get("qty") or 0) * float(i.get("price") or 0)
    paid = 0.0
    for oid in order_ids:
        paid += _get_order_totals(oid)["paid"]
    due = max(subtotal - paid, 0.0)
    return JSONResponse({
        "table_id": tid,
        "order_ids": order_ids,
        "items": items,
        "subtotal": round(subtotal, 2),
        "paid": round(paid, 2),
        "due": round(due, 2),
    })


@router.post("/call/waiter")
def call_waiter(
    table_id: str = Form(...),
    oper_id: str = Form(default=""),
    kind: str = Form(default="call"),
    order_id: str = Form(default=""),
):
    tid = (table_id or "").strip()
    if not tid:
        raise HTTPException(400, detail="Укажите номер стола")
    kind_norm = (kind or "call").strip().lower()
    if kind_norm not in {"call", "pay"}:
        raise HTTPException(400, detail="kind: call или pay")
    op = (oper_id or tid).strip()
    bill = _guest_table_items(tid)
    due = 0.0
    order_ids = sorted({i["order_id"] for i in bill})
    for oid in order_ids:
        due += _get_order_totals(oid)["due"]
    payload = {
        "table_id": tid,
        "kind": kind_norm,
        "due": round(due, 2),
        "order_ids": order_ids,
    }
    if order_id:
        payload["order_id"] = order_id.strip()
    try:
        db.execute("""
            INSERT INTO order_events (order_id, item_id, event_type, old_val, new_val, oper_id, pin_ok)
            VALUES (?, NULL, 'waiter_call', NULL, ?, ?, 1)
        """, (
            order_id.strip() or (order_ids[-1] if order_ids else f"TABLE-{tid}"),
            json.dumps(payload, ensure_ascii=False),
            op,
        ))
        migrate_v3()
        _set_table_signal(tid, kind_norm)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка вызова: {e}")
    if kind_norm == "pay":
        msg = f"Запрос на оплату отправлен (к оплате: {due:.0f} ₽)"
    else:
        msg = "Официант вызван"
    return JSONResponse({"status": msg, "table_id": tid, "kind": kind_norm, "due": round(due, 2)})

# ------------------------------
# Брони столов (1 час)
# ------------------------------
@router.get("/reservations/free")
def reservations_free(res_date: str = Query(...), res_time: str = Query(...)):
    migrate_v3()
    try:
        _parse_res_dt(res_date, res_time)
    except ValueError:
        raise HTTPException(400, detail="Дата/время: YYYY-MM-DD и HH:MM")
    max_n = get_max_table()
    busy = _booked_table_ids(res_date, res_time)
    free = [str(i) for i in range(1, max_n + 1) if str(i) not in busy]
    return JSONResponse({"free": free, "max_table": max_n, "duration_min": RESERVE_MINUTES})

@router.get("/reservations")
def reservations_list(day: str = Query(default="")):
    migrate_v3()
    d = (day or "").strip() or datetime.now().strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT id, table_id, res_date, res_time, duration_min, phone, guest_name, created_by, created_at,
               guests_count, is_closed_event, advance_amount, advance_method, order_json, parent_res_id,
               status, cancel_notify_time
        FROM reservations
        WHERE res_date = ?
        ORDER BY res_time, CAST(table_id AS INTEGER)
    """, (d,)).fetchall()
    return JSONResponse({"day": d, "items": [dict(r) for r in rows]})

@router.post("/reservations")
def reservations_create(
    table_id: str = Form(...),
    res_date: str = Form(...),
    res_time: str = Form(...),
    phone: str = Form(...),
    guest_name: str = Form(...),
    oper_id: str = Form(default=""),
):
    migrate_v3()
    tid = (table_id or "").strip()
    d = (res_date or "").strip()
    t = (res_time or "").strip()
    if len(t) >= 5:
        t = t[:5]
    name = (guest_name or "").strip()
    phone_n = (phone or "").strip()
    if not tid or not d or not t:
        raise HTTPException(400, detail="Нужны стол, дата и время")
    if not name or not phone_n:
        raise HTTPException(400, detail="Укажите телефон и имя гостя")
    try:
        start = _parse_res_dt(d, t)
    except ValueError:
        raise HTTPException(400, detail="Дата/время: YYYY-MM-DD и HH:MM")
    if start < datetime.now() - timedelta(minutes=5):
        raise HTTPException(400, detail="Нельзя бронировать прошедшее время")
    max_n = get_max_table()
    if not tid.isdigit() or not (1 <= int(tid) <= max_n):
        raise HTTPException(400, detail=f"Стол вне 1–{max_n}")
    if tid in _booked_table_ids(d, t):
        raise HTTPException(400, detail=f"Стол {tid} уже занят бронью на этот час")
    try:
        cur = db.execute("""
            INSERT INTO reservations (table_id, res_date, res_time, duration_min, phone, guest_name, created_by, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'Активна')
        """, (tid, d, t, RESERVE_MINUTES, phone_n, name, (oper_id or "").strip()))
        db.commit()
        return JSONResponse({
            "status": "ok",
            "id": cur.lastrowid,
            "table_id": tid,
            "res_date": d,
            "res_time": t,
            "duration_min": RESERVE_MINUTES,
        })
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка брони: {e}")

@router.get("/reservations/reminders")
def reservations_reminders():
    """Брони сегодня + напоминания (за сутки, утро, 2 часа) + конфликты закрытых банкетов + СМС/звонок."""
    migrate_v3()
    migrate_v5()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT id, table_id, res_date, res_time, duration_min, phone, guest_name,
               guests_count, is_closed_event, status, parent_res_id
        FROM reservations
        WHERE res_date = ? OR res_date = ?
        ORDER BY res_date, res_time
    """, (today, (now + timedelta(days=1)).strftime("%Y-%m-%d"))).fetchall()
    today_items = []
    due_soon = []
    day_before = []
    morning = []
    due_soon_2h = []
    closed_masters = []
    for r in rows:
        if (r["status"] or "Активна") == "Отменен":
            continue
        if int(r["is_closed_event"] or 0) and r["parent_res_id"]:
            continue  # служебные строки закрытого банкета не показываем
        item = dict(r)
        try:
            start = _parse_res_dt(r["res_date"], r["res_time"])
        except ValueError:
            continue
        minutes = int((start - now).total_seconds() / 60)
        item["minutes_until"] = minutes
        if r["res_date"] == today and start >= now.replace(hour=0, minute=0, second=0, microsecond=0):
            today_items.append(item)
        if 0 <= minutes <= 60:
            due_soon.append(item)
        if r["res_date"] == today and minutes >= 0:
            morning.append(item)
        if r["res_date"] != today and minutes > 60:
            day_before.append(item)
        if 0 <= minutes <= 120:
            due_soon_2h.append(item)
        if int(r["is_closed_event"] or 0) and not r["parent_res_id"]:
            closed_masters.append((item, start))

    # Конфликты: обычные брони, пересекающиеся с закрытым банкетом
    cancel_requests = []
    used_ids = set()
    for m, mstart in closed_masters:
        for r in rows:
            if (r["status"] or "Активна") == "Отменен":
                continue
            if int(r["is_closed_event"] or 0) or r["id"] in used_ids:
                continue
            if r["parent_res_id"] and int(r["parent_res_id"]) == m["id"]:
                continue
            try:
                other = _parse_res_dt(r["res_date"], r["res_time"])
            except ValueError:
                continue
            if _reservation_overlap(mstart, other):
                used_ids.add(r["id"])
                cancel_requests.append({
                    "id": r["id"], "banquet_id": m["id"], "banquet_table": m["table_id"],
                    "table_id": r["table_id"], "res_date": r["res_date"],
                    "res_time": str(r["res_time"])[:5], "phone": r["phone"] or "",
                    "guest_name": r["guest_name"] or "",
                })

    # Наступило время уведомления по отмене по инициативе кафе
    sms_due = []
    for r in db.execute("""
        SELECT id, table_id, res_date, res_time, phone, guest_name, cancel_notify_time
        FROM reservations
        WHERE status = 'Отменен' AND cancel_notify_time IS NOT NULL AND cancel_notify_time != ''
    """).fetchall():
        try:
            nt = datetime.strptime(r["cancel_notify_time"], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if now >= nt:
            sms_due.append({
                "id": r["id"], "table_id": r["table_id"], "res_date": r["res_date"],
                "res_time": str(r["res_time"])[:5], "phone": r["phone"] or "",
                "guest_name": r["guest_name"] or "",
            })

    return JSONResponse({"today": today_items, "due_soon": due_soon, "day": today,
                         "day_before": day_before, "morning": morning, "due_soon_2h": due_soon_2h,
                         "cancel_requests": cancel_requests, "sms_due": sms_due})


CANCEL_PINS = {"7777": "Директор", "4444": "Бармен"}


@router.post("/reservations/cancel")
def reservation_cancel(
    res_id: int = Form(...),
    pin: str = Form(...),
    initiator: str = Form(default="client"),
    notify_time: str = Form(default=""),
):
    """Отмена брони/банкета. Инициатор 'client' — без уведомлений; 'cafe' — с напоминанием СМС/звонок."""
    migrate_v5()
    if pin.strip() not in CANCEL_PINS:
        raise HTTPException(403, detail="Неверный ПИН (директор/бармен)")
    r = _res_row(res_id)
    if not r:
        raise HTTPException(404, detail="Бронь не найдена")
    role = CANCEL_PINS[pin.strip()]
    nt = ""
    if (initiator or "client").strip() == "cafe":
        nt = (notify_time or "").strip()
        if nt:
            try:
                datetime.strptime(nt, "%Y-%m-%d %H:%M")
            except ValueError:
                raise HTTPException(400, detail="Время уведомления: ГГГГ-ММ-ДД ЧЧ:ММ")
    db.execute("UPDATE reservations SET status = 'Отменен', cancel_notify_time = ? WHERE id = ?", (nt, res_id))
    db.commit()
    return JSONResponse({"status": "ok", "id": res_id, "reservation_status": "Отменен",
                         "cancelled_by": role, "notify_time": nt})


# ------------------------------
# Предзаказ при бронировании (банкет)
# ------------------------------
def _res_row(res_id: int) -> dict | None:
    r = db.execute("SELECT * FROM reservations WHERE id = ?", (res_id,)).fetchone()
    return dict(r) if r else None


def _validate_order_items(items_json: str) -> list[dict]:
    """Проверяет состав предзаказа и подставляет актуальные цены из меню."""
    try:
        raw = json.loads(items_json or "[]")
    except (ValueError, TypeError):
        raise HTTPException(400, detail="items_json: некорректный JSON")
    if not isinstance(raw, list):
        raise HTTPException(400, detail="items_json: ожидается список")
    result = []
    for it in raw:
        name_id = str(it.get("name_id", "")).strip()
        qty = int(it.get("qty", 0))
        if not name_id or qty <= 0:
            continue
        m = db.execute("SELECT name_id, name, price FROM menu WHERE name_id = ?", (name_id,)).fetchone()
        if not m:
            raise HTTPException(400, detail=f"Позиции {name_id} нет в меню")
        result.append({"name_id": m["name_id"], "name": m["name"], "price": float(m["price"]), "qty": qty})
    return result


@router.post("/reservations/order")
def reservation_order_create(
    table_id: str = Form(...),
    res_date: str = Form(...),
    res_time: str = Form(...),
    phone: str = Form(...),
    guest_name: str = Form(...),
    oper_id: str = Form(default=""),
    guests_count: int = Form(default=0),
    is_closed_event: int = Form(default=0),
    items_json: str = Form(default="[]"),
    advance_amount: float = Form(default=0.0),
    advance_method: str = Form(default=""),
    res_id: int = Form(default=0),
):
    """Бронь с предзаказом (банкет). При закрытом мероприятии занимается весь зал."""
    migrate_v5()
    tid = (table_id or "").strip()
    d = (res_date or "").strip()
    t = (res_time or "").strip()
    if len(t) >= 5:
        t = t[:5]
    name = (guest_name or "").strip()
    phone_n = (phone or "").strip()
    if not tid or not d or not t:
        raise HTTPException(400, detail="Нужны стол, дата и время")
    if not name or not phone_n:
        raise HTTPException(400, detail="Укажите телефон и имя гостя")
    try:
        start = _parse_res_dt(d, t)
    except ValueError:
        raise HTTPException(400, detail="Дата/время: YYYY-MM-DD и HH:MM")
    if start < datetime.now() - timedelta(minutes=5):
        raise HTTPException(400, detail="Нельзя бронировать прошедшее время")
    max_n = get_max_table()
    if not tid.isdigit() or not (1 <= int(tid) <= max_n):
        raise HTTPException(400, detail=f"Стол вне 1–{max_n}")
    items = _validate_order_items(items_json)
    total = round(sum(i["price"] * i["qty"] for i in items), 2)
    adv = round(float(advance_amount or 0), 2)
    if adv < 0:
        raise HTTPException(400, detail="Аванс не может быть отрицательным")
    closed = 1 if int(is_closed_event or 0) == 1 else 0
    if adv > total > 0:
        raise HTTPException(400, detail="Аванс больше суммы заказа")

    edit_id = int(res_id or 0)
    busy = _booked_table_ids(d, t, exclude_id=edit_id or None)
    if not closed and tid in busy:
        raise HTTPException(400, detail=f"Стол {tid} уже занят бронью на этот час")

    try:
        if edit_id:
            db.execute("""
                UPDATE reservations SET table_id=?, res_date=?, res_time=?, phone=?, guest_name=?,
                    guests_count=?, is_closed_event=?, order_json=?, advance_amount=?, advance_method=?
                WHERE id=?
            """, (tid, d, t, phone_n, name, int(guests_count or 0), closed,
                  json.dumps(items, ensure_ascii=False), adv, (advance_method or "").strip(), edit_id))
            db.execute("DELETE FROM reservations WHERE parent_res_id = ?", (edit_id,))
            master_id = edit_id
        else:
            cur = db.execute("""
                INSERT INTO reservations (table_id, res_date, res_time, duration_min, phone, guest_name,
                    created_by, guests_count, is_closed_event, order_json, advance_amount, advance_method)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (tid, d, t, RESERVE_MINUTES, phone_n, name, (oper_id or "").strip(),
                  int(guests_count or 0), closed, json.dumps(items, ensure_ascii=False),
                  adv, (advance_method or "").strip()))
            master_id = cur.lastrowid

        closed_tables = []
        if closed:
            # Закрытый банкет приоритетен: занимаем все столы, даже занятые ранее
            for i in range(1, max_n + 1):
                other = str(i)
                if other == tid:
                    continue
                db.execute("""
                    INSERT INTO reservations (table_id, res_date, res_time, duration_min, phone, guest_name,
                        created_by, guests_count, is_closed_event, parent_res_id, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'Активна')
                """, (other, d, t, RESERVE_MINUTES, phone_n, f"ЗАКРЫТОЕ МЕРОПРИЯТИЕ (стол {tid})",
                      (oper_id or "").strip(), int(guests_count or 0), master_id))
                closed_tables.append(other)
            db.execute("UPDATE reservations SET status = 'Активна' WHERE id = ?", (master_id,))
        db.commit()

        # Конфликты: активные брони, пересекающиеся с закрытым банкетом (не считая самого банкета)
        conflicts = []
        if closed:
            day_from = (start - timedelta(days=1)).strftime("%Y-%m-%d")
            day_to = (start + timedelta(days=1)).strftime("%Y-%m-%d")
            for r in db.execute(
                "SELECT id, table_id, res_date, res_time, phone, guest_name, parent_res_id FROM reservations "
                "WHERE res_date BETWEEN ? AND ? AND (status IS NULL OR status != 'Отменен')",
                (day_from, day_to),
            ).fetchall():
                if int(r["id"]) == master_id or (r["parent_res_id"] and int(r["parent_res_id"]) == master_id):
                    continue
                if _reservation_overlap(start, _parse_res_dt(r["res_date"], r["res_time"])):
                    conflicts.append({
                        "id": r["id"], "table_id": r["table_id"],
                        "res_date": r["res_date"], "res_time": str(r["res_time"])[:5],
                        "phone": r["phone"] or "", "guest_name": r["guest_name"] or "",
                    })

        return JSONResponse({
            "status": "ok", "id": master_id, "table_id": tid, "res_date": d, "res_time": t,
            "duration_min": RESERVE_MINUTES, "guests_count": int(guests_count or 0),
            "is_closed_event": closed, "total": total, "advance_amount": adv,
            "advance_method": (advance_method or "").strip(), "closed_tables": closed_tables,
            "conflicts": conflicts,
        })
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка брони с предзаказом: {e}")


@router.get("/reservations/order")
def reservation_order_get(res_id: int = Query(...)):
    """Бронь с составом предзаказа."""
    migrate_v5()
    r = _res_row(res_id)
    if not r:
        raise HTTPException(404, detail="Бронь не найдена")
    try:
        items = json.loads(r.get("order_json") or "[]")
    except ValueError:
        items = []
    total = round(sum(i.get("price", 0) * i.get("qty", 0) for i in items), 2)
    r["items"] = items
    r["total"] = total
    return JSONResponse(r)


@router.get("/reservations/order_pdf")
def reservation_order_pdf(res_id: int = Query(...)):
    """PDF-чек предзаказа брони (сохраняется в папку отчётов)."""
    from fastapi.responses import FileResponse
    migrate_v5()
    r = _res_row(res_id)
    if not r:
        raise HTTPException(404, detail="Бронь не найдена")
    items = []
    try:
        items = json.loads(r.get("order_json") or "[]")
    except ValueError:
        items = []
    total = round(sum(i.get("price", 0) * i.get("qty", 0) for i in items), 2)
    adv = float(r.get("advance_amount") or 0)

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import ParagraphStyle
    except ImportError:
        raise HTTPException(500, detail="Не установлена библиотека reportlab (pip install reportlab)")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"reservation-{res_id}.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm, topMargin=15*mm, bottomMargin=15*mm)
    try:
        pdfmetrics.registerFont(TTFont("Arial", r"C:\Windows\Fonts\arial.ttf"))
        font_name = "Arial"
    except Exception:
        raise HTTPException(500, detail="Не найден шрифт Arial (C:\\Windows\\Fonts\\arial.ttf)")

    h1 = ParagraphStyle("h1", fontName=font_name, fontSize=16, spaceAfter=4)
    h2 = ParagraphStyle("h2", fontName=font_name, fontSize=11, textColor=colors.HexColor("#495057"))
    cell = ParagraphStyle("cell", fontName=font_name, fontSize=9, leading=11)

    title = "Предзаказ (банкет)" + (" — ЗАКРЫТОЕ МЕРОПРИЯТИЕ" if int(r.get("is_closed_event") or 0) else "")
    head = [
        Paragraph(title, h1),
        Paragraph(f"Бронь №{res_id}    Сформирован: {datetime.now().strftime('%d.%m.%Y %H:%M')}", h2),
        Spacer(1, 4*mm),
        Paragraph(f"Дата брони: <b>{r['res_date']} {str(r['res_time'])[:5]}</b>    "
                  f"Стол: <b>{r['table_id']}</b>    Гостей: <b>{r.get('guests_count') or '—'}</b>", h2),
        Paragraph(f"Гость: <b>{r.get('guest_name') or ''}</b>    Телефон: <b>{r.get('phone') or ''}</b>", h2),
        Spacer(1, 6*mm),
    ]

    if items:
        rows = [["Позиция", "Цена", "Кол-во", "Сумма"]]
        for i in items:
            rows.append([
                Paragraph(str(i.get("name", "")), cell),
                f"{i.get('price', 0):.2f}",
                str(i.get("qty", 0)),
                f"{i.get('price', 0) * i.get('qty', 0):.2f}",
            ])
        rows.append(["", "", "ИТОГО:", f"{total:.2f}"])
        t = Table(rows, colWidths=[100*mm, 25*mm, 20*mm, 30*mm], repeatRows=1)
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f8f9fa")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#ced4da")),
            ("ALIGN", (1, 1), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        head += [t, Spacer(1, 6*mm)]
    else:
        head += [Paragraph("Состав заказа не заполнен.", h2), Spacer(1, 6*mm)]

    pay_rows = [["Аванс", "Метод оплаты", "К оплате"],
                [f"{adv:.2f}", str(r.get("advance_method") or "—"), f"{max(total - adv, 0):.2f}"]]
    tp = Table(pay_rows, colWidths=[50*mm, 70*mm, 55*mm])
    tp.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#ced4da")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    head.append(tp)

    try:
        doc.build(head)
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка генерации PDF: {e}")
    return FileResponse(str(path), media_type="application/pdf",
                        filename=f"reservation-{res_id}.pdf")

# ------------------------------
# Директор: монитор ожидания Чиров и ручной пуск/стоп линии 2
# ------------------------------
@router.get("/director/chiry-status")
def get_chiry_status(oper_id: str = Query(None)):
    # Только суперпользователи видят монитор
    if (oper_id or "").strip() not in SUPERUSERS:
        return JSONResponse({"wait_time": 0, "line2_active": False})
    row = db.execute("""
        SELECT created_at FROM active_orders
        WHERE CAST(name_id AS INTEGER) BETWEEN 401 AND 450
          AND status IN ('new', 'working')
        ORDER BY created_at ASC LIMIT 1
    """).fetchone()
    wait_min = 0
    if row:
        # created_at в формате 'YYYY-MM-DD HH:MM:SS'
        created = datetime.fromisoformat(str(row[0]))
        wait_min = int((datetime.now() - created).total_seconds() / 60)
    is_active = _chiry_line2_active()
    return JSONResponse({"wait_time": wait_min, "line2_active": is_active, "threshold": 35})

@router.post("/director/activate-line2")
def activate_chiry_line2(pin: str = Form(...), oper_id: str = Form(...)):
    if (oper_id or "").strip() not in SUPERUSERS or pin != CHIRY_PIN:
        raise HTTPException(403, detail="Неверный PIN или нет прав")
    try:
        with open(LINE2_FLAG, "w", encoding="utf-8") as f:
            f.write("1")
        return JSONResponse({"status": "activated"})
    except Exception as e:
        raise HTTPException(500, detail=f"Не удалось создать флаг линии 2: {e}")

@router.post("/director/deactivate-line2")
def deactivate_chiry_line2(oper_id: str = Form(...)):
    if (oper_id or "").strip() not in SUPERUSERS:
        raise HTTPException(403, detail="Нет прав")
    try:
        if os.path.exists(LINE2_FLAG):
            os.remove(LINE2_FLAG)
        return JSONResponse({"status": "deactivated"})
    except Exception as e:
        raise HTTPException(500, detail=f"Не удалось удалить флаг линии 2: {e}")

@router.get("/settings/max-table")
def api_get_max_table():
    n = get_max_table()
    return JSONResponse({"max_table": n, "min": MIN_MAX_TABLE, "max": MAX_MAX_TABLE})

@router.post("/director/max-table")
def api_set_max_table(
    pin: str = Form(...),
    oper_id: str = Form(...),
    max_table: int = Form(...),
):
    if (oper_id or "").strip() not in SUPERUSERS or pin != MAX_TABLE_PIN:
        raise HTTPException(403, detail="Неверный PIN или нет прав")
    try:
        n = int(max_table)
    except (TypeError, ValueError):
        raise HTTPException(400, detail="Max_Table должен быть числом")
    if n < MIN_MAX_TABLE or n > MAX_MAX_TABLE:
        raise HTTPException(400, detail=f"Max_Table: от {MIN_MAX_TABLE} до {MAX_MAX_TABLE}")
    try:
        _save_max_table(n)
        return JSONResponse({"status": "ok", "max_table": n})
    except Exception as e:
        raise HTTPException(500, detail=f"Не удалось сохранить Max_Table: {e}")

# ------------------------------
# Журналы/выгрузка за день и события
# ------------------------------
@router.get("/orders/events")
def list_events(order_id: str = Query(None), limit: int = Query(200)):
    q = "SELECT * FROM order_events"
    params = []
    if order_id:
        q += " WHERE order_id = ?"
        params.append(order_id)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = db.execute(q, params).fetchall()
    return JSONResponse([dict(r) for r in rows])

@router.post("/export/day")
def export_day(oper_id: str = Form(...)):
    _require_superuser(oper_id)
    day = datetime.now().strftime("%Y-%m-%d")
    base = Path(__file__).parent.parent / "export"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"export-{day}.txt"
    try:
        items = db.execute("""
            SELECT order_id, table_id, name_id, name, qty, price, status, created_at
            FROM active_orders
            WHERE substr(created_at,1,10) = ?
            ORDER BY order_id, id
        """, (day,)).fetchall()

        pays = db.execute("""
            SELECT order_id, method, amount, oper_id, ts
            FROM payments
            WHERE substr(ts,1,10) = ?
            ORDER BY ts
        """, (day,)).fetchall()

        tips = db.execute("""
            SELECT order_id, waiter_id, amount, ts
            FROM tips
            WHERE substr(ts,1,10) = ?
            ORDER BY ts
        """, (day,)).fetchall()

        discs = db.execute("""
            SELECT order_id, percent, set_by_oper_id, ts
            FROM discounts
            WHERE substr(ts,1,10) = ?
            ORDER BY ts
        """, (day,)).fetchall()

        evts = db.execute("""
            SELECT order_id, item_id, event_type, old_val, new_val, oper_id, pin_ok, ts
            FROM order_events
            WHERE substr(ts,1,10) = ?
            ORDER BY ts
        """, (day,)).fetchall()

        # Итоги дня
        gross = 0.0
        cancelled_cnt = 0
        for r in items:
            amt = float(r["qty"] or 0) * float(r["price"] or 0.0)
            if r["status"] != "cancelled":
                gross += amt
            else:
                cancelled_cnt += 1

        paid = float(db.execute("""
            SELECT COALESCE(SUM(amount),0) FROM payments WHERE substr(ts,1,10) = ?
        """, (day,)).fetchone()[0] or 0.0)

        tip_sum = float(db.execute("""
            SELECT COALESCE(SUM(amount),0) FROM tips WHERE substr(ts,1,10) = ?
        """, (day,)).fetchone()[0] or 0.0)

        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# Export day: {day}\n\n")
            f.write("## SUMMARY\n")
            f.write(f"Gross (items excl. cancelled): {gross:.2f}\n")
            f.write(f"Cancelled items: {cancelled_cnt}\n")
            f.write(f"Paid total: {paid:.2f}\n")
            f.write(f"Tips total: {tip_sum:.2f}\n\n")

            f.write("## ITEMS\n")
            for r in items:
                line = f"{r['order_id']}\t{r['table_id']}\t{r['name_id']}\t{r['name']}\t{r['qty']}\t{r['price']}\t{r['status']}\t{r['created_at']}\n"
                f.write(line)
            f.write("\n")
 
            f.write("## PAYMENTS\n")
            for r in pays:
                line = f"{r['order_id']}\t{r['method']}\t{r['amount']}\t{r['oper_id']}\t{r['ts']}\n"
                f.write(line)
            f.write("\n")

            f.write("## TIPS\n")
            for r in tips:
                line = f"{r['order_id']}\t{r['waiter_id']}\t{r['amount']}\t{r['ts']}\n"
                f.write(line)
            f.write("\n")

            f.write("## DISCOUNTS\n")
            for r in discs:
                line = f"{r['order_id']}\t{r['percent']}\t{r['set_by_oper_id']}\t{r['ts']}\n"
                f.write(line)
            f.write("\n")

            f.write("## EVENTS\n")
            for r in evts:
                oldv = (r['old_val'] or '').replace('\t',' ')
                newv = (r['new_val'] or '').replace('\t',' ')
                line = f"{r['order_id']}\t{r['item_id']}\t{r['event_type']}\t{oldv}\t{newv}\t{r['oper_id']}\t{r['pin_ok']}\t{r['ts']}\n"
                f.write(line)

        return JSONResponse({"status": "ok", "file": str(path)})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)
		
@router.post("/admin/rebuild_active_orders")
def admin_rebuild_active_orders(oper_id: str = Form(...)):
    _require_superuser(oper_id)
    try:
        db.execute("BEGIN")

        # 1) Создадим новую таблицу со status_ts
        db.execute("""
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
        )
        """)

        # 2) Переносим данные (если старой колонки нет — подставим created_at)
        cols = {r['name'] for r in db.execute("PRAGMA table_info(active_orders)").fetchall()}
        if 'status_ts' in cols:
            db.execute("""
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
            db.execute("""
            INSERT INTO __ao_new (
                id, order_id, oper_id, table_id, name_id, name, qty, price, status,
                stage, production_line, pickup_point, discount_pct, created_at, status_ts
            )
            SELECT
                id, order_id, oper_id, table_id, name_id, name, qty, price, status,
                stage, production_line, pickup_point, discount_pct, created_at, created_at
            FROM active_orders
            """)

        # 3) Удаляем старую таблицу и переименовываем новую
        db.execute("DROP TABLE active_orders")
        db.execute("ALTER TABLE __ao_new RENAME TO active_orders")

        # 4) Пересоздаём триггер
        db.execute("DROP TRIGGER IF EXISTS trg_status_ts")
        db.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_status_ts
        AFTER UPDATE OF status ON active_orders
        BEGIN
            UPDATE active_orders SET status_ts = datetime('now') WHERE id = NEW.id;
        END;
        """)

        # 5) Индексы (минимально необходимые)
        db.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON active_orders(status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_orders_stage  ON active_orders(stage)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_orders_ts     ON active_orders(status_ts)")

        db.commit()
        return JSONResponse({"status": "ok", "message": "active_orders rebuilt with status_ts"})
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"rebuild failed: {e}")

# ------------------------------
# Справочники: список операторов
# ------------------------------
@router.get("/operators/list")
def get_operators_list():
    rows = db.execute(
        "SELECT oper_id, oper, name, lastname FROM oper ORDER BY CAST(oper_id AS INTEGER)"
    ).fetchall()
    return JSONResponse([dict(r) for r in rows])

# ------------------------------
# Журнал авторизаций (login/logout/scan_qr)
# ------------------------------
@router.post("/auth/log")
def auth_log(action: str = Form(...), oper_id: str = Form(...), request: Request = None):
    action = (action or "").lower()
    if action not in {"login", "logout", "scan_qr"}:
        raise HTTPException(400, detail="action: login|logout|scan_qr")
    ip = request.client.host if request and request.client else "-"
    ua = request.headers.get("user-agent", "-") if request else "-"
    try:
        db.execute(
            "INSERT INTO auth_log (oper_id, action, ip, ua) VALUES (?, ?, ?, ?)",
            (oper_id, action, ip, ua)
        )
        db.commit()
        return JSONResponse({"status": "ok"})
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка auth_log: {e}")

@router.get("/admin/debug_schema")
def admin_debug_schema(oper_id: str = Query(None)):
    _require_superuser(oper_id)
    tbl = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='active_orders'").fetchone()
    trg = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_status_ts'").fetchone()
    return JSONResponse({
        "table_sql": (tbl["sql"] if tbl else None),
        "trigger_sql": (trg["sql"] if trg else None)
    })
	
@router.post("/menu/stoplist")
def menu_stoplist(name_id: str = Form(...), stoplisted: int = Form(...), oper_id: str = Form(...)):
    _require_superuser(oper_id)
    db.execute("UPDATE menu SET stoplisted=? WHERE name_id=?", (1 if int(stoplisted) else 0, str(name_id)))
    db.commit()
    return JSONResponse({"status": "ok", "name_id": name_id, "stoplisted": int(stoplisted)})

@router.post("/menu/priority")
def menu_priority(name_id: str = Form(...), priority: int = Form(...), oper_id: str = Form(...)):
    _require_superuser(oper_id)
    pr = max(0, min(3, int(priority)))
    db.execute("UPDATE menu SET priority=? WHERE name_id=?", (pr, str(name_id)))
    db.commit()
    return JSONResponse({"status": "ok", "name_id": name_id, "priority": pr})

# ------------------------------
# Причины отмен и «быстрые причины»
# ------------------------------
@router.post("/reasons/cancel/upsert")
def cancel_reason_upsert(code: str = Form(...), title: str = Form(...)):
    db.execute("INSERT INTO cancel_reasons(code, title) VALUES(?, ?) ON CONFLICT(code) DO UPDATE SET title=excluded.title",
               (code.strip(), title.strip()))
    db.commit()
    return JSONResponse({"status": "ok", "code": code, "title": title})

@router.get("/reasons/cancel/list")
def cancel_reason_list():
    rows = db.execute("SELECT code, title FROM cancel_reasons ORDER BY code").fetchall()
    return JSONResponse([dict(r) for r in rows])

@router.post("/reasons/quick/upsert")
def quick_reason_upsert(kind: str = Form(...), code: str = Form(...), title: str = Form(...)):
    k = (kind or "").strip().lower()
    if k not in {"cancel", "edit", "discount"}:
        raise HTTPException(400, detail="kind должен быть cancel|edit|discount")
    db.execute("""
        INSERT INTO quick_reasons(kind, code, title)
        VALUES(?, ?, ?)
        ON CONFLICT(kind, code) DO UPDATE SET title=excluded.title
    """, (k, code.strip(), title.strip()))
    db.commit()
    return JSONResponse({"status": "ok", "kind": k, "code": code, "title": title})

@router.get("/reasons/quick/list")
def quick_reason_list(kind: str = Query(None)):
    q = "SELECT kind, code, title FROM quick_reasons"
    params = []
    if kind:
        q += " WHERE kind = ?"
        params.append(kind.strip().lower())
    q += " ORDER BY kind, code"
    rows = db.execute(q, params).fetchall()
    return JSONResponse([dict(r) for r in rows])
@router.get("/report/tips_by_waiter")
def report_tips_by_waiter(day: str = Query(None), limit: int = Query(100)):
    """
    Чаевые по официантам за указанный день (YYYY-MM-DD). По умолчанию — сегодня.
    Возвращает сводку и список по waiter_id.
    """
    day = (day or datetime.now().strftime("%Y-%m-%d")).strip()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except Exception:
        raise HTTPException(400, detail="day должен быть YYYY-MM-DD")

    # Разбивка по официантам
    rows = db.execute("""
        SELECT
          t.waiter_id,
          COALESCE(o.name, o.oper, t.waiter_id) AS waiter_name,
          COUNT(DISTINCT t.order_id) AS orders_with_tips,
          ROUND(SUM(t.amount), 2) AS sum_tips,
          MIN(t.ts) AS first_tip,
          MAX(t.ts) AS last_tip
        FROM tips t
        LEFT JOIN oper o ON o.oper_id = t.waiter_id
        WHERE substr(t.ts,1,10) = ?
        GROUP BY t.waiter_id
        ORDER BY sum_tips DESC
        LIMIT ?
    """, (day, int(limit))).fetchall()

    total_sum = float(db.execute("""
        SELECT COALESCE(SUM(amount),0) FROM tips WHERE substr(ts,1,10)=?
    """, (day,)).fetchone()[0] or 0.0)

    return JSONResponse({
        "day": day,
        "total_tips": round(total_sum, 2),
        "items": [dict(r) for r in rows]
    })

@router.get("/report/discounts_by_day")
def report_discounts_by_day(day: str = Query(None), limit: int = Query(200)):
    """
    Скидки за день: общая статистика и разбивка по операторам (кто ставил скидку).
    """
    day = (day or datetime.now().strftime("%Y-%m-%d")).strip()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except Exception:
        raise HTTPException(400, detail="day должен быть YYYY-MM-DD")

    total_rows = int(db.execute("""
        SELECT COUNT(*) FROM discounts WHERE substr(ts,1,10)=?
    """, (day,)).fetchone()[0] or 0)

    avg_pct = float(db.execute("""
        SELECT COALESCE(AVG(percent),0) FROM discounts WHERE substr(ts,1,10)=?
    """, (day,)).fetchone()[0] or 0.0)

    max_pct = float(db.execute("""
        SELECT COALESCE(MAX(percent),0) FROM discounts WHERE substr(ts,1,10)=?
    """, (day,)).fetchone()[0] or 0.0)

    # Разбивка по операторам, кто ставил скидку
    br_rows = db.execute("""
        SELECT
          d.set_by_oper_id,
          COALESCE(o.name, o.oper, d.set_by_oper_id) AS oper_name,
          COUNT(*) AS cnt,
          ROUND(AVG(d.percent), 2) AS avg_pct,
          ROUND(MAX(d.percent), 2) AS max_pct
        FROM discounts d
        LEFT JOIN oper o ON o.oper_id = d.set_by_oper_id
        WHERE substr(d.ts,1,10)=?
        GROUP BY d.set_by_oper_id
        ORDER BY cnt DESC
    """, (day,)).fetchall()

    # Список скидок по времени (с ограничением)
    recs = db.execute("""
        SELECT order_id, percent, set_by_oper_id, ts
        FROM discounts
        WHERE substr(ts,1,10)=?
        ORDER BY ts
        LIMIT ?
    """, (day, int(limit))).fetchall()

    return JSONResponse({
        "day": day,
        "summary": {
            "count": total_rows,
            "avg_percent": round(avg_pct, 2),
            "max_percent": round(max_pct, 2)
        },
        "by_operator": [dict(r) for r in br_rows],
        "records": [dict(r) for r in recs]
    })

# ------------------------------
# Снапшот дня (агрегаты director)
# ------------------------------
@router.post("/summary/snapshot_day")
def summary_snapshot_day(day: str = Form(None)):
    """
    day: YYYY-MM-DD; если не задан — текущая дата.
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    # Валидируем формат
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except Exception:
        raise HTTPException(400, detail="day должен быть YYYY-MM-DD")

    # Итоги
    items_gross = float(db.execute("""
        SELECT COALESCE(SUM(qty*price),0) FROM active_orders
        WHERE substr(created_at,1,10)=? AND status!='cancelled'
    """, (day,)).fetchone()[0] or 0.0)

    paid = float(db.execute("""
        SELECT COALESCE(SUM(amount),0) FROM payments WHERE substr(ts,1,10)=?
    """, (day,)).fetchone()[0] or 0.0)

    tips = float(db.execute("""
        SELECT COALESCE(SUM(amount),0) FROM tips WHERE substr(ts,1,10)=?
    """, (day,)).fetchone()[0] or 0.0)

    cancels_count = int(db.execute("""
        SELECT COUNT(*) FROM active_orders
        WHERE substr(created_at,1,10)=? AND status='cancelled'
    """, (day,)).fetchone()[0] or 0)

    # Среднее ожидание для Чиров (от created_at до status_ts при готовности/выдаче)
    rows = db.execute("""
        SELECT created_at, status_ts FROM active_orders
        WHERE CAST(name_id AS INTEGER) BETWEEN 401 AND 450
          AND substr(created_at,1,10)=?
          AND (status='ready' OR status='served' OR status='closed')
    """, (day,)).fetchall()
    total_min = 0
    cnt = 0
    for r in rows:
        try:
            c = datetime.fromisoformat(str(r["created_at"]).replace(" ", "T"))
            s = datetime.fromisoformat(str(r["status_ts"]).replace(" ", "T")) if r["status_ts"] else c
            total_min += max(int((s - c).total_seconds() / 60), 0)
            cnt += 1
        except Exception:
            pass
    chiry_wait_avg = round((total_min / cnt), 2) if cnt else 0.0

    db.execute("""
        INSERT INTO day_snapshot(day, items_gross, paid, tips, cancels_count, chiry_wait_avg)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(day) DO UPDATE SET
          items_gross=excluded.items_gross,
          paid=excluded.paid,
          tips=excluded.tips,
          cancels_count=excluded.cancels_count,
          chiry_wait_avg=excluded.chiry_wait_avg
    """, (day, round(items_gross,2), round(paid,2), round(tips,2), cancels_count, chiry_wait_avg))
    db.commit()
    return JSONResponse({
        "status": "ok",
        "day": day,
        "items_gross": round(items_gross,2),
        "paid": round(paid,2),
        "tips": round(tips,2),
        "cancels_count": cancels_count,
        "chiry_wait_avg": chiry_wait_avg
    })

# ------------------------------
# Очередь печати / уведомлений
# ------------------------------
@router.post("/print/queue")
def print_queue_add(target: str = Form(...), payload_json: str = Form(...)):
    t = (target or "").strip().lower()
    if t not in {"bar", "kitchen", "client", "custom"}:
        raise HTTPException(400, detail="target: bar|kitchen|client|custom")
    # валидация json
    try:
        _ = json.loads(payload_json)
    except Exception:
        raise HTTPException(400, detail="payload_json должен быть валидным JSON")
    try:
        db.execute(
            "INSERT INTO print_queue (target, payload, status) VALUES (?, ?, 'new')",
            (t, payload_json)
        )
        qid = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.commit()
        return JSONResponse({"status": "queued", "id": qid, "target": t})
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка добавления в очередь печати: {e}")

@router.get("/print/queue")
def print_queue_list(status: str = Query("new"), limit: int = Query(50)):
    s = (status or "new").strip().lower()
    rows = db.execute(
        "SELECT id, target, payload, status, ts FROM print_queue WHERE status = ? ORDER BY id LIMIT ?",
        (s, int(limit))
    ).fetchall()
    return JSONResponse([dict(r) for r in rows])

@router.post("/print/ack")
def print_queue_ack(id: int = Form(...), status: str = Form(...)):
    s = (status or "").strip().lower()
    if s not in {"sent", "failed"}:
        raise HTTPException(400, detail="status: sent|failed")
    try:
        db.execute("UPDATE print_queue SET status = ? WHERE id = ?", (s, int(id)))
        db.commit()
        return JSONResponse({"status": "ok", "id": id, "set": s})
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка обновления статуса очереди: {e}")

@router.post("/menu/stoplist_bulk")
def menu_stoplist_bulk(
    stoplisted: int = Form(...),            # 0/1
    oper_id: str = Form(...),
    min_id: int = Form(None),
    max_id: int = Form(None),
    ids_json: str = Form(None)              # JSON: [451, 452, "453"]
):
    _require_superuser(oper_id)
    st = 1 if int(stoplisted) else 0
    try:
        if ids_json:
            try:
                ids = json.loads(ids_json)
                if not isinstance(ids, list) or not ids:
                    raise ValueError
                ids = [str(x) for x in ids]
            except Exception:
                raise HTTPException(400, detail="ids_json должен быть JSON-массивом name_id")
            placeholders = ",".join(["?"] * len(ids))
            sql = f"UPDATE menu SET stoplisted=? WHERE name_id IN ({placeholders})"
            params = [st, *ids]
            cur = db.execute(sql, params)
            db.commit()
            return JSONResponse({"status":"ok","affected": cur.rowcount, "mode":"list"})
        else:
            if min_id is None or max_id is None:
                raise HTTPException(400, detail="Укажите min_id и max_id, либо ids_json")
            cur = db.execute(
                "UPDATE menu SET stoplisted=? WHERE CAST(name_id AS INTEGER) BETWEEN ? AND ?",
                (st, int(min_id), int(max_id))
            )
            db.commit()
            return JSONResponse({"status":"ok","affected": cur.rowcount, "mode":"range"})
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка bulk stoplist: {e}")

@router.post("/menu/priority_bulk")
def menu_priority_bulk(
    priority: int = Form(...),              # 0..3
    oper_id: str = Form(...),
    min_id: int = Form(None),
    max_id: int = Form(None),
    ids_json: str = Form(None)
):
    _require_superuser(oper_id)
    pr = max(0, min(3, int(priority)))
    try:
        if ids_json:
            try:
                ids = json.loads(ids_json)
                if not isinstance(ids, list) or not ids:
                    raise ValueError
                ids = [str(x) for x in ids]
            except Exception:
                raise HTTPException(400, detail="ids_json должен быть JSON-массивом name_id")
            placeholders = ",".join(["?"] * len(ids))
            sql = f"UPDATE menu SET priority=? WHERE name_id IN ({placeholders})"
            params = [pr, *ids]
            cur = db.execute(sql, params)
            db.commit()
            return JSONResponse({"status":"ok","affected": cur.rowcount, "mode":"list"})
        else:
            if min_id is None or max_id is None:
                raise HTTPException(400, detail="Укажите min_id и max_id, либо ids_json")
            cur = db.execute(
                "UPDATE menu SET priority=? WHERE CAST(name_id AS INTEGER) BETWEEN ? AND ?",
                (pr, int(min_id), int(max_id))
            )
            db.commit()
            return JSONResponse({"status":"ok","affected": cur.rowcount, "mode":"range"})
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка bulk priority: {e}")

# ------------------------------
# Аудит-цепочка (tamper-evident)
# ------------------------------
@router.post("/audit/append")
def audit_append(payload_json: str = Form(...)):
    # payload_json должен быть строкой JSON с ключевыми полями (type/ref/oper_id/...)
    try:
        _ = json.loads(payload_json)
    except Exception:
        raise HTTPException(400, detail="payload_json должен быть валидным JSON")

    # считаем SHA256(prev_hash || payload)
    try:
        import hashlib
        prev = db.execute("SELECT hash FROM audit_chain ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = prev["hash"] if prev else ""
        h = hashlib.sha256()
        h.update((prev_hash + payload_json).encode("utf-8"))
        digest = h.hexdigest()

        db.execute(
            "INSERT INTO audit_chain (prev_hash, payload, hash) VALUES (?, ?, ?)",
            (prev_hash, payload_json, digest)
        )
        db.commit()
        return JSONResponse({"status": "ok", "hash": digest})
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка записи в audit_chain: {e}")

# ------------------------------
# Оценка обслуживания (1–5) + комментарий гостя
# ------------------------------
@router.post("/feedback/add")
def feedback_add(
    order_id: str = Form(...),
    rating: int = Form(...),
    comment: str = Form(default=""),
    table_id: str = Form(default=""),
    oper_id: str = Form(default=""),
):
    migrate_v4()
    oid = (order_id or "").strip()
    if not oid:
        raise HTTPException(400, detail="Укажите order_id")
    any_row = db.execute("SELECT 1 FROM active_orders WHERE order_id = ? LIMIT 1", (oid,)).fetchone()
    if not any_row:
        raise HTTPException(404, detail="Заказ не найден")
    try:
        r = int(rating)
    except (TypeError, ValueError):
        raise HTTPException(400, detail="Оценка должна быть числом от 1 до 5")
    if not 1 <= r <= 5:
        raise HTTPException(400, detail="Оценка: от 1 до 5")
    if db.execute("SELECT 1 FROM service_feedback WHERE order_id = ?", (oid,)).fetchone():
        raise HTTPException(400, detail="По этому заказу оценка уже оставлена")
    try:
        db.execute(
            "INSERT INTO service_feedback (order_id, table_id, rating, comment, oper_id) VALUES (?, ?, ?, ?, ?)",
            (oid, (table_id or "").strip(), r, (comment or "").strip(), (oper_id or "").strip())
        )
        db.commit()
        return JSONResponse({"status": "ok", "order_id": oid, "rating": r})
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=f"Ошибка сохранения оценки: {e}")

@router.get("/feedback/status")
def feedback_status(order_id: str = Query(...)):
    """Оставлена ли оценка по заказу (для гостевого UI)."""
    migrate_v4()
    row = db.execute(
        "SELECT rating, comment FROM service_feedback WHERE order_id = ?",
        ((order_id or "").strip(),)
    ).fetchone()
    return JSONResponse({
        "order_id": order_id,
        "rated": bool(row),
        "rating": row["rating"] if row else None,
    })

@router.get("/feedback/day")
def feedback_day(day: str = Query(None)):
    """Оценки за день: средний балл и список."""
    migrate_v4()
    day = (day or datetime.now().strftime("%Y-%m-%d")).strip()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, detail="day должен быть YYYY-MM-DD")
    rows = db.execute("""
        SELECT order_id, table_id, rating, comment, oper_id, created_at
        FROM service_feedback
        WHERE substr(created_at,1,10) = ?
        ORDER BY created_at DESC
    """, (day,)).fetchall()
    items = [dict(r) for r in rows]
    avg = round(sum(i["rating"] for i in items) / len(items), 2) if items else 0.0
    return JSONResponse({"day": day, "count": len(items), "avg_rating": avg, "items": items})
@router.get("/feedback/day_pdf")
def feedback_day_pdf(day: str = Query(None), oper_id: str = Query(None)):
    """PDF-отчёт по оценкам обслуживания за день (только суперпользователь)."""
    from fastapi.responses import FileResponse
    _require_superuser(oper_id)
    migrate_v4()
    day = (day or datetime.now().strftime("%Y-%m-%d")).strip()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, detail="day должен быть YYYY-MM-DD")
    rows = db.execute("""
        SELECT order_id, table_id, rating, comment, oper_id, created_at
        FROM service_feedback
        WHERE substr(created_at,1,10) = ?
        ORDER BY created_at DESC
    """, (day,)).fetchall()
    items = [dict(r) for r in rows]
    avg = round(sum(i["rating"] for i in items) / len(items), 2) if items else 0.0
    dist = {s: 0 for s in range(1, 6)}
    for i in items:
        dist[int(i["rating"])] = dist.get(int(i["rating"]), 0) + 1

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import ParagraphStyle
    except ImportError:
        raise HTTPException(500, detail="Не установлена библиотека reportlab (pip install reportlab)")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"feedback-{day}.pdf"

    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm, topMargin=15*mm, bottomMargin=15*mm)
    font_name = "Helvetica"
    try:
        pdfmetrics.registerFont(TTFont("Arial", r"C:\Windows\Fonts\arial.ttf"))
        font_name = "Arial"
    except Exception:
        raise HTTPException(500, detail="Не найден шрифт Arial (C:\Windows\Fonts\arial.ttf)")

    h1 = ParagraphStyle("h1", fontName=font_name, fontSize=16, spaceAfter=4)
    h2 = ParagraphStyle("h2", fontName=font_name, fontSize=11, textColor=colors.HexColor("#495057"))
    cell = ParagraphStyle("cell", fontName=font_name, fontSize=9, leading=11)

    story = [
        Paragraph("Отчёт: оценки обслуживания", h1),
        Paragraph(f"Дата: {day}    Сформирован: {datetime.now().strftime('%d.%m.%Y %H:%M')}", h2),
        Spacer(1, 6*mm),
        Paragraph(f"Оценок за день: <b>{len(items)}</b>    Средний балл: <b>{avg if items else '—'}</b>", h2),
        Spacer(1, 4*mm),
    ]

    dist_rows = [["Звёзды", "1★", "2★", "3★", "4★", "5★"],
                 ["Кол-во"] + [str(dist[s]) for s in range(1, 6)]]
    t = Table(dist_rows, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#ced4da")),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [t, Spacer(1, 6*mm)]

    if items:
        head = ["Время", "Заказ", "Стол", "Оценка", "Комментарий"]
        body = [[
            Paragraph(str(i["created_at"] or "")[11:16], cell),
            Paragraph(str(i["order_id"] or ""), cell),
            Paragraph(str(i["table_id"] or ""), cell),
            Paragraph("★" * int(i["rating"]), cell),
            Paragraph(str(i["comment"] or ""), cell),
        ] for i in items]
        tt = Table([head] + body, colWidths=[18*mm, 30*mm, 15*mm, 28*mm, 84*mm], repeatRows=1)
        tt.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), font_name),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#ced4da")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(tt)
    else:
        story.append(Paragraph("За этот день оценок нет.", h2))

    try:
        doc.build(story)
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка генерации PDF: {e}")
    return FileResponse(str(path), media_type="application/pdf",
                        filename=f"feedback-{day}.pdf")

# ------------------------------
# Директор: PDF по блокам формы
# ------------------------------
def _build_block_pdf(path: str, title: str, subtitle: str, header: list,
                     rows: list, widths: list, footer: list | None = None):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import ParagraphStyle

    try:
        pdfmetrics.registerFont(TTFont("Arial", r"C:\Windows\Fonts\arial.ttf"))
        font_name = "Arial"
    except Exception:
        raise HTTPException(500, detail="Не найден шрифт Arial (C:\Windows\Fonts\arial.ttf)")

    h1 = ParagraphStyle("h1", fontName=font_name, fontSize=14, spaceAfter=4)
    h2 = ParagraphStyle("h2", fontName=font_name, fontSize=10, textColor=colors.HexColor("#495057"))
    cell = ParagraphStyle("cell", fontName=font_name, fontSize=8, leading=10)

    story = [
        Paragraph(title, h1),
        Paragraph(subtitle, h2),
        Spacer(1, 5*mm),
    ]
    if rows:
        body = [[Paragraph(str(c), cell) for c in r] for r in rows]
        t = Table([header] + body, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#ced4da")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)
        if footer:
            story += [Spacer(1, 4*mm), Paragraph(footer, h2)]
    else:
        story.append(Paragraph("Данных нет.", h2))

    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=12*mm, rightMargin=12*mm, topMargin=12*mm, bottomMargin=12*mm)
    doc.build(story)


@router.get("/director/block_pdf")
def director_block_pdf(block: str = Query(...), day: str = Query(None), oper_id: str = Query(None)):
    """PDF-отчёт по блоку директорской формы: sales|queue|snapshot|tips|discounts|reservations."""
    from fastapi.responses import FileResponse
    from reportlab.lib.units import mm
    _require_superuser(oper_id)
    day = (day or datetime.now().strftime("%Y-%m-%d")).strip()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, detail="day должен быть YYYY-MM-DD")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    now_s = datetime.now().strftime('%d.%m.%Y %H:%M')
    path = str(REPORTS_DIR / f"director-{block}-{day}.pdf")

    if block == "sales":
        rows_db = db.execute("""
            SELECT order_id, oper_id, table_id, name, price, qty, status, created_at
            FROM active_orders WHERE status != 'served'
            ORDER BY created_at DESC
        """).fetchall()
        rows, rev, qty = [], 0.0, 0.0
        for r in rows_db:
            line = float(r["price"] or 0) * float(r["qty"] or 0)
            rev += line
            qty += float(r["qty"] or 0)
            rows.append([r["order_id"], r["oper_id"], r["table_id"], r["name"],
                         f"{float(r['price'] or 0):.2f}", r["qty"], r["status"], str(r["created_at"] or "")[:19]])
        _build_block_pdf(path, "Продажи (активные заказы)", f"Дата: {day}    Сформирован: {now_s}",
                         ["Заказ", "Опер", "Стол", "Блюдо", "Цена", "Кол-во", "Статус", "Время"],
                         rows, [22*mm, 14*mm, 13*mm, 62*mm, 18*mm, 15*mm, 18*mm, 33*mm],
                         footer=f"Итого: {rev:.2f} ₽    Позиций: {qty:.1f}")

    elif block == "queue":
        rows_db = db.execute("""
            SELECT id, target, status, ts, payload FROM print_queue ORDER BY id DESC LIMIT 100
        """).fetchall()
        rows = [[r["id"], r["target"], r["status"], str(r["ts"] or "")[:19],
                 (r["payload"] or "")[:160]] for r in rows_db]
        _build_block_pdf(path, "Очередь печати / уведомлений", f"Дата: {day}    Сформирован: {now_s}    Последние 100",
                         ["ID", "Цель", "Статус", "Время", "Payload"], rows,
                         [12*mm, 20*mm, 18*mm, 33*mm, 112*mm])

    elif block == "snapshot":
        r = db.execute("SELECT * FROM day_snapshot WHERE day = ?", (day,)).fetchone()
        rows = []
        if r:
            rows = [["Выручка (paid)", f"{float(r['paid'] or 0):.2f} ₽"],
                    ["Продано позиций (items_gross)", f"{float(r['items_gross'] or 0):.1f}"],
                    ["Чаевые", f"{float(r['tips'] or 0):.2f} ₽"],
                    ["Отмены", str(r["cancels_count"] or 0)],
                    ["Среднее ожидание Чиры", f"{float(r['chiry_wait_avg'] or 0):.1f} мин"],
                    ["Создан", str(r["created_at"] or "")[:19]]]
        _build_block_pdf(path, "Снимок дня (агрегаты)", f"Дата: {day}    Сформирован: {now_s}",
                         ["Показатель", "Значение"], rows, [90*mm, 85*mm])

    elif block == "tips":
        rows_db = db.execute("""
            SELECT t.waiter_id,
                   COALESCE(o.name, o.oper, t.waiter_id) AS waiter_name,
                   COUNT(DISTINCT t.order_id) AS orders_with_tips,
                   ROUND(SUM(t.amount), 2) AS sum_tips,
                   MIN(t.ts) AS first_tip, MAX(t.ts) AS last_tip
            FROM tips t LEFT JOIN oper o ON o.oper_id = t.waiter_id
            WHERE substr(t.ts,1,10) = ?
            GROUP BY t.waiter_id ORDER BY sum_tips DESC
        """, (day,)).fetchall()
        rows = [[f"{r['waiter_name'] or ''} ({r['waiter_id']})", r["orders_with_tips"],
                 f"{float(r['sum_tips'] or 0):.2f}", f"{str(r['first_tip'] or '-')[11:16]} — {str(r['last_tip'] or '-')[11:16]}"]
                for r in rows_db]
        total = sum(float(r["sum_tips"] or 0) for r in rows_db)
        _build_block_pdf(path, "Чаевые по официантам", f"Дата: {day}    Сформирован: {now_s}",
                         ["Официант", "Заказов с чаевыми", "Сумма", "Период"],
                         rows, [60*mm, 35*mm, 35*mm, 45*mm],
                         footer=f"Итого чаевых: {total:.2f} ₽")

    elif block == "discounts":
        total_rows = int(db.execute("SELECT COUNT(*) FROM discounts WHERE substr(ts,1,10)=?", (day,)).fetchone()[0] or 0)
        avg_pct = float(db.execute("SELECT COALESCE(AVG(percent),0) FROM discounts WHERE substr(ts,1,10)=?", (day,)).fetchone()[0] or 0)
        max_pct = float(db.execute("SELECT COALESCE(MAX(percent),0) FROM discounts WHERE substr(ts,1,10)=?", (day,)).fetchone()[0] or 0)
        by_op = db.execute("""
            SELECT d.set_by_oper_id, COALESCE(o.name, o.oper, d.set_by_oper_id) AS oper_name,
                   COUNT(*) AS cnt, ROUND(AVG(d.percent),2) AS avg_pct, ROUND(MAX(d.percent),2) AS max_pct
            FROM discounts d LEFT JOIN oper o ON o.oper_id = d.set_by_oper_id
            WHERE substr(d.ts,1,10)=? GROUP BY d.set_by_oper_id ORDER BY cnt DESC
        """, (day,)).fetchall()
        recs = db.execute("SELECT order_id, percent, set_by_oper_id, ts FROM discounts WHERE substr(ts,1,10)=? ORDER BY ts LIMIT 200", (day,)).fetchall()
        rows = [["По операторам", "", "", ""],
                ["Оператор", "Кол-во", "Средний %", "Макс %"]]
        rows += [[f"{r['oper_name'] or ''} ({r['set_by_oper_id']})", str(r["cnt"]),
                  f"{float(r['avg_pct']):.2f}", f"{float(r['max_pct']):.2f}"] for r in by_op]
        rows.append(["Список скидок", "", "", ""])
        rows += [[str(r["ts"] or "")[:19], r["order_id"], f"{float(r['percent']):.2f}%", r["set_by_oper_id"]] for r in recs]
        _build_block_pdf(path, "Скидки за день", f"Дата: {day}    Сформирован: {now_s}",
                         ["Время", "Заказ", "%", "Кем"],
                         rows, [40*mm, 55*mm, 30*mm, 50*mm],
                         footer=f"Всего скидок: {total_rows}    Средний %: {avg_pct:.2f}    Макс %: {max_pct:.2f}")

    elif block == "reservations":
        rows_db = db.execute("""
            SELECT id, res_date, res_time, table_id, guest_name, phone, guests_count,
                   is_closed_event, advance_amount, advance_method, status, parent_res_id
            FROM reservations WHERE res_date = ? ORDER BY res_time, CAST(table_id AS INTEGER)
        """, (day,)).fetchall()
        rows = []
        for r in rows_db:
            kind = "Банкет" if int(r["is_closed_event"] or 0) else f"Стол {r['table_id']}"
            if r["parent_res_id"]:
                kind += f" (зал, банкет №{r['parent_res_id']})"
            rows.append([str(r["res_time"])[:5], kind, r["guest_name"] or "", r["phone"] or "",
                         str(r["guests_count"] or ""), f"{float(r['advance_amount'] or 0):.2f}",
                         r["advance_method"] or "", r["status"] or "Активна"])
        _build_block_pdf(path, "Брони и мероприятия", f"Дата: {day}    Сформирован: {now_s}",
                         ["Время", "Стол/Банкет", "Гость", "Телефон", "Гостей", "Аванс", "Оплата", "Статус"],
                         rows, [15*mm, 30*mm, 32*mm, 28*mm, 14*mm, 16*mm, 30*mm, 18*mm])

    else:
        raise HTTPException(400, detail="block: sales|queue|snapshot|tips|discounts|reservations")

    return FileResponse(path, media_type="application/pdf", filename=f"director-{block}-{day}.pdf")
