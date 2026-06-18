"""
core/port_manager.py — 格口管理

替代原存储过程：
  Proc_Updateportinfo  → try_reassign_overflow（扫码后自动触发）
  Proc_AutoUpdatePort  → auto_update_port（按钮触发）
  Proc_PortStatus      → get_port_status（看板告警）
"""
import time
import logging

from core.db import qone, qval, qall, execute, get_db_conn
from plc.plc_client import (
    write_port_light,
    LIGHT_OFF,
    LIGHT_GREEN,
    LIGHT_RED,
    LIGHT_YELLOW,
    LIGHT_YELLOW_FLASH,
)

logger = logging.getLogger(__name__)


def try_reassign_overflow(db_conn, scanned_innerport: int):
    """
    扫码完成后检查：如果该格口所有包裹都已落包（status=3），
    找最早的溢出条码（innerport=0）重新分配进来。
    替代原 Proc_Updateportinfo。
    ⚠️ UPDLOCK+HOLDLOCK：防两个扫码线程同时通过检查后双重触发重分配。
    """
    pending = qval(db_conn,
        "SELECT COUNT(*) FROM sorting_rules WITH (UPDLOCK, HOLDLOCK) "
        "WHERE innerport=? AND status<3",
        (scanned_innerport,))
    if pending and pending > 0:
        return  # 还有未落包，不处理

    overflow_rows = qall(db_conn,
        "SELECT id, barcode, portno, queue_seq FROM sorting_rules "
        "WHERE innerport=0 AND status=1 ORDER BY queue_seq ASC, id ASC")

    if not overflow_rows:
        # 无溢出，直接标格口已清
        try:
            execute(db_conn,
                "UPDATE sorting_rules SET status=4 WHERE innerport=?",
                (scanned_innerport,))
            db_conn.commit()
        except Exception:
            db_conn.rollback()
            raise
        return

    first_portno = overflow_rows[0]["portno"]
    same_group_count = sum(1 for r in overflow_rows if r["portno"] == first_portno)

    try:
        execute(db_conn,
            "UPDATE sorting_rules SET status=4 WHERE innerport=?",
            (scanned_innerport,))
        execute(db_conn,
            "UPDATE sorting_rules SET innerport=? WHERE portno=? AND innerport=0 AND status=1",
            (scanned_innerport, first_portno))
        execute(db_conn,
            "UPDATE sort_ports SET init_num=?, fj_num=0, modified_at=GETDATE() WHERE portno=?",
            (same_group_count, scanned_innerport))
        db_conn.commit()
    except Exception:
        db_conn.rollback()
        raise


def auto_update_port(db_conn, freed_port: int) -> bool:
    """
    按钮触发：把指定已清空格口分配给最早的溢出包。
    替代原 Proc_AutoUpdatePort(@Port)。
    返回 True=重分配成功，False=无溢出（格口已清零）。
    """
    has_overflow = qval(db_conn,
        "SELECT COUNT(*) FROM sorting_rules "
        "WHERE innerport=0 AND status=1")
    port_is_free = qval(db_conn,
        "SELECT COUNT(*) FROM sort_ports WHERE portno=? AND init_num=fj_num AND init_num!=0",
        (freed_port,))

    try:
        if has_overflow and port_is_free:
            overflow_portno = qval(db_conn,
                "SELECT TOP 1 portno FROM sorting_rules "
                "WHERE innerport=0 AND status=1 ORDER BY queue_seq ASC")
            total = qval(db_conn,
                "SELECT COUNT(*) FROM sorting_rules WHERE portno=? AND innerport=0",
                (overflow_portno,))
            execute(db_conn,
                "UPDATE sorting_rules SET innerport=? WHERE portno=? AND innerport=0",
                (freed_port, overflow_portno))
            execute(db_conn,
                "UPDATE sort_ports SET remark=0, fj_num=0, init_num=? WHERE portno=?",
                (total, freed_port))
            db_conn.commit()
            return True
        else:
            execute(db_conn,
                "UPDATE sort_ports SET remark=0, fj_num=0, init_num=0 WHERE portno=?",
                (freed_port,))
            db_conn.commit()
            return False
    except Exception:
        db_conn.rollback()
        raise


def get_port_status(db_conn) -> list[dict]:
    """
    返回需要处理的格口列表（已满=1，超时告警=2）。
    替代原 Proc_PortStatus。
    """
    cursor = db_conn.cursor()
    cursor.execute("""
        SELECT portno, init_num, fj_num, remark,
               CASE
                 WHEN init_num != 0 AND init_num = fj_num AND remark = 0
                   THEN 1
                 WHEN DATEADD(MINUTE, 40, modified_at) < GETDATE()
                      AND init_num != 0 AND fj_num != 0
                      AND init_num != fj_num AND remark = 0
                   THEN 2
                 ELSE 0
               END AS port_status
        FROM sort_ports
        WHERE init_num != 0
    """)
    cols = [c[0] for c in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    return [r for r in rows if r["port_status"] > 0]


def _desired_light(port: dict) -> int:
    if port.get("is_enable", 1) == 0 or port.get("remark", 0) == 1:
        return LIGHT_RED
    if port.get("init_num", 0) == 0:
        return LIGHT_OFF
    if port.get("fj_num", 0) >= port.get("init_num", 0):
        return LIGHT_GREEN
    return LIGHT_YELLOW_FLASH


def sync_port_lights(db_conn, plc):
    ports = qall(db_conn, """
        SELECT sp.portno, sp.init_num, sp.fj_num, sp.remark, sp.is_enable,
               ISNULL(pl.light_val, 0) AS light_val
        FROM sort_ports sp
        LEFT JOIN port_lights pl ON pl.portno = sp.portno
        ORDER BY sp.portno
    """)
    for port in ports:
        desired = _desired_light(port)
        if port["light_val"] == desired:
            continue
        try:
            write_port_light(db_conn, plc, port["portno"], desired)
            logger.info(f"[light] port={port['portno']} value={desired} "
                        f"init={port['init_num']} fj={port['fj_num']} "
                        f"remark={port['remark']} enable={port['is_enable']}")
        except Exception as e:
            logger.warning(f"[light] write failed port={port['portno']}: {e}")


def port_monitor_loop(plc):
    """
    每 5s 检查格口超时告警，触发黄灯控制。
    替代原 C# 定时轮询。
    同步 C# HandleLightSignListen 灯控规则：
      有任务未完成 -> 黄闪；全部扫完 -> 绿灯；关闭/停用 -> 红灯；空闲 -> 灭灯。
    """
    while True:
        try:
            db = get_db_conn()
            sync_port_lights(db, plc)
            alerts = get_port_status(db)
            for a in alerts:
                if a["port_status"] == 2:   # 超时告警
                    logger.warning(f"[port_monitor] 格口 {a['portno']} 超时告警（"
                                   f"init={a['init_num']}, fj={a['fj_num']}）")
                    write_port_light(db, plc, a["portno"], LIGHT_YELLOW)
        except Exception as e:
            logger.warning(f"[port_monitor_loop] 异常: {e}")
        time.sleep(5)
