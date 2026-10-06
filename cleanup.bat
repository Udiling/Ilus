rem Как использовать:
rem Сухой прогон: дважды щёлкните cleanup.bat (DRY_RUN=1) — увидите, что будет удалено.
rem Реальная очистка: откройте файл, установите DRY_RUN=0, сохраните, запустите заново.
@echo off
setlocal enableextensions enabledelayedexpansion

rem ============================================
rem  Cleanup script for "Offline Server"
rem  Place: C:\Offline Server\cleanup.bat
rem  Не трогаем папку "Doc" и сам файл скрипта.
rem ============================================

rem --- НАСТРОЙКИ ---
set "PROJECT_DIR=%~dp0"
set "RETENTION_DAYS=30"      rem хранить файлы не старше N дней
set "DRY_RUN=1"              rem 1 = показать, что будет удалено; 0 = реально удалять
set "ROTATE_LOGS=1"          rem 1 = ротировать логи (с датой); 0 = обнулить логи

rem --- ЦЕЛЕВЫЕ ПАПКИ ДЛЯ ЧИСТКИ ---
set "TARGET1=export"
set "TARGET2=spr\archiv_menu"
set "TARGET3=spr\archiv_oper"
set "TARGET4=arch\menu"
set "TARGET5=arch\oper"
set "TARGET6=static\qr_codes"

rem --- ФАЙЛЫ ЛОГОВ ---
set "LOG1=load.log"
rem err_*.log — обрабатываем по шаблону ниже

rem --- ИСКЛЮЧЕНИЯ ---
set "FORBID_DIR=Doc"         rem документация — не трогать

echo.
echo [*] Проект: "%PROJECT_DIR%"
echo [*] DRY_RUN=%DRY_RUN%  (1 = имитация; 0 = реальная очистка)
echo [*] Храним файлы моложе %RETENTION_DAYS% дн.
echo [*] Исключение: "%FORBID_DIR%\" и "%~nx0"
echo.

choice /M "Продолжить очистку?" /C YN /N
if errorlevel 2 (
  echo Отмена пользователем.
  goto :EOF
)
echo.

rem --- ПРОВЕРКА, что работаем из корня проекта ---
if not exist "%PROJECT_DIR%main.py" (
  echo [WARN] Похоже, скрипт запущен не из корня проекта. Продолжить может быть небезопасно.
  choice /M "Все равно продолжить?" /C YN /N
  if errorlevel 2 goto :EOF
)

rem --- ФУНКЦИИ ---
rem :purge_older "relative\path"
:purge_older
set "REL=%~1"
set "ABS=%PROJECT_DIR%%REL%"
if /I "%REL%"=="%FORBID_DIR%" (
  echo [SKIP] "%REL%\" помечена как документация.
  goto :eof
)
if not exist "%ABS%" (
  echo [INFO] Папка не найдена: "%REL%"
  goto :eof
)

echo [DIR] "%REL%" — удаляем файлы старше %RETENTION_DAYS% дн.
if "%DRY_RUN%"=="1" (
  forfiles /P "%ABS%" /S /D -%RETENTION_DAYS% /C "cmd /c if @isdir==FALSE echo  DEL @path"
) else (
  forfiles /P "%ABS%" /S /D -%RETENTION_DAYS% /C "cmd /c if @isdir==FALSE del /q @path"
)

rem удаляем пустые подкаталоги
if not "%DRY_RUN%"=="1" (
  for /f "delims=" %%D in ('dir "%ABS%" /ad /b /s ^| sort /R') do rd "%%~fD" 2>nul
)
goto :eof

rem :rotate_or_truncate "file"
:rotate_or_truncate
set "F=%PROJECT_DIR%%~1"
if not exist "%F%" (
  echo [LOG] Нет файла: "%~1"
  goto :eof
)
if "%ROTATE_LOGS%"=="1" (
  for /f "tokens=1 delims=." %%d in ("%~n1") do set "BASENAME=%%d"
  set "DATESTR=%DATE:~6,4%-%DATE:~3,2%-%DATE:~0,2%"
  set "ROT=%PROJECT_DIR%%~dp1%BASENAME%_%DATESTR%.log"
  echo [LOG] Ротация "%~1" -^> "%ROT%"
  if not "%DRY_RUN%"=="1" (
    move /Y "%F%" "%ROT%" >nul 2>nul
    type nul > "%F%"
  ) else (
    echo MOVE "%F%" "%ROT%"
    echo ECHO (truncate) ^> "%F%"
  )
) else (
  echo [LOG] Обнуление "%~1"
  if not "%DRY_RUN%"=="1" (
    type nul > "%F%"
  ) else (
    echo ECHO (truncate) ^> "%F%"
  )
)
goto :eof

rem --- ОЧИСТКА ПАПОК ---
call :purge_older "%TARGET1%"
call :purge_older "%TARGET2%"
call :purge_older "%TARGET3%"
call :purge_older "%TARGET4%"
call :purge_older "%TARGET5%"
call :purge_older "%TARGET6%"

echo.
echo [*] Работа с логами...
call :rotate_or_truncate "%LOG1%"

rem err_*.log — ротация или обнуление
for /f "delims=" %%F in ('dir /b /a:-d "%PROJECT_DIR%err_*.log" 2^>nul') do (
  call :rotate_or_truncate "%%F"
)

echo.
if "%DRY_RUN%"=="1" (
  echo [DONE] Режим DRY_RUN. Ничего не удалено. Для реальной очистки установите DRY_RUN=0 вверху файла.
) else (
  echo [DONE] Очистка завершена.
)

endlocal
exit /b 0