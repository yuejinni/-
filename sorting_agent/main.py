"""
main.py — 分拣机 Python Agent 入口

启动 6 个后台线程 + asyncio TCP 服务器：
  1. read_car_status_loop  — PLC 小车状态（100ms 轮询）
  2. read_button_loop      — 格口按钮检测（500ms 轮询）
  3. port_monitor_loop     — 格口超时告警（5s 轮询）
  4. rule_sync_loop        — 云端规则同步（30s 轮询）
  5. event_push_loop       — 扫码事件推送（3s 批量）
  6. run_flask_app         — Flask Web API（:5009，waitress）
  7. start_tcp_server      — TCP 扫码枪（:8888，主线程 asyncio）

⚠️ pyodbc Connection 不是线程安全的。
   每个线程启动时独立调用 get_db_conn()，不共享同一 connection 对象。
"""
import asyncio
import json
import logging
import os
import sys
import threading

# 确保 sorting_agent 根目录在 sys.path 中（Windows 双击 start.bat 时）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    config_path = os.path.join(BASE_DIR, 'config.json')
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"config.json 不存在，请从 config.example.json 复制并填写配置：{config_path}"
        )
    with open(config_path, encoding='utf-8') as f:
        return json.load(f)


def main():
    config = load_config()
    logger.info("=== 分拣机 Python Agent 启动 ===")

    from plc.plc_client import connect_plc
    from plc.plc_reader import read_car_status_loop, read_button_loop
    from core.db import get_db_conn
    from core.port_manager import port_monitor_loop
    from sync.rule_sync import rule_sync_loop
    from sync.event_push import event_push_loop
    from api.web_api import run_flask_app
    from tcp.tcp_server import start_tcp_server

    plc = connect_plc(config['plc_ip'])

    flask_port = config.get('flask_port', 5009)
    tcp_port   = config.get('tcp_port', 8888)

    # 后台线程（均为 daemon，主线程退出时自动终止）
    threads = [
        threading.Thread(target=read_car_status_loop,
                         args=(get_db_conn(), plc), daemon=True,
                         name="car-status-loop"),
        threading.Thread(target=read_button_loop,
                         args=(get_db_conn(), plc, config), daemon=True,
                         name="button-loop"),
        threading.Thread(target=port_monitor_loop,
                         daemon=True, name="port-monitor"),
        threading.Thread(target=rule_sync_loop,
                         daemon=True, name="rule-sync"),
        threading.Thread(target=event_push_loop,
                         daemon=True, name="event-push"),
        threading.Thread(target=run_flask_app,
                         args=(flask_port,), daemon=True, name="flask"),
    ]
    for t in threads:
        t.start()
        logger.info(f"线程已启动：{t.name}")

    # 主线程运行 TCP 服务器（asyncio event loop）
    logger.info(f"TCP 服务器启动，监听 :{tcp_port}")
    asyncio.run(start_tcp_server(get_db_conn(), plc, port=tcp_port))


if __name__ == '__main__':
    main()
