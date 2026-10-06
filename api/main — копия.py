from pathlib import Path
from fastapi import APIRouter, HTTPException, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
import os, json, shutil
from datetime import datetime
from core.config import TEMPLATES_DIR
from db.manager import db
from utils.references import generate_ref

router = APIRouter()

# --- ВРЕМЕННАЯ ДИАГНОСТИКА ---
from db.manager import migrate_v1

@router.get("/admin/check_schema")
def admin_check_schema():
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
def admin_migrate():
    try:
        migrate_v1()
        return JSONResponse({"status": "ok", "message": "migrate_v1 applied"})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

# ------------------------------
# Конфигурация доступа и логистики
# ------------------------------

SUPERUSERS = {'0', '52', '77'}

STATION_RANGES = {
    'bar_all': (101, 400),
    'il_drive': (601, 700),   # бывший Гриль
    'hot': (451, 500),
    'banquet': (501, 550),
    'salads': (551, 600),
    'chiry': (401, 450)
}

# Привязка ID к станциям
ACCESS_MATRIX = {
    '0': 'ALL', '52': 'ALL', '77': 'ALL',
    '31': 'bar', '32': 'bar', '33': 'bar', '34': 'bar', '35': 'bar',
    '36': 'bar', '37': 'bar', '38': 'bar', '39': 'bar', '40': 'bar',
    '41': 'il_drive', '42': 'il_drive', '43': 'il_drive', '44': 'il_drive',
    '45': 'il_drive', '46': 'il_drive', '47': 'il_drive', '48': 'il_drive',
    '49': 'il_drive', '50': 'il_drive',
    '61': 'hot', '62': 'hot',
    '63': 'salads', '64': 'salads',
    # Чиры, по этапам — валидация делается в /orders/status
    '65': 'chiry', '66': 'chiry', '67': 'chiry',
    '68': 'chiry', '69': 'chiry', '70': 'chiry',
    '71': 'chiry', '72': 'chiry', '73': 'chiry',
    '74': 'chiry', '75': 'chiry', '76': 'chiry',
}

def get_station_by_id(op_id: str) -> str:
    if not op_id:
        return 'none'
    op = op_id.strip()
    if op in SUPERUSERS:
        return 'ALL'
    return ACCESS_MATRIX.get(op, 'none')

def get_pickup_by_id(op_id: str) -> str:
    # Точка выдачи (бар или il_drive)
    if not op_id:
        return 'bar'
    op = op_id.strip()
    if ('41' <= op <= '50') or ('61' <= op <= '62') or ('65' <= op <= '76'):
        return 'il_drive'
    return 'bar'

# ------------------------------
# PIN и права на отмену/редактирование/скидки
# ------------------------------

ROOT_PIN = "9999"     #
STAFF_PIN = "2222"   # для 31–40
ALLOWED_CANCEL_IDS = set([str(i) for i in range(31, 41)]) | {'0', '52', '77'}

# Файловый флаг ручного пуска 2-й линии Чиры
BASE_DIR = Path(__file__).parent.parent
LINE2_FLAG = os.path.join(BASE_DIR, "chiry_line2.flag")
CHIRY_PIN = "5555"  # PIN директора для запуска линии 2

def _verify_pin(oper_id: str, pin: str) -> bool:
    if not oper_id or not pin:
        return False
    op = oper_id.strip()
    if op in SUPERUSERS:
        return pin == ROOT_PIN
    if op in ALLOWED_CANCEL_IDS:
        return pin == STAFF_PIN
    return False

def _ensure_not_served(item_row: dict):
    if item_row.get('status') == 'served':
        raise HTTPException(400, detail="Позиция уже выдана (served) — операция запрещена")

# ------------------------------
# UI и справочники
# ------------------------------

@router.get("/ui/{template_id}")
def get_ui(template_id: str):
    tpl_path = TEMPLATES_DIR / (template_id + ".")
    if not tpl_path.exists():
        tpl_path = TEMPLATES_DIR / "default."
    if not tpl_path.exists():
        raise HTTPException(404, detail="Шаблон не найден")
    with open(tpl_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@router.get("/menu")
def get_full_menu():
    rows = db.execute(
        "SELECT name_id, name, unit, price, description, category FROM menu WHERE is_active = 1 ORDER BY CAST(name_id AS INTEGER)"
    ).fetchall()
    return JSONResponse([dict(r) for r in rows])

# ------------------------------
# QR-скан
# ------------------------------

@router.get("/scan_qr")
def scan_qr_endpoint(scan_data: str = Query(...)):
    ref = generate_ref("SCAN", scan_data)
    parts = scan_data.strip().split("|")
    if len(parts) < 4 or parts[0] != "QR":
        return JSONResponse({"success": False, "error": "INVALID_FORMAT", "ref": ref})
    oper_id, role, template_id = parts[1], parts[2], parts[3]
    row = db.execute("SELECT oper_id, oper, name, lastname, qr_data FROM oper WHERE oper_id = ?", (oper_id,)).fetchone()
    if not row:
        return JSONResponse({"success": False, "error": "OPERATOR_NOT_FOUND", "ref": ref})
    operator = dict(row)
    # можно расширить правами из utils.access_rights при необходимости
    return JSONResponse({"success": True, "operator": operator, "ui": template_id, "ref": ref})

# ------------------------------
# Заказы: создание / получение / статусы
# ------------------------------

@router.post("/orders/create")
def create_order(oper_id: str = Form(...), table_id: str = Form(...), items_json: str = Form(...)):
    try:
        items = json.loads(items_json)
    except Exception:
        raise HTTPException(400, detail="items_json должен быть валидным JSON")
    if not items:
        raise HTTPException(400, detail="Пустой заказ")

    pickup = get_pickup_by_id(oper_id)
    # Линии для Чиров (401–450). Если LINE2_FLAG активен — всё уходит на 2-ю.
    is_chiry = any(401 <= int(i.get('name_id', 0)) <= 450 for i in items)
    prod_line = 1
    if is_chiry:
        if os.path.exists(LINE2_FLAG):
            prod_line = 2
        else:
            c1 = db.execute("SELECT COUNT(*) FROM active_orders WHERE production_line=1 AND status IN ('new','working')").fetchone()[0]
            c2 = db.execute("SELECT COUNT(*) FROM active_orders WHERE production_line=2 AND status IN ('new','working')").fetchone()[0]
            if c1 > 4 and c2 < c1:
                prod_line = 2

    order_id = f"ORD-{datetime.now().strftime('%H%M%S')}"
    sql = """
        INSERT INTO active_orders (
            order_id, oper_id, table_id, name_id, name, qty, price,
            production_line, pickup_point, status, stage, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', 1, datetime('now'))
    """
    data = [
        (
            order_id, oper_id, table_id,
            str(i.get('name_id')), str(i.get('name')), int(i.get('qty', 1)),
            float(i.get('price', 0.0)),
            prod_line, pickup
        )
        for i in items
    ]
    try:
        db.executemany(sql, data)
        db.commit()
        return JSONResponse({"order_id": order_id, "pickup": pickup, "line": prod_line})
    except Exception as e:
        db.rollback
        raise HTTPException(500, detail=f"Ошибка создания заказа: {e}")

@router.get("/orders/active")
def get_active_orders(
    station: str = Query(None),
    oper_id: str = Query(None),
    line: str = Query("all")
):
    op = (oper_id or "").strip()
    allowed = get_station_by_id(op)
    is_waiter = ('31' <= op <= '40')
    pickup = get_pickup_by_id(op)

    q = "SELECT * FROM active_orders WHERE status != 'served'"
    params = []

    if is_waiter:
        q += " AND status = 'ready' AND pickup_point = ?"
        params.append(pickup)
    else:
        # фильтр по линии, если указан
        if line and line != "all":
            q += " AND production_line = ?"
            params.append(int(line))
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

@router.post("/orders/status")
def update_order_status(
    item_id: int = Form(...),
    new_status: str = Form(...),
    oper_id: str = Form(default="system")
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
            else:
                if is_chiry:
                    # Л1Э1
                    if op in {'65','66','67'} and stage == 1 and new_status == 'working':
                        stage, new_status = 2, 'working'
                    # Л1Э2
                    elif op in {'68','69','70'} and stage == 2 and new_status == 'ready':
                        stage, new_status = 3, 'ready'
                    # Л2Э1
                    elif op in {'71','72','73'} and stage == 1 and new_status == 'working':
                        stage, new_status = 2, 'working'
                    # Л2Э2
                    elif op in {'74','75','76'} and stage == 2 and new_status == 'ready':
                        stage, new_status = 3, 'ready'
                    elif new_status == 'served' and stage >= 3:
                        # отдача любым «фронтом» после готовности
                        pass
                    else:
                        raise HTTPException(403, detail="⛔ Неверный этап/оператор для Чиров")
                elif is_il:
                    # Il_Drive — упрощённые два этапа
                    if stage == 1 and new_status == 'working':
                        stage = 2
                    elif stage == 2 and new_status == 'ready':
                        stage = 3
                    elif new_status == 'served' and stage >= 3:
                        pass
                    else:
                        raise HTTPException(403, detail="⛔ Неверный этап/оператор Il_Drive")
        else:
            # Обычные позиции
            if new_status == 'served':
                stage = 1

        db.execute("UPDATE active_orders SET status=?, stage=? WHERE id=?", (new_status, stage, item_id))
        db.commit()
        # Лог события
        db.execute("""
            INSERT INTO order_events(order_id, item_id, event_type, old_val, new_val, oper_id, pin_ok)
            VALUES(?, ?, 'status_change', NULL, ?, ?, 1)
        """, (item["order_id"], item_id, json.dumps({"status": new_status, "stage": stage}, ensure_ascii=False), op))
        db.commit()
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

    new_order_id = f"ORD-{datetime.now().strftime('%H%M%S')}"
    try:
        db.execute("BEGIN")
        for r in rows:
            it = dict(r)
            if it["status"] == "served":
                raise HTTPException(400, detail=f"Позиция {it['id']} уже выдана")
            db.execute("UPDATE active_orders SET order_id=? WHERE id=?", (new_order_id, it["id"]))
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

    new_order_id = f"ORD-{datetime.now().strftime('%H%M%S')}"
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
    # Права + PIN
    if oper_id not in ALLOWED_CANCEL_IDS and oper_id not in SUPERUSERS:
        raise HTTPException(403, detail="Нет прав на установку скидки")
    if not _verify_pin(oper_id, pin):
        raise HTTPException(403, detail="Неверный PIN")

    # Заказ существует?
    any_row = db.execute("SELECT 1 FROM active_orders WHERE order_id = ? LIMIT 1", (order_id,)).fetchone()
    if not any_row:
        raise HTTPException(404, detail="Заказ не найден")

    try:
        p = float(percent)
    except Exception:
        raise HTTPException(400, detail="Процент скидки должен быть числом")
    if p < 0 or p > 100:
        raise HTTPException(400, detail="Процент скидки должен быть в диапазоне 0..100")

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
    is_active = os.path.exists(LINE2_FLAG)
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
def export_day():
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
def admin_rebuild_active_orders():
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
def admin_debug_schema():
    tbl = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='active_orders'").fetchone()
    trg = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_status_ts'").fetchone()
    return JSONResponse({
        "table_sql": (tbl["sql"] if tbl else None),
        "trigger_sql": (trg["sql"] if trg else None)
    })
	
@router.post("/menu/stoplist")
def menu_stoplist(name_id: str = Form(...), stoplisted: int = Form(...)):
    db.execute("UPDATE menu SET stoplisted=? WHERE name_id=?", (1 if int(stoplisted) else 0, str(name_id)))
    db.commit()
    return JSONResponse({"status": "ok", "name_id": name_id, "stoplisted": int(stoplisted)})

@router.post("/menu/priority")
def menu_priority(name_id: str = Form(...), priority: int = Form(...)):
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