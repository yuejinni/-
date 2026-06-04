@echo off
chcp 65001 >nul

%SystemRoot%\System32\reg.exe delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "BaoGuanServer" /f >nul 2>&1

echo.
echo [OK] 已取消开机自动启动
echo.
pause
