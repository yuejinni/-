@echo off
chcp 65001 >nul
title 报关文件生成服务（端口 5008）

:: ── 优先使用打包好的 exe ──────────────────────────────────
if exist "%~dp0server.exe" (
    echo 正在启动报关服务（exe 版）...
    start "" "%~dp0server.exe"
    goto :started
)

:: ── 没有 exe，改用 Python 启动 ────────────────────────────
echo 未找到 server.exe，尝试用 Python 启动...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo ❌ 未找到 Python！请先安装 Python 3.9+
    echo    下载：https://www.python.org/downloads/
    echo    安装时勾选 "Add Python to PATH"
    pause
    exit /b 1
)

echo 安装/更新依赖（首次较慢，请稍候）...
pip install flask flask-cors openpyxl xlrd --quiet

echo 启动服务...
start "" python "%~dp0server.py"

:started
echo.
echo ✅ 报关服务已启动，端口 5008
echo    关闭窗口不会停止服务
echo    如需停止：打开任务管理器，结束 server.exe 或 python.exe 进程
echo.
timeout /t 3
