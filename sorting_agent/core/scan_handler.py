"""
core/scan_handler.py — 扫码处理核心

替代原 SQL Server 存储过程：
  Proc_Getportinfo    → handle_scan
  Proc_GetManualportinfo → handle_manual_scan
  Proc_Scanbarcode    → handle_pda_scan
"""
import struct
import logging
from datetime import datetime

from core.db import qone, qval, qall, execute
from plc.plc_client import _plc_lock, write_start_plc, write_port_light, LIGHT_GREEN
from core.port_manager import try_reassign_overflow

logger = logging.getLogger(__name__)

# ── 楼层推算（对应原 Proc_Sendsorting 中的 IF/ELSE 逻辑）────────────────────
# goodsmodel 示例："RED E12" → split → 第二段 "E12" → 首字母 'E' → floor=2
# 映射来自 SQL 原文（4层）：
#   A/B/C/D → 1楼    E/F/G → 2楼
#   H/I/J   → 3楼    K/L/M/N → 4楼
_FLOOR_MAP: dict = {}
for _c in 'ABCDabcd': _FLOOR_MAP[_c] = 1
for _c in 'EFGefg':   _FLOOR_MAP[_c] = 2
for _c in 'HIJhij':   _FLOOR_MAP[_c] = 3
for _c in 'KLMNklmn': _FLOOR_MAP[_c] = 4


def floor_from_goodsmodel(goodsmodel: str) -> int:
    """从 goodsmodel 第二段首字母推算楼层，无第二段返回 0。"""
    segs = (goodsmodel or '').split()
    if len(segs) < 2:
        return 0
    first_char = segs[1][0]
    return _FLOOR_MAP.get(first_char, 0)


# ── syno 生成（毫秒精度，避免同秒两次扫码碰撞）────────────────────────────────
# 原系统：'1'+HH+MM+SS+'1'（秒级，同秒碰撞 UNIQUE 冲突）
# 新方案：'1'+HH+MM+SS+ms3位（12位，远小于 UDInt 上限 4294967295）
def gen_syno() -> int:
    now = datetime.now()
    return int(f"1{now.hour:02d}{now.minute:02d}{now.second:02d}{now.microsecond // 1000:03d}")


def _write_plc(plc, carno: int, port: int, serialnum: int, syno: int = 0):
    """写 PLC DB200（10字节大端序）。使用 plc_client.write_start_plc。"""
    write_start_plc(plc, carno, port, serialnum, syno)


def _update_stats(db_conn, ok: bool):
    """更新扫码统计：total_num+1，今日正常/异常计数+1（对应原 C# 内存计数器）。"""
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        db_conn.cursor().execute("""
            UPDATE sys_config SET value='0'
            WHERE [key] IN ('stat_day_num','stat_day_ng_num')
              AND (SELECT value FROM sys_config WHERE [key]='stat_day_date') <> ?
        """, (today,))
        db_conn.cursor().execute(
            "UPDATE sys_config SET value=? WHERE [key]='stat_day_date'", (today,))
        db_conn.cursor().execute(
            "UPDATE sys_config SET value=CAST(CAST(value AS INT)+1 AS NVARCHAR) "
            "WHERE [key]='stat_total_num'")
        key = 'stat_day_num' if ok else 'stat_day_ng_num'
        db_conn.cursor().execute(
            "UPDATE sys_config SET value=CAST(CAST(value AS INT)+1 AS NVARCHAR) "
            "WHERE [key]=?", (key,))
        db_conn.commit()
    except Exception as e:
        db_conn.rollback()
        logger.warning(f"[_update_stats] 统计更新失败: {e}")


def _sync_completed_port_light(db_conn, plc, portno: int):
    port = qone(db_conn,
        "SELECT init_num, fj_num, remark FROM sort_ports WHERE portno=?",
        (portno,))
    if not port:
        return
    if port["remark"] == 0 and port["init_num"] != 0 and port["fj_num"] >= port["init_num"]:
        write_port_light(db_conn, plc, portno, LIGHT_GREEN)
        logger.info(f"[light] port={portno} green complete "
                    f"init={port['init_num']} fj={port['fj_num']}")


def handle_scan(db_conn, plc, carno: int, barcode: str) -> bool:
    """
    传送带扫码处理（替代 Proc_Getportinfo）。
    条码命中 → 写 PLC DB200 → 更新 sorting_rules + sort_ports + scan_events。
    未命中或溢出 → 写错误格口 51。
    ⚠️ status=2（可上机）才命中；规则同步后需经 PDA 扫码或一键上机才能置 status=2。
    """
    active_batch = qval(db_conn,
        "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''

    cursor = db_conn.cursor()
    cursor.execute(
        "SELECT TOP 1 id, innerport, serialnum FROM sorting_rules "
        "WHERE barcode=? AND batchno=? AND status=2 ORDER BY slot_seq",
        (barcode, active_batch)
    )
    row = cursor.fetchone()

    if not row:
        # 未知条码 → 写错误格口 51，计异常
        err_syno = gen_syno()
        _write_plc(plc, carno, port=51, serialnum=0, syno=err_syno)
        _update_stats(db_conn, ok=False)
        return False

    rule_id, innerport, serialnum = row

    # ⚠️ 溢出包（innerport=0）：格口未分配，当作异常件处理
    if innerport == 0:
        err_syno = gen_syno()
        _write_plc(plc, carno, port=51, serialnum=0, syno=err_syno)
        _update_stats(db_conn, ok=False)
        return False

    syno = gen_syno()

    # 先写 PLC，再写 DB（保证硬件优先，对齐原系统行为）
    _write_plc(plc, carno, port=innerport, serialnum=serialnum, syno=syno)

    try:
        execute(db_conn, "UPDATE sorting_rules SET status=3 WHERE id=?", (rule_id,))
        execute(db_conn,
            "UPDATE sort_ports SET fj_num=fj_num+1, modified_at=GETDATE() WHERE portno=?",
            (innerport,))
        execute(db_conn,
            "INSERT INTO scan_events "
            "(batchno, barcode, innerport, carno, syno, serialnum, event_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (active_batch, barcode, innerport, carno, syno, serialnum,
             f"{barcode}_{syno}"))
        db_conn.commit()
    except Exception as e:
        db_conn.rollback()
        logger.error(f"[handle_scan] DB 写入失败: {e}")
        # ⚠️ PLC 已写，DB 失败仍计异常统计
        _update_stats(db_conn, ok=False)
        raise

    _update_stats(db_conn, ok=True)
    try:
        _sync_completed_port_light(db_conn, plc, innerport)
    except Exception as e:
        logger.warning(f"[light] write green failed port={innerport}: {e}")
    try_reassign_overflow(db_conn, innerport)
    return True


def handle_manual_scan(db_conn, carno: int, barcode: str,
                       length: int, width: int, height: int, weight: int,
                       dwsno: int = 0) -> int:
    """
    手工分拣扫码（替代 Proc_GetManualportinfo）。
    不写 PLC，直接写 scan_events（packagetime=scanned_at+1min）。
    只处理 picktype=1（column3=1）的商品。
    返回格口号，0 表示未命中。
    ⚠️ 使用毫秒精度 gen_syno()，避免同秒两次手工扫同条码 event_key 碰撞。
    """
    active_batch = qval(db_conn,
        "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    row = qone(db_conn,
        "SELECT id, innerport, serialnum FROM sorting_rules "
        "WHERE barcode=? AND batchno=? AND status=2",
        (barcode, active_batch))
    if not row:
        return 0

    rule_id   = row["id"]
    innerport = row["innerport"]
    serialnum = row["serialnum"]
    syno      = gen_syno()

    try:
        execute(db_conn, "UPDATE sorting_rules SET status=3 WHERE id=?", (rule_id,))
        execute(db_conn,
            "UPDATE sort_ports SET fj_num=fj_num+1, modified_at=GETDATE() WHERE portno=?",
            (innerport,))
        execute(db_conn,
            "INSERT INTO scan_events "
            "(batchno, barcode, innerport, carno, syno, serialnum, "
            " length, width, height, weight, is_manual, packagetime, dwsno, event_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, DATEADD(MINUTE,1,GETDATE()), ?, ?)",
            (active_batch, barcode, innerport, carno, syno, serialnum,
             length, width, height, weight, dwsno, f"{barcode}_{syno}"))
        db_conn.commit()
    except Exception as e:
        db_conn.rollback()
        logger.error(f"[handle_manual_scan] 失败: {e}")
        raise
    return innerport


def handle_pda_scan(db_conn, barcode: str, floor: int) -> dict:
    """
    PDA 扫码确认拣货（替代 Proc_Scanbarcode）。
    检查 pick_progress.anum < num → anum++ → sorting_rules 最小 slot_seq status=1→2。
    ⚠️ UPDLOCK+HOLDLOCK：持锁到事务提交，防两台 PDA 同时读到 anum<num 后双重推进。
    返回: {"ok": bool, "msg": str}
    """
    active_batch = qval(db_conn,
        "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''

    # 查拣货进度（UPDLOCK+HOLDLOCK 持锁到事务提交）
    progress = qone(db_conn,
        "SELECT id, num, anum FROM pick_progress WITH (UPDLOCK, HOLDLOCK) "
        "WHERE batchno=? AND barcode=? AND floor=?",
        (active_batch, barcode, floor))

    if not progress:
        other = qval(db_conn,
            "SELECT TOP 1 floor FROM pick_progress WHERE batchno=? AND barcode=?",
            (active_batch, barcode))
        if other is not None:
            return {"ok": False, "msg": f"该商品在 {other} 楼，当前扫码楼层 {floor} 不匹配"}
        return {"ok": False, "msg": "条码不存在或不属于当前批次"}

    if progress["anum"] >= progress["num"]:
        return {"ok": False, "msg": f"该商品已全部出库（{progress['num']} 件）"}

    # 找 status=1 且格口号最小的那一件（对齐原系统 ORDER BY port）
    rule_row = qone(db_conn,
        "SELECT TOP 1 id FROM sorting_rules WITH (UPDLOCK, HOLDLOCK) "
        "WHERE batchno=? AND barcode=? AND status=1 ORDER BY portno",
        (active_batch, barcode))

    if not rule_row:
        return {"ok": False, "msg": "该商品无待拣库存（可能已一键上机或状态异常）"}

    try:
        execute(db_conn, "UPDATE sorting_rules SET status=2 WHERE id=?", (rule_row["id"],))
        execute(db_conn,
            "UPDATE pick_progress SET anum=anum+1, updated_at=GETDATE() WHERE id=?",
            (progress["id"],))
        db_conn.commit()
    except Exception as e:
        db_conn.rollback()
        raise

    remaining = progress["num"] - progress["anum"] - 1
    return {"ok": True, "msg": f"扫码成功，剩余 {remaining} 件待拣"}
