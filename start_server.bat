@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title Offline Server

echo [1/5] Checking Python...
python -V >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Add Python to PATH.
    pause
    exit /b 1
)

echo [2/5] Checking dependencies...
python -c "import fastapi, uvicorn, sqlalchemy" >nul 2>&1
if errorlevel 1 (
    echo Installing: fastapi uvicorn sqlalchemy python-multipart...
    python -m pip install fastapi uvicorn sqlalchemy python-multipart
    if errorlevel 1 (
        echo ERROR: pip install failed.
        pause
        exit /b 1
    )
) else (
    python -c "import multipart" >nul 2>&1
    if errorlevel 1 (
        echo Installing python-multipart...
        python -m pip install python-multipart
    )
)

echo [3/5] Initializing database...
python -c "import sys; sys.path.insert(0, '.'); from db.manager import init_db; init_db()"
if errorlevel 1 (
    echo.
    echo ERROR: Database init failed. Server will not start.
    pause
    exit /b 1
)
echo Database initialized OK.

echo [4/5] Checking port 8080...
set "PORT_PID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8080" ^| findstr "LISTENING"') do set "PORT_PID=%%a"
if defined PORT_PID (
    echo Port 8080 in use by PID %PORT_PID%. Stopping old process...
    taskkill /PID %PORT_PID% /F >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Could not kill PID %PORT_PID%. Free port 8080 manually.
        pause
        exit /b 1
    )
    timeout /t 1 /nobreak >nul
)

echo [5/5] Starting server (uvicorn)...
echo Server: http://localhost:8080
echo Press Ctrl+C to stop.
echo.

python -m uvicorn main:app --host 127.0.0.1 --port 8080

if errorlevel 1 (
    echo.
    echo Server stopped with an error.
    pause
)

endlocal