@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
    echo [准备环境] 首次运行需要创建虚拟环境并安装依赖，约 1-3 分钟...
    set "PY="
    where python >nul 2>nul && set "PY=python"
    if not defined PY (
        echo.
        echo [错误] 没找到 Python。
        echo 请安装 Python 3.11 或更高版本：https://www.python.org/downloads/
        echo 安装时记得勾选 "Add Python to PATH"，然后重试。
        echo.
        pause
        exit /b 1
    )
    "!PY!" -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络连接后重试。
        pause
        exit /b 1
    )
    echo [准备环境] 完成！
    echo.
)

".venv\Scripts\python.exe" app.py
pause
