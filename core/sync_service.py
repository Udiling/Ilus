# C:\Offline Server\core\sync_service.py
import os  # Импорт модуля для работы с операционной системой и файловыми путями
from datetime import datetime  # Импорт класса для получения текущей даты и времени

from core.config import BASE_DIR, SPR_DIR, ARCH_MENU_DIR, ARCH_OPER_DIR

def sync_dictionary(dict_type: str) -> dict:  # Функция синхронизации справочника, принимающая тип данных и возвращающая словарь статуса
    txt_file = os.path.join(BASE_DIR, "spr", f"{dict_type}-{datetime.now().strftime('%Y-%m-%d')}.txt")  # Формирование абсолютного пути к файлу справочника за текущую дату
    if not os.path.exists(txt_file):  # Проверка наличия файла на диске
        return {"status": "skipped", "reason": "Файл за сегодня отсутствует, текущий справочник актуален"}  # Возврат статуса пропуска синхронизации

    from db.manager import get_active_menu, get_active_oper, replace_menu, replace_oper  # Динамический импорт функций работы с БД (не рекомендуется делать внутри функции)
    getter_map = {"menu": get_active_menu, "oper": get_active_oper}  # Словарь сопоставления типа справочника и функции чтения данных
    upserter_map = {"menu": replace_menu, "oper": replace_oper}  # Словарь сопоставления типа справочника и функции записи данных
    
    getter = getter_map[dict_type]  # Выбор функции чтения на основе переданного типа
    upserter = upserter_map[dict_type]  # Выбор функции записи на основе переданного типа

    current = getter()  # Получение текущего состояния справочника из базы данных
    arch_dir = ARCH_MENU_DIR if dict_type == "menu" else ARCH_OPER_DIR  # Формирование пути к папке архива для данного типа данных
    os.makedirs(arch_dir, exist_ok=True)  # Создание директории архива без ошибки, если она уже существует
    arch_name = f"{dict_type}-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"  # Генерация имени архивного файла с меткой времени (исправлена синтаксическая ошибка оригинала)
    arch_path = os.path.join(arch_dir, arch_name)  # Формирование полного пути к архивному файлу

    with open(arch_path, "w", encoding="utf-8") as af:  # Открытие архивного файла на запись в кодировке UTF-8
        for rec in current:  # Перебор всех записей, извлечённых из базы
            af.write("*".join(str(v) for v in rec.values()) + "\n")  # Сериализация значений записи в строку через разделитель * и запись с переносом

    valid_records = []  # Инициализация буфера для валидных записей из нового файла
    cols_menu = ["name_id", "name", "unit", "price", "description", "category"]  # Ожидаемые имена полей для справочника меню
    cols_oper = ["oper_id", "oper", "name", "lastname", "qr_data"]  # Ожидаемые имена полей для справочника операторов
    expected = 6 if dict_type == "menu" else 5  # Определение допустимого количества колонок в строке
    fields = cols_menu if dict_type == "menu" else cols_oper  # Выбор списка полей под текущий тип справочника

    with open(txt_file, "r", encoding="utf-8") as f:  # Открытие нового файла справочника на чтение
        for line in f:  # Построчный перебор содержимого
            line = line.strip()  # Удаление конечных пробелов и символов перевода строки
            if not line:  # Пропуск пустых строк
                continue  # Переход к следующей строке
            parts = [p.strip() for p in line.split("*")]  # Разделение строки по символу * и очистка каждого фрагмента
            if len(parts) != expected or any(p == "" for p in parts):  # Валидация: проверка количества полей и отсутствие пустых значений
                continue  # Пропуск некорректной строки
            if dict_type == "menu":  # Специфическая обработка для меню
                try:  # Блок защиты от ошибок преобразования типов
                    parts[3] = float(parts[3])  # Преобразование цены из строки в число с плавающей точкой
                except ValueError:  # Обработка ситуации, когда строка не является валидным числом
                    continue  # Пропуск строки с некорректной ценой
            valid_records.append(dict(zip(fields, parts)))  # Сопоставление имён полей и значений, добавление словаря в список валидных записей

    if not valid_records:  # Проверка, удалось ли собрать хотя бы одну корректную запись
        return {"status": "empty", "archived": arch_name, "loaded": 0}  # Возврат статуса пустого файла с указанием имени архива

    upserter(valid_records)  # Вызов функции массовой замены данных в базе
    return {"status": "synced", "archived": arch_name, "new_records": len(valid_records)}  # Возврат статуса успешной синхронизации и количества загруженных записей

