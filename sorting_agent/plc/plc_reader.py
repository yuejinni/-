"""
plc/plc_reader.py — PLC 状态读取循环

替代原 C# ReadCarPLC + ReadBtnPLC。
⚠️ 读 PLC 也加 _plc_lock，与写操作串行，snap7 C 库不保证多线程安全。
"""
import struct
import time
import threading
import logging

from plc.plc_client import _plc_lock, LIGHT_GREEN, LIGHT_YELLOW, LIGHT_YELLOW_FLASH, LIGHT_OFF

logger = logging.getLogger(__name__)


def read_car_status_loop(db_conn, plc):
    """
    每 100ms 读 DB201[0-899]，150辆小车，每辆 6 字节。
    替代原 C# ReadCarPLC。
    ⚠️ 线程启动时传入独立 db_conn，整个循环内复用，不每次迭代新建。
    """
    while True:
        try:
            with _plc_lock:
                buf = plc.db_read(201, 0, 900)  # 150 × 6 = 900 字节
            cursor = db_conn.cursor()
            for i in range(150):
                syno   = struct.unpack_from('>i', buf, 6 * i)[0]
                portno = struct.unpack_from('>h', buf, 6 * i + 4)[0]
                carno  = i + 1
                cursor.execute("""
                    IF EXISTS (SELECT 1 FROM car_status WHERE carno=?)
                        UPDATE car_status SET syno=?, portno=?, last_updated=GETDATE() WHERE carno=?
                    ELSE
                        INSERT INTO car_status(carno, syno, portno) VALUES(?, ?, ?)
                """, (carno, syno, portno, carno, carno, syno, portno))
            db_conn.commit()
        except Exception as e:
            logger.warning(f"[car_status_loop] 读取异常: {e}")
        time.sleep(0.1)


def read_button_status(plc) -> list[bool]:
    """
    读 DB201[900-924]，25字节=200bit（格口按钮[1..200] Bool）。
    返回 200 个布尔值（索引 0 = 格口 1）。
    """
    buf = plc.db_read(201, 900, 25)
    return [bool((buf[b] >> bit) & 1) for b in range(25) for bit in range(8)]


def read_button_loop(db_conn, plc, config: dict):
    """
    每 500ms 轮询 DB201[900-924]，检测格口按钮上升沿事件。
    按钮按下且格口绿灯（init_num=fj_num!=0）时，异步触发打印面单。
    ⚠️ print_port_label 含 PDF 生成（3-10s），必须用 daemon thread 异步执行，不阻塞本循环。
    """
    from core.db import qone, get_db_conn
    from print.print_manager import print_port_label

    prev_states = [False] * 200
    while True:
        try:
            with _plc_lock:
                states = read_button_status(plc)
            for i, (prev, curr) in enumerate(zip(prev_states, states)):
                portno = i + 1
                if not prev and curr:   # 上升沿：按钮被按下
                    port = qone(db_conn,
                        "SELECT init_num, fj_num FROM sort_ports WHERE portno=?", (portno,))
                    if port and port["fj_num"] == port["init_num"] != 0:
                        # 绿灯状态（全部落包）→ 异步打印
                        threading.Thread(
                            target=print_port_label,
                            args=(get_db_conn(), portno,
                                  config.get("printer_name", ""),
                                  config.get("label_template", "print/templates/label.html"),
                                  config.get("wkhtmltopdf_path", "wkhtmltopdf.exe")),
                            daemon=True
                        ).start()
                        # 异步清格口 + 写灯（不阻塞按钮轮询）
                        threading.Thread(
                            target=_release_port_after_button,
                            args=(get_db_conn(), plc, portno),
                            daemon=True
                        ).start()
            prev_states = states
        except Exception as e:
            logger.warning(f"[button_loop] 读取异常: {e}")
        time.sleep(0.5)


def _release_port_after_button(db_conn, plc, portno: int):
    """
    按钮按下后异步执行：
    1. auto_update_port — 清格口或把溢出包分配进来
    2. 写灯 — 有溢出重分配 → 黄灯闪烁(5)，无 → 全灭(0)
    """
    from core.port_manager import auto_update_port
    from plc.plc_client import write_port_light
    try:
        has_overflow = auto_update_port(db_conn, portno)
        light = LIGHT_YELLOW_FLASH if has_overflow else LIGHT_OFF
        write_port_light(db_conn, plc, portno, light)
        logger.info(f"[button_loop] 格口 {portno} 清格口完成，写灯={light}")
    except Exception as e:
        logger.warning(f"[button_loop] 清格口失败 portno={portno}: {e}")
