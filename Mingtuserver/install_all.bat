@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title MingTu Navigation - Install

echo ============================================================
echo   MingTu Navigation - One-Click Install
echo ============================================================
echo.

:: ==================== Check Python ====================
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.11/3.12
    echo Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

:: ==================== Create Virtual Environment ====================
if not exist "venv" (
    echo [1/4] Creating virtual environment...
    python -m venv venv
) else (
    echo [1/4] Virtual environment already exists, skipping
)

:: ==================== Activate Virtual Environment ====================
echo [2/4] Activating virtual environment...
call venv\Scripts\activate.bat

:: ==================== Upgrade pip ====================
echo [3/4] Upgrading pip...
python -m pip install --upgrade pip -q

:: ==================== Install Dependencies ====================
echo [4/4] Installing dependencies...

:: Assistant AI dependencies
if exist "requirements_assistant.txt" (
    echo   - Installing Assistant AI dependencies...
    pip install -r requirements_assistant.txt --quiet
) else (
    echo   - Installing Assistant AI dependencies (built-in list)...
    pip install websockets websocket-client requests httpx --quiet
)

:: GPS AI dependencies
if exist "requirements_gps.txt" (
    echo   - Installing GPS AI dependencies...
    pip install -r requirements_gps.txt --quiet
) else (
    echo   - Installing GPS AI dependencies (built-in list)...
    pip install flask flask-cors requests --quiet
)

:: Vision AI dependencies
if exist "requirements_vision.txt" (
    echo   - Installing Vision AI dependencies...
    pip install -r requirements_vision.txt --quiet
) else (
    echo   - Installing Vision AI dependencies (built-in list)...
    pip install websockets opencv-python-headless Pillow numpy ultralytics rapidocr_onnxruntime --quiet
)

:: ==================== Check Model Files ====================
echo.
echo [Check] Vision AI model files...
set "MODELS_DIR=vision_ai\models"
if not exist "%MODELS_DIR%\custom_seg.pt" (
    echo   [Warning] custom_seg.pt not found (blind road segmentation model)
)
if not exist "%MODELS_DIR%\yolov8s.pt" (
    echo   [Warning] yolov8s.pt not found (general detection model)
)
if not exist "%MODELS_DIR%\traffic_light.pt" (
    echo   [Warning] traffic_light.pt not found (traffic light model)
)

:: ==================== Create .env Template ====================
if not exist ".env" (
    echo.
    echo [Config] Creating .env template...
    (
        echo # MingTu Navigation API Key Configuration
        echo # Amap Key: https://console.amap.com/
        echo AMAP_API_KEY=your_amap_key_here
        echo.
        echo # DeepSeek Key: https://platform.deepseek.com/
        echo DEEPSEEK_API_KEY=your_deepseek_key_here
    ) > .env
    echo   .env file created. Please edit and fill in your API keys.
)

echo.
echo ============================================================
echo   Installation Complete!
echo ============================================================
echo.
echo   Next Steps:
echo   1. Edit .env file and fill in your API keys
echo   2. Run run.bat to start the services
echo.
pause
