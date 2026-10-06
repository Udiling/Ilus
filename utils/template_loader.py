# C:\Offline Server\utils\template_loader.py
import os  # Импорт стандартного модуля для работы с файловой системой

# Формирование абсолютного пути к директории шаблонов (два уровня вверх от текущей папки utils)
from core.config import TEMPLATES_DIR

def load_ui_template(template_id: str) -> str:  # Функция загрузки содержимого HTML-файла по идентификатору
    path = os.path.join(TEMPLATES_DIR, f"{template_id}.html")  # Сборка полного пути к файлу (расширение отсутствует)
    if os.path.exists(path):  # Проверка существования запрошенного файла на диске
        with open(path, "r", encoding="utf-8") as f:  # Открытие файла в текстовом режиме с кодировкой UTF-8
            return f.read()  # Чтение всего содержимого и возврат строки
    # Резервный вариант: если указанный файл отсутствует, загружается базовый шаблон
    with open(os.path.join(TEMPLATES_DIR, "default.html"), "r", encoding="utf-8") as f:
        return f.read()  # Возврат содержимого файла default.