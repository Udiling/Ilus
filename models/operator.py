# C:\Offline Server\models\operator.py
from db.manager import get_conn  # Импорт функции безопасного получения соединения с основным хранилищем

def get_operator_id(code: str) -> int | None:  # Объявление функции поиска оператора; аннотация типов указывает на возврат целого числа или None
    with get_conn() as conn:  # Открытие соединения через контекстный менеджер (обеспечивает фиксацию или откат транзакции)
        c = conn.cursor()  # Создание курсора для изолированного выполнения запросов
        c.execute("SELECT id FROM operators WHERE code = ?", (code,))  # Параметризированный запрос, защищающий от SQL-инъекций
        row = c.fetchone()  # Получение первой найденной записи
        return row["id"] if row else None  # Безопасное извлечение ID: если запись есть — возвращает число, если нет — возвращает None
