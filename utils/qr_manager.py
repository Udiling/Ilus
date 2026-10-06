# C:\Offline Server\utils\qr_manager.py
from datetime import datetime
import qrcode
from core.config import STATIC_DIR, LOG_PATH

QR_DIR = STATIC_DIR / "qr_codes"


def build_qr_string(oper_id: str, role: str, template_id: str = "default") -> str:
    """Формирует текстовую строку для кодирования в QR."""
    return f"QR|{oper_id}|{role}|{template_id}"


def generate_qr(oper_id: str, role: str, template_id: str = "default") -> str:
    """Генерирует, сохраняет QR-код и возвращает веб-путь к файлу."""
    qr_str = build_qr_string(oper_id, role, template_id)
    QR_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    ref = f"{now.strftime('%Y%m%d%H%M%S')}/QR_GEN"
    filename = f"qr_{oper_id}_{now.strftime('%Y%m%d')}.png"
    path = QR_DIR / filename

    try:
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(qr_str)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(str(path))
    except Exception as e:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ref}] Ошибка сохранения QR: {e}\n")
        raise RuntimeError(f"Не удалось сохранить QR-код: {e}")

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{ref}] Сформирован QR: {filename} | {qr_str}\n")

    return f"/static/qr_codes/{filename}"
