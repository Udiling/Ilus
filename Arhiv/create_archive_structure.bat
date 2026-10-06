@echo off
chcp 65001 >nul
title Cafe Offline Server — создание структуры C:\Arhiv

set "BASE_DIR=C:\Arhiv"

echo [1/4] Создаю корневую папку C:\Arhiv...
if not exist "%BASE_DIR%" mkdir "%BASE_DIR%"

echo [2/4] Создаю папки: positions (файлы с ID), secure_rar (готовые архивы), logs, temp...
mkdir "%BASE_DIR%\positions"
mkdir "%BASE_DIR%\secure_rar"
mkdir "%BASE_DIR%\logs"
mkdir "%BASE_DIR%\temp"

echo [3/4] Добавляю .keep-файлы, чтобы папки не пропадали при очистке...
if not exist "%BASE_DIR%\logs\.keep" type nul > "%BASE_DIR%\logs\.keep"
if not exist "%BASE_DIR%\temp\.keep" type nul > "%BASE_DIR%\temp\.keep"
if not exist "%BASE_DIR%\positions\.keep" type nul > "%BASE_DIR%\positions\.keep"

echo [4/4] Готово.
echo.
echo Структура C:\Arhiv:
echo   positions  — сюда складываются файлы с уникальным ID (DOC-001_A123.jpg, DOC-001_B456.pdf и т.д.)
echo   secure_rar — сюда складываются готовые RAR-архивы по позициям
echo   logs       — логи архивации
echo   temp       — временные файлы
echo.
pause
