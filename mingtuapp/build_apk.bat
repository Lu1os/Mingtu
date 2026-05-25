@echo off
chcp 65001 >nul 2>&1
title 明途App - 一键编译

echo ============================================
echo   明途App - APK一键编译工具
echo ============================================
echo.

cd /d "%~dp0"

set ANDROID_HOME=C:\Users\29121\AppData\Local\Android\Sdk
set ANDROID_SDK_ROOT=%ANDROID_HOME%
echo sdk.dir=%ANDROID_HOME% > local.properties
echo [OK] local.properties 已生成

echo.
echo [1/3] 开始编译Debug版APK...
echo       首次编译需要下载依赖，请耐心等待5-10分钟...
echo.

call gradlew.bat assembleDebug

if %errorlevel% neq 0 (
    echo.
    echo ============================================
    echo   [失败] 编译出错！请查看上方错误信息
    echo ============================================
    pause
    exit /b 1
)

echo.
echo ============================================
echo   [成功] APK已生成！
echo ============================================
echo.
for %%f in (app\build\outputs\apk\debug\*.apk) do echo   APK路径: %%ff
echo.
pause
