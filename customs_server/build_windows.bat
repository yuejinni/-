@echo off
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo Python not found. Please install Python 3.9+ first.
    pause
    exit /b 1
)
python "%~dp0build.py"
