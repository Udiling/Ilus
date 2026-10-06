# C:\Offline Server\core\config.py
from pathlib import Path  # Импорт библиотеки для работы с файлами и путями
import json  # Чтение файла PIN-кодов
import os  # Импорт модуля для взаимодействия с операционной системой

BASE_DIR = Path(__file__).resolve().parent.parent  # Определение корневой папки проекта (на два уровня выше текущего файла)
DB_PATH = BASE_DIR / "cafe.db"  # Формирование абсолютного пути к базе данных
LOG_PATH = BASE_DIR / "load.log"  # Абсолютный путь к файлу журнала
SPR_DIR = BASE_DIR / "spr"  # Директория для временных справочников
ARCH_MENU_DIR = BASE_DIR / "arch" / "menu"  # Папка архива меню
ARCH_OPER_DIR = BASE_DIR / "arch" / "oper"  # Папка архива операций
STATIC_DIR = BASE_DIR / "static"  # Директория статических файлов
TEMPLATES_DIR = BASE_DIR / "templates"  # Директория HTML-шаблонов
REPORTS_DIR = BASE_DIR / "reports"  # Директория PDF-отчётов

for dir_path in [SPR_DIR, ARCH_MENU_DIR, ARCH_OPER_DIR, STATIC_DIR, TEMPLATES_DIR, REPORTS_DIR]:  # Перебор списка служебных папок
    dir_path.mkdir(parents=True, exist_ok=True)  # Создание папки без ошибки, если она уже существует

DB_SETTINGS = {  # Параметры подключения к SQLite
    "path": str(DB_PATH),  # Преобразование пути в строку для драйвера
    "timeout": 15,  # Время ожидания снятия блокировки таблицы
    "pragma_journal_mode": "WAL",  # Режим асинхронной записи для ускорения работы
    "pragma_synchronous": "NORMAL",  # Баланс производительности и сохранности данных
    "check_same_thread": False  # Разрешение доступа к соединению из других потоков
}

TABLES = {  # Единый реестр имён таблиц для всего проекта
    "menu": "menu",  # Фактическое имя таблицы позиций меню
    "operators": "oper",  # Фактическое имя таблицы сотрудников
    "transactions": "transactions"  # Фактическое имя таблицы транзакций
}

SERVER_SETTINGS = {  # Настройки запуска сервера
    "host": "0.0.0.0",  # Прослушивание всех сетевых интерфейсов
    "port": 8080,  # Стандартный порт веб-сервиса
    "reload": False  # Отключение горячей перезагрузки кода в продакшене
}

PINS_PATH = BASE_DIR / "pins.json"  # PIN-коды хранятся вне кода
DEFAULT_PINS = {  # Значения по умолчанию, если файла нет или ключ пропущен
    "root": "9999",        # PIN суперпользователей (0/52/77)
    "staff": "2222",       # PIN персонала бара (31–40): отмена/редактирование
    "chiry_line2": "5555", # PIN директора: пуск линии 2 Чиров
    "max_table": "7777",   # PIN директора: изменение числа столов
}

def load_pins() -> dict:  # Загрузка PIN-кодов из pins.json с подстановкой значений по умолчанию
    try:
        with open(PINS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return dict(DEFAULT_PINS)
    merged = dict(DEFAULT_PINS)
    for key in DEFAULT_PINS:
        if key in data and str(data[key]).strip():
            merged[key] = str(data[key]).strip()
    return merged

PINS = load_pins()  # Единая точка доступа к PIN-кодам
