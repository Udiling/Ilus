@echo off
chcp 65001 >nul
title Создание структуры для архива (всё в кучу)

set "BASE_DIR=C:\Arhiv"

echo [1/3] Создаю корневую папку...
if not exist "%BASE_DIR%" mkdir "%BASE_DIR%"

echo [2/3] Создаю папки: позиции, архив, логи, временное...
mkdir "%BASE_DIR%\positions"
mkdir "%BASE_DIR%\secure_rar"
mkdir "%BASE_DIR%\logs"
mkdir "%BASE_DIR%\temp"

echo [3/3] Добавляю файлы-маркеры, чтобы папки не удалялись при очистке...
if not exist "%BASE_DIR%\logs\.keep" type nul > "%BASE_DIR%\logs\.keep"
if not exist "%BASE_DIR%\temp\.keep" type nul > "%BASE_DIR%\temp\.keep"

echo.
echo ГОТОВО. Всё в C:\Arhiv.
echo   - positions: сюда будут падать файлы с уникальным ID
echo   - secure_rar: сюда будут складываться готовые RAR-архивы
echo   - logs: логи
echo   - temp: временные файлы
echo.
pause