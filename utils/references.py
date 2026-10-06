# utils/references.py с построчными комментариями:

import os  # Импорт модуля для работы с путями и файловой системой
from datetime import datetime  # Импорт класса для получения текущего времени
import sqlite3  # Импорт драйвера для работы с SQLite
import threading  # Импорт модуля для обеспечения потокобезопасности

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # Определение корневой директории проекта (на два уровня выше текущего файла)
SEQ_DB = os.path.join(BASE_DIR, "ref_seq.db")  # Формирование абсолютного пути к отдельной базе данных для счётчиков ссылок
_ref_lock = threading.Lock()  # Создание объекта блокировки для безопасного доступа к счётчикам из разных потоков FastAPI

def _ensure_db():  # Внутренняя функция инициализации базы счётчиков
    if not os.path.exists(SEQ_DB):  # Проверка отсутствия файла базы данных на диске
        conn = sqlite3.connect(SEQ_DB)  # Создание нового соединения с файлом
        conn.execute("CREATE TABLE IF NOT EXISTS ref_seq (key TEXT PRIMARY KEY, val INTEGER DEFAULT 0)")  # Создание таблицы счётчиков, если её ещё нет
        conn.commit()  # Подтверждение выполнения DDL-запроса
        conn.close()  # Закрытие соединения

_ensure_db()  # Вызов функции инициализации при первом импорте модуля

def generate_ref(action_type: str, identifier: str = None) -> str:  # Функция генерации уникального референсного номера
    ts = datetime.now().strftime("%Y%m%d%H%M%S")  # Получение текущей даты и времени в формате ГГГГММДДЧЧММСС
    if identifier:  # Проверка наличия переданного пользователем идентификатора
        clean = str(identifier).strip().replace("/", "_").split(",")[0]  # Очистка строки: удаление пробелов, замена слэшей на подчёркивания, обрезка до первой запятой
        return f"{ts}/{action_type}_{clean}"  # Возврат ссылки в формате время/тип_идентификатор (без инкремента)

    with _ref_lock:  # Захват блокировки потока для предотвращения гонок записи в счётчик
        conn = sqlite3.connect(SEQ_DB)  # Открытие соединения с базой счётчиков
        conn.execute("BEGIN")  # Явное начало транзакции для атомарности операций
        conn.execute("INSERT OR IGNORE INTO ref_seq (key) VALUES (?)", (action_type,))  # Создание записи типа операции, если её ещё нет в таблице
        row = conn.execute("SELECT val FROM ref_seq WHERE key = ?", (action_type,)).fetchone()  # Чтение текущего значения счётчика
        new_val = (row[0] if row else 0) + 1  # Инкремент значения (или установка 1, если записи ранее не было)
        conn.execute("UPDATE ref_seq SET val = ? WHERE key = ?", (new_val, action_type))  # Обновление счётчика в базе
        conn.commit()  # Фиксация изменений в транзакции
        conn.close()  # Закрытие соединения
    return f"{ts}/{action_type}_{new_val:05d}"  # Возврат ссылки с пятизначным порядковым номером (с ведущими нулями)