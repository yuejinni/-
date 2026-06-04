@echo off
chcp 65001 >nul

set "EXE=%~dp0server.exe"

%SystemRoot%\System32\reg.exe add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "BaoGuanServer" /t REG_SZ /d "\"%EXE%\"" /f >nul

if %errorlevel% equ 0 (
    echo.
    echo [OK] 已设置开机自动启动
    echo      下次开机后服务将在后台自动运行，无需手动启动
    echo      如需取消，运行 卸载开机自启.bat
    echo.
) else (
    echo.
    echo [ERROR] 设置失败，请右键以管理员身份运行
    echo.
)
pause
