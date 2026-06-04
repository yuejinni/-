#!/bin/bash
# macOS 启动脚本（开发用）
cd "$(dirname "$0")"

echo "正在检查依赖..."
pip3 install flask flask-cors openpyxl xlrd xlutils certifi --break-system-packages -q 2>/dev/null || \
pip3 install flask flask-cors openpyxl xlrd xlutils certifi -q

echo "✅ 报关服务启动中... http://localhost:5008"
python3 server.py
