# C:\Offline Server\update_dict.py
import os
import sys
from datetime import datetime
import importlib.util

from core.config import BASE_DIR, SPR_DIR, ARCH_MENU_DIR, ARCH_OPER_DIR, LOG_PATH

TXT_DIR = SPR_DIR
LOG_FILE = str(LOG_PATH)

# Загрузчик manager.py без конфликтов с основным потоком
def load_manager():
    mgr_path = os.path.join(BASE_DIR, "db", "manager.py")
    spec = importlib.util.spec_from_file_location("db_manager", mgr_path)
    mgr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mgr)
    return mgr

def gen_ref(step):
    time_tag = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{time_tag}/{step}"

def log_ref(msg, ref):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ref}] {msg}\n")
    print(f"[{ref}] {msg}")

def validate_and_parse(txt_path, dict_type):
    expected_n = 6 if dict_type == "menu" else 5
    fields_menu = ["name_id","name","unit","price","description","category"]
    fields_oper = ["oper_id","oper","name","lastname","qr_data"]
    fields = fields_menu if dict_type == "menu" else fields_oper
    errors = []
    records = []
    try:
        with open(txt_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return [], ["Файл пуст"]
        lines = content.splitlines()
    except Exception as e:
        return [], [f"Ошибка чтения/кодировки: {e}"]
    
    for i, line in enumerate(lines, 1):
        parts = [p.strip() for p in line.split("*")]
        if len(parts) != expected_n:
            errors.append(f"Строка {i}: неверное число полей ({len(parts)} вместо {expected_n}). Строка: {line}")
            continue
        if any(p == "" for p in parts):
            errors.append(f"Строка {i}: обнаружено пустое поле. Строка: {line}")
            continue
        rec = dict(zip(fields, parts))
        if "price" in rec:
            try: rec["price"] = float(rec["price"])
            except: errors.append(f"Строка {i}: price должно быть числом. Строка: {line}"); continue
        records.append(rec)
    return records, errors

def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("menu", "oper"):
        print("Использование: python update_dict.py menu|oper")
        sys.exit(1)
    dict_type = sys.argv[1]
    today = datetime.now().strftime("%Y-%m-%d")
    txt_file = os.path.join(TXT_DIR, f"{dict_type}-{today}.txt")
    ref_root = gen_ref("UPDATE_INIT")

    if not os.path.exists(txt_file):
        log_ref("Нет файла за сегодня. Текущий справочник остаётся в базе.", ref_root)
        return

    # 1. Валидация
    ref_validate = gen_ref("VALIDATE")
    parsed, errs = validate_and_parse(txt_file, dict_type)
    log_ref(f"Структура проверена. Ошибок: {len(errs)}.", ref_validate)
    if errs:
        err_log = os.path.join(BASE_DIR, f"err_{dict_type}.log")
        with open(err_log, "w", encoding="utf-8") as ef:
            ef.write("\n".join(errs))
        log_ref(f"Лист ошибок сохранён: {err_log}", ref_validate)

    # 2. Загрузка текущего активного справочника через manager.py
    ref_load_cur = gen_ref("LOAD_CUR")
    mgr = load_manager()
    getter = getattr(mgr, f"get_active_{dict_type}", None)
    if not getter:
        log_ref(f"Внедрение: в db/manager.py отсутствует функция get_active_{dict_type}()", ref_load_cur)
        current_data = []
    else:
        current_data = getter()
        log_ref(f"Получено {len(current_data)} записей из активной БД.", ref_load_cur)

    # 3. Архивация текущего состояния
    ref_archive = gen_ref("ARCHIVE")
    if current_data:
        arch_dir = os.path.join(TXT_DIR, f"archiv_{dict_type}")
        os.makedirs(arch_dir, exist_ok=True)
        time_part = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        arch_name = f"{dict_type}-{time_part}.txt"
        arch_path = os.path.join(arch_dir, arch_name)
        with open(arch_path, "w", encoding="utf-8") as af:
            for rec in current_data:
                af.write("*".join(map(str, rec.values())) + "\n")
        log_ref(f"Архив создан: {arch_name}", ref_archive)
    else:
        arch_path = "N/A"
        log_ref("Текущая БД пуста, архив не создан.", ref_archive)

    # 4. Расчёт изменений
    cur_ids = set(r.get("name_id" if dict_type=="menu" else "oper_id") for r in current_data if r)
    new_ids = set(r.get("name_id" if dict_type=="menu" else "oper_id") for r in parsed if r)
    removed = len(cur_ids - new_ids)
    new_rec = len(new_ids - cur_ids)
    ref_calc = gen_ref("CALC_CHANGES")
    log_ref(f"Изменения: удалено/перемещено {removed}, новых записей {new_rec}.", ref_calc)

    # 5. Обновление БД
    ref_update = gen_ref("WRITE_DB")
    upserter = getattr(mgr, f"replace_{dict_type}", None)
    if not upserter:
        log_ref(f"Внедрение: в db/manager.py отсутствует функция replace_{dict_type}(data)", ref_update)
    else:
        upserter(parsed)
        log_ref(f"Новый справочник записан в базу ({len(parsed)} записей).", ref_update)

    # Итоговый лог
    log_ref(f"Операция обновления '{dict_type}' завершена. Архив: {arch_path}", ref_root)

if __name__ == "__main__":
    main()