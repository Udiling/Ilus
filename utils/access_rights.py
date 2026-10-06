# C:\Offline Server\utils\access_rights.py
"""Матрица доступа: id_oper ↔ формы/станции, id_menu ↔ диапазоны меню."""

from __future__ import annotations

SUPERUSERS = {"0", "52", "77"}

# Привязка id_menu к категориям (spr/Задачи для GPT.txt)
MENU_RANGES = {
    "bar_alcohol": (101, 250),   # Бар алкоголь
    "bar_bao": (251, 300),       # Бао сл.алк.
    "bar_drinks": (301, 400),    # Бар напитки и пр.
    "chiry": (401, 450),
    "hot": (451, 500),
    "banquet": (501, 550),
    "salads": (551, 600),
    "grill": (601, 700),
}

# Диапазоны алкоголя (запрет для доставки id=60)
ALCOHOL_RANGES = [(101, 250), (251, 300)]

STATION_RANGES = {
    "bar_all": (101, 400),
    "il_drive": (601, 700),
    "hot": (451, 500),
    "banquet": (501, 550),
    "salads": (551, 600),
    "chiry": (401, 450),
}

# Оператор → станция производства (cook_pos / orders/active)
ACCESS_MATRIX = {
    "0": "ALL", "52": "ALL", "77": "ALL",
    "31": "bar", "32": "bar", "33": "bar", "34": "bar", "35": "bar",
    "36": "bar", "37": "bar", "38": "bar", "39": "bar", "40": "bar",
    "41": "il_drive", "42": "il_drive", "43": "il_drive", "44": "il_drive",
    "45": "il_drive", "46": "il_drive", "47": "il_drive", "48": "il_drive",
    "49": "il_drive", "50": "il_drive",
    "61": "hot", "62": "hot",
    "63": "salads", "64": "salads",
    "65": "chiry", "66": "chiry", "67": "chiry",
    "68": "chiry", "69": "chiry", "70": "chiry",
    "71": "chiry", "72": "chiry", "73": "chiry",
    "74": "chiry", "75": "chiry", "76": "chiry",
}

# Оператор Чиры → (линия, этап) для cook_pos и фильтрации очереди
CHIRY_LINE_STAGE = {
    "65": (1, 1), "66": (1, 1), "67": (1, 1),
    "68": (1, 2), "69": (1, 2), "70": (1, 2),
    "71": (2, 1), "72": (2, 1), "73": (2, 1),
    "74": (2, 2), "75": (2, 2), "76": (2, 2),
}

# Шаблоны UI
FORM_TEMPLATES = {
    "default": "default.html",
    "form_order": "form_order.html",
    "guest_pos": "guest_pos.html",
    "waiter_pos": "waiter_pos.html",
    "cook_pos": "cook_pos.html",
    "director": "director.html",
}

DELIVERY_OPER_ID = "60"
DELIVERY_DISCOUNT_PCT = 18.0

# Диапазоны oper_id по формам
_RANGE_DEFAULT = [(31, 40), (41, 50), (56, 60)]
_RANGE_GUEST_TABLE = (1, 25)
_RANGE_GUEST_TAKEAWAY = (26, 30)
_RANGE_WAITER = (31, 40)
_RANGE_COOK = [(41, 50), (61, 76)]
_RANGE_DISCOUNT = (56, 60)

# PIN / отмена (бар 31–40 + root)
ALLOWED_CANCEL_IDS = {str(i) for i in range(31, 41)} | SUPERUSERS


def _norm(op_id: str | None) -> str:
    return (op_id or "").strip()


def _in_range(op_id: str, lo: int, hi: int) -> bool:
    try:
        n = int(op_id)
        return lo <= n <= hi
    except (ValueError, TypeError):
        return False


def _in_any_range(op_id: str, ranges: list[tuple[int, int]]) -> bool:
    return any(_in_range(op_id, lo, hi) for lo, hi in ranges)


def is_superuser(op_id: str | None) -> bool:
    return _norm(op_id) in SUPERUSERS


def normalize_template(template_id: str | None) -> str:
    t = (_norm(template_id) or "default").lower()
    if t.endswith(".html"):
        t = t[:-5]
    while t and t[-1] in "!.,;:":
        t = t[:-1]
    return t if t in FORM_TEMPLATES else "default"


def allowed_forms(op_id: str | None) -> list[str]:
    op = _norm(op_id)
    if not op:
        return []
    if is_superuser(op):
        return list(FORM_TEMPLATES.keys())
    forms: list[str] = []
    if _in_any_range(op, _RANGE_DEFAULT):
        forms.append("default")
        forms.append("form_order")
    if _in_range(op, *_RANGE_GUEST_TABLE) or _in_range(op, *_RANGE_GUEST_TAKEAWAY):
        forms.append("guest_pos")
    if _in_range(op, *_RANGE_WAITER) or is_superuser(op):
        if "waiter_pos" not in forms:
            forms.append("waiter_pos")
    if _in_any_range(op, _RANGE_COOK):
        forms.append("cook_pos")
    return forms


def can_access_form(op_id: str | None, template_id: str | None) -> bool:
    op = _norm(op_id)
    tpl = normalize_template(template_id)
    if is_superuser(op):
        return True
    if tpl == "director":
        return False
    if tpl == "default" or tpl == "form_order":
        return _in_any_range(op, _RANGE_DEFAULT)
    if tpl == "guest_pos":
        return _in_range(op, *_RANGE_GUEST_TABLE) or _in_range(op, *_RANGE_GUEST_TAKEAWAY)
    if tpl == "waiter_pos":
        return _in_range(op, *_RANGE_WAITER)
    if tpl == "cook_pos":
        return _in_any_range(op, _RANGE_COOK)
    return False


def get_station_by_id(op_id: str | None) -> str:
    op = _norm(op_id)
    if not op:
        return "none"
    if is_superuser(op):
        return "ALL"
    return ACCESS_MATRIX.get(op, "none")


def get_chiry_line_stage(op_id: str | None) -> tuple[int, int] | None:
    """Линия и этап для оператора Чиры (65–76), иначе None."""
    return CHIRY_LINE_STAGE.get(_norm(op_id))


PICKUP_POINTS = ("bar", "il_drive")

# Оператор → точка выдачи (spr/Задачи для GPT.txt)
_PICKUP_BY_OPER = {
    "bar_staff": "bar",       # 31–40
    "grill_staff": "il_drive",  # 41–50, ОкноГриль
    "delivery": "bar",        # 60
    "discount": "bar",        # 56–60
    "guest_table": "bar",     # 1–25
    "guest_takeaway": "bar",  # 26–30
}


def _oper_pickup_role(op_id: str) -> str:
    if is_superuser(op_id):
        return "admin"
    if _in_range(op_id, 31, 40):
        return "bar_staff"
    if _in_range(op_id, 41, 50):
        return "grill_staff"
    if _norm(op_id) == DELIVERY_OPER_ID:
        return "delivery"
    if _in_range(op_id, *_RANGE_DISCOUNT):
        return "discount"
    if _in_range(op_id, *_RANGE_GUEST_TABLE):
        return "guest_table"
    if _in_range(op_id, *_RANGE_GUEST_TAKEAWAY):
        return "guest_takeaway"
    return "guest"


def get_pickup_by_id(op_id: str | None) -> str:
    """Точка выдачи по oper_id создавшего заказ."""
    op = _norm(op_id)
    if not op:
        return "bar"
    role = _oper_pickup_role(op)
    if role == "admin":
        return "bar"
    return _PICKUP_BY_OPER.get(role, "bar")


def get_pickup_for_item(op_id: str | None, name_id) -> str:
    """Точка выдачи для позиции: oper_id + категория меню."""
    op = _norm(op_id)
    nid = name_id_int(name_id)
    base = get_pickup_by_id(op)
    # Персонал ОкноГриль — всё на il_drive
    if base == "il_drive":
        return "il_drive"
    # Барные позиции (101–400) — выдача на баре
    if 101 <= nid <= 400:
        return "bar"
    # Гриль (601–700) при заказе не с окна — выдача на баре (стол/самовынос)
    if 601 <= nid <= 700:
        return "bar"
    # Кухня (401–600) — выдача на баре (официант/самовынос)
    return "bar"


def pickup_label(pickup_point: str | None) -> str:
    return "ОкноГриль" if pickup_point == "il_drive" else "Бар"


def is_pickup_queue_view(op_id: str | None, station: str | None) -> bool:
    """Режим очереди выдачи на баре/окне (без фильтра станции)."""
    if station:
        return False
    op = _norm(op_id)
    if not op:
        return False
    if is_superuser(op):
        return True
    return _in_range(op, 31, 40) or _in_range(op, 41, 50)


def is_waiter_serve_view(op_id: str | None, view: str | None) -> bool:
    """Пульт официанта: готовые блюда для подачи за стол (не только бар)."""
    if (view or "").strip().lower() != "waiter":
        return False
    op = _norm(op_id)
    if not op:
        return False
    if is_superuser(op):
        return True
    return can_access_form(op, "waiter_pos")


def name_id_int(name_id) -> int:
    try:
        return int(str(name_id))
    except (ValueError, TypeError):
        return 0


def is_alcohol_item(name_id) -> bool:
    nid = name_id_int(name_id)
    return any(lo <= nid <= hi for lo, hi in ALCOHOL_RANGES)


def menu_category(name_id) -> str | None:
    nid = name_id_int(name_id)
    for cat, (lo, hi) in MENU_RANGES.items():
        if lo <= nid <= hi:
            return cat
    return None


def is_delivery(op_id: str | None) -> bool:
    return _norm(op_id) == DELIVERY_OPER_ID


def is_discount_operator(op_id: str | None) -> bool:
    return _in_range(_norm(op_id), *_RANGE_DISCOUNT)


def parse_discount_pct(lastname: str | None) -> float | None:
    if lastname is None:
        return None
    s = str(lastname).strip().replace(",", ".")
    if not s or s.lower() in {"none", "null", "-"}:
        return None
    try:
        pct = float(s)
        if 0 <= pct <= 100:
            return pct
    except ValueError:
        pass
    return None


def get_operator_discount_pct(op_id: str | None, lastname: str | None = None) -> float:
    op = _norm(op_id)
    if is_superuser(op):
        return 100.0
    if is_delivery(op):
        return DELIVERY_DISCOUNT_PCT
    if is_discount_operator(op):
        pct = parse_discount_pct(lastname)
        if pct is not None:
            return pct
    return 0.0


def max_manual_discount_pct(op_id: str | None, lastname: str | None = None) -> float:
    """Максимальная скидка, которую оператор может выставить вручную."""
    op = _norm(op_id)
    if is_superuser(op):
        return 100.0
    if is_discount_operator(op):
        return get_operator_discount_pct(op, lastname)
    if _in_range(op, 31, 40):
        return 100.0
    return 0.0


def can_set_manual_discount(op_id: str | None) -> bool:
    op = _norm(op_id)
    return is_superuser(op) or _in_range(op, 31, 40) or is_discount_operator(op)


def can_order_item(op_id: str | None, name_id) -> tuple[bool, str]:
    op = _norm(op_id)
    if is_delivery(op) and is_alcohol_item(name_id):
        return False, "Доставка: заказ алкоголя запрещён"
    return True, ""


def can_create_order(op_id: str | None) -> bool:
    op = _norm(op_id)
    if not op:
        return False
    if is_superuser(op):
        return True
    if _in_any_range(op, _RANGE_DEFAULT):
        return True
    if _in_range(op, *_RANGE_GUEST_TABLE) or _in_range(op, *_RANGE_GUEST_TAKEAWAY):
        return True
    return False


def filter_menu_for_operator(items: list[dict], op_id: str | None) -> list[dict]:
    if not is_delivery(_norm(op_id)):
        return items
    return [i for i in items if not is_alcohol_item(i.get("name_id"))]


def operator_permissions(op_id: str | None, lastname: str | None = None) -> dict:
    op = _norm(op_id)
    disc = get_operator_discount_pct(op, lastname)
    return {
        "oper_id": op,
        "is_superuser": is_superuser(op),
        "is_delivery": is_delivery(op),
        "is_discount_operator": is_discount_operator(op),
        "allowed_forms": allowed_forms(op),
        "station": get_station_by_id(op),
        "pickup_point": get_pickup_by_id(op),
        "pickup_label": pickup_label(get_pickup_by_id(op)),
        "discount_pct": disc,
        "max_discount_pct": max_manual_discount_pct(op, lastname),
        "can_set_discount": can_set_manual_discount(op),
        "can_cancel": op in ALLOWED_CANCEL_IDS,
        "alcohol_banned": is_delivery(op),
        "can_impersonate": is_superuser(op),
    }
