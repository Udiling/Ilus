import sys
import os
from datetime import datetime

base_dir = r"C:\Offline Server"
spr_dir = os.path.join(base_dir, "spr")
log_file = os.path.join(base_dir, "load.log")

sys.path.insert(0, base_dir)
from utils.qr_manager import generate_qr

oper_files = [f for f in os.listdir(spr_dir) if f.startswith("oper-") and f.endswith(".txt")]
if not oper_files:
    print("[ОШИБКА] Файл oper-*.txt не найден в spr/")
    sys.exit(1)

target_file = sorted(oper_files)[-1]
file_path = os.path.join(spr_dir, target_file)
print(f"[INFO] Обработка файла: {target_file}")

count = 0
with open(file_path, "r", encoding="utf-8") as fin, open(log_file, "a", encoding="utf-8") as lf:
    for line in fin:
        line = line.strip()
        if not line:
            continue
        try:
            p = line.split("*")
            if len(p) < 5:
                continue
            oid = p[0].strip()
            role_field = p[1].strip().lower()

            role = "guest"
            if any(k in role_field for k in ["кассир", "cashier"]):
                role = "cashier"
            elif any(k in role_field for k in ["кухня", "kitchen"]):
                role = "kitchen"
            elif any(k in role_field for k in ["админ", "admin", "свои"]):
                role = "admin"

            path = generate_qr(oid, role, "default")
            bname = os.path.basename(path)
            ts = datetime.now().strftime("%Y%m%d%H%M%S")
            lf.write(f"[{ts}/QR_BAT] OK | {bname} | {oid} | {role}\n")
            count += 1
        except Exception as e:
            ts = datetime.now().strftime("%Y%m%d%H%M%S")
            lf.write(f"[{ts}/QR_BAT] ERR | {line} -> {str(e)}\n")

print(f"Генерация завершена. Создано QR-файлов: {count}")