"""
plc/plc_client.py — snap7 PLC 连接封装

⚠️ _plc_lock 定义在本模块级，所有模块 import 同一个锁对象，保证 PLC 读写串行。
   snap7 底层 C 库不保证多线程安全。
"""
import struct
import threading
import logging

logger = logging.getLogger(__name__)

# 全局 PLC 锁：所有 PLC 读写操作（含 plc_reader）均使用此锁
_plc_lock = threading.Lock()

# 格口灯状态常量（DB200.格口状态 USInt，A08程序确认）
LIGHT_OFF          = 0  # 全灭（空闲）
LIGHT_GREEN        = 1  # 绿灯常亮（完成落格）
LIGHT_YELLOW       = 2  # 黄灯常亮（超时等待 >40min）
LIGHT_RED          = 3  # 红灯常亮（格口关闭）
LIGHT_RED_FLASH    = 4  # 红灯闪烁（强制完成/缺货）
LIGHT_YELLOW_FLASH = 5  # 黄灯闪烁（等待手工配货）


def connect_plc(ip: str):
    """
    连接 S7-1200 PLC，返回 snap7.client.Client 实例。
    config.json 中 use_mock_plc=true 时返回 MockPLC。
    """
    import json, os
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config.json')
    with open(config_path, encoding='utf-8') as f:
        cfg = json.load(f)

    if cfg.get('use_mock_plc', False):
        from plc.plc_mock import MockPLC
        plc = MockPLC()
        plc.connect(ip)
        logger.info(f"[PLC] 使用 MockPLC（use_mock_plc=true）")
    else:
        import snap7
        plc = snap7.client.Client()
        plc.connect(ip, 0, 1)
        logger.info(f"[PLC] 已连接 {ip}")

    return plc


def write_start_plc(plc, carno: int, port: int, serialnum: int, syno: int = 0):
    """
    写 PLC DB200（10字节大端序）相机信号。
    PLC字段布局：
      bytes 0-3: syno     (UDInt, 4B) — 序列号（毫秒精度时间戳）
      bytes 4-5: port     (UInt,  2B) — 格口号（innerport）
      bytes 6-7: carno    (UInt,  2B) — 小车号
      bytes 8-9: serialnum(UInt,  2B) — 喷码号
    ⚠️ 使用 innerport（非 portno），溢出包 innerport=0 时不应调用此函数。
    """
    buf = bytearray(10)
    struct.pack_into('>I', buf, 0, syno)        # UDInt 4B — 序列号
    struct.pack_into('>H', buf, 4, port)        # UInt  2B — 格口号
    struct.pack_into('>H', buf, 6, carno)       # UInt  2B — 小车号
    struct.pack_into('>H', buf, 8, serialnum)   # UInt  2B — 喷码号
    with _plc_lock:
        plc.db_write(200, 0, bytes(buf))


def write_port_light(db_conn, plc, portno: int, light_val: int):
    """
    写单个格口灯，DB200 offset = 9 + portno。
    同时回写 port_lights 表（本地镜像，供看板/API 读取）。
    ⚠️ 需 HMI.主机模式=TRUE。
    """
    from core.db import execute
    with _plc_lock:
        plc.db_write(200, 9 + portno, bytes([light_val]))
    execute(db_conn,
        "UPDATE port_lights SET light_val=?, last_updated=GETDATE() WHERE portno=?",
        (light_val, portno))
    db_conn.commit()


def write_all_port_lights(db_conn, plc, light_values: list):
    """
    批量写 200 个格口灯，DB200 offset=10，共 200 字节。
    同步回写 port_lights 表。
    """
    from core.db import execute
    vals = light_values[:200]
    with _plc_lock:
        plc.db_write(200, 10, bytes(vals))
    for i, v in enumerate(vals, start=1):
        execute(db_conn,
            "UPDATE port_lights SET light_val=?, last_updated=GETDATE() WHERE portno=?",
            (v, i))
    db_conn.commit()
