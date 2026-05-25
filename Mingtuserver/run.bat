@echo off
chcp 65001 >nul 2>&1
title MingTu Navigation - Start Services

echo ============================================================
echo   MingTu Navigation - One-Click Start
echo ============================================================
echo.

:: ==================== Get Server Root Directory ====================
set "SERVER_DIR=%~dp0"
set "SERVER_DIR=%SERVER_DIR:~0,-1%"

:: ==================== Load .env Environment Variables ====================
if exist "%SERVER_DIR%\.env" (
    echo [Config] Loading environment variables...
    for /f "usebackq tokens=1,* delims==" %%a in ("%SERVER_DIR%\.env") do (
        set "%%a=%%b"
    )
    echo       Environment variables loaded
    echo.
)

:: ==================== Check Virtual Environment ====================
if not exist "%SERVER_DIR%\venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found. Please run install_all.bat first.
    pause
    exit /b 1
)

set "VENV_PY=%SERVER_DIR%\venv\Scripts\python.exe"

:: ==================== Start Service 1: Assistant AI ====================
echo [1/3] Starting Assistant AI (assistant_server.py) ...
start "Assistant AI" cmd /k "cd /d "%SERVER_DIR%" && "%VENV_PY%" assistant_server.py"
timeout /t 3 /nobreak >nul

:: ==================== Start Service 2: GPS AI ====================
:: Note: GPS AI files (config.py, amap_service.py, navigation_engine.py) are in gps\ directory
:: gps_app.py will add gps\ to sys.path automatically
echo [2/3] Starting GPS AI (gps\gps_app.py) ...
start "GPS AI" cmd /k "cd /d "%SERVER_DIR%\gps" && "%VENV_PY%" gps_app.py"
timeout /t 3 /nobreak >nul

:: ==================== Start Service 3: Vision AI ====================
echo [3/3] Starting Vision AI (vision\yolo_websocket_server.py) ...
start "Vision AI" cmd /k "cd /d "%SERVER_DIR%\vision" && "%VENV_PY%" yolo_websocket_server.py"

echo.
echo ============================================================
echo   All Services Started (3 windows)
echo ============================================================
echo.
echo   Assistant AI  - Port 8766 (App WebSocket) + 8767 (GPS AI WebSocket)
echo   GPS AI        - Port 5000 (REST API)
echo   Vision AI     - Port 8768 (WebSocket)
echo.
echo   Close the windows to stop the corresponding service
echo ============================================================
echo.
pause
