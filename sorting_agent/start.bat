@echo off
chcp 65001 >nul
echo === sorting_agent start ===

:: 切换到脚本所在目录（双击启动时保证路径正确）
cd /d "%~dp0"

:: 如需激活虚拟环境，取消下行注释并修改路径
:: call .venv\Scripts\activate.bat

:: 可在此设置环境变量（覆盖 config.json 中的值）
:: set SORTING_MOCK_PLC=1

python main.py
pause
