@echo off
setlocal

REM Проверяем, есть ли файл .cursorignore, если нет — создаём
if not exist ".cursorignore" (
    echo Создаю .cursorignore в корне проекта...
) else (
    echo Файл .cursorignore уже есть, перезаписываю содержимое...
)

REM Пишем содержимое файла .cursorignore
(
echo # Игнорируем базу данных (самое важное для твоего кафе-сервера)
echo *.db
echo *.sqlite
echo *.sqlite3
echo *.db-journal
echo.
echo # Игнорируем виртуальное окружение (чтобы не индексировал пакеты)
echo .venv/
echo venv/
echo env/
echo.
echo # Кэш Python
echo __pycache__/
echo *.pyc
echo.
echo # Логи и временные файлы
echo logs/
echo *.log
echo node_modules/
) > .cursorignore

echo Готово! Файл .cursorignore создан/обновлён в текущей папке.
echo Содержимое:
type .cursorignore
pause
