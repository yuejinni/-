"""
sync/rule_sync.py — 从云端拉取分拣规则

每 30s 轮询云端 GET /agent/rules?since_ver=N，按批次全量替换本地 sorting_rules。
替代原系统手工导入按钮（Proc_Importordergoodsinfo + Proc_Producttoport + Proc_Sendsorting）。
"""
import time
import logging
import requests

from core.db import qval, execute, get_db_conn

logger = logging.getLogger(__name__)


def sync_rules_from_cloud(db_conn, cloud_url: str, current_ver: int):
    """
    从云端拉取 since_ver 之后的规则差量，写入本地 sorting_rules。
    新批次到来时：先删除该批次旧规则 + 重置所有格口计数（对齐原系统全量替换语义）。
    """
    resp = requests.get(
        f"{cloud_url}/agent/rules",
        params={"since_ver": current_ver},
        timeout=5
    )
    resp.raise_for_status()
    data = resp.json()
    rules = data.get("rules", [])
    if not rules:
        return

    new_batchno = rules[0].get("batchno")
    try:
        if new_batchno:
            # 对齐原系统：新批次全量替换（TRUNCATE 语义）
            execute(db_conn,
                "DELETE FROM sorting_rules WHERE batchno=?", (new_batchno,))
            # 重置所有格口计数（对齐 Proc_Sendsorting 开头 UPDATE Wcs_port）
            execute(db_conn,
                "UPDATE sort_ports SET init_num=0, fj_num=0, remark=0")

        for r in rules:
            # 溢出格口 innerport=0，等重分配后才有值
            innerport = r["portno"] if r["portno"] <= 102 else 0
            execute(db_conn, """
                INSERT INTO sorting_rules
                    (rule_ver, batchno, barcode, slot_seq, portno, innerport,
                     customer, goodsno, goodsmodel, floor, serialnum,
                     label_data, box_type, status, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,GETDATE())
            """, (
                r["ver"], r["batchno"], r["barcode"], r.get("slot_seq", 1),
                r["portno"], innerport,
                r.get("customer"), r.get("goodsno"), r.get("goodsmodel"),
                r.get("floor", 0), r["serialnum"],
                r.get("label_data"),    # 云端 allocate_ports 返回（格式：orderno-box_num）
                r.get("box_type", 1)
            ))
            if innerport != 0:
                execute(db_conn,
                    "UPDATE sort_ports SET init_num=init_num+1 WHERE portno=?",
                    (innerport,))

        # 初始化 pick_progress（每个 batchno+barcode 聚合为一行）
        db_conn.cursor().execute("""
            MERGE pick_progress AS target
            USING (
                SELECT
                    batchno, barcode,
                    MAX(floor)      AS floor,
                    MAX(goodsno)    AS goodsnum,
                    MAX(goodsmodel) AS model,
                    COUNT(*)        AS num,
                    0               AS picktype,
                    MAX(portno)     AS port
                FROM sorting_rules
                WHERE batchno=?
                GROUP BY batchno, barcode
            ) AS src ON target.batchno=src.batchno AND target.barcode=src.barcode
            WHEN MATCHED THEN
                UPDATE SET num=src.num, anum=0, floor=src.floor,
                           goodsnum=src.goodsnum, model=src.model,
                           picktype=src.picktype, port=src.port,
                           updated_at=GETDATE()
            WHEN NOT MATCHED THEN
                INSERT (batchno, barcode, floor, goodsnum, model, num, anum, picktype, port)
                VALUES (src.batchno, src.barcode, src.floor, src.goodsnum, src.model,
                        src.num, 0, src.picktype, src.port);
        """, (new_batchno,))

        execute(db_conn,
            "UPDATE sys_config SET value=? WHERE [key]='rule_version'",
            (str(rules[-1]["ver"]),))
        execute(db_conn,
            "UPDATE sys_config SET value=? WHERE [key]='active_batchno'",
            (new_batchno,))
        db_conn.commit()
        logger.info(f"[rule_sync] 同步完成，批次={new_batchno}，规则数={len(rules)}")
    except Exception:
        db_conn.rollback()
        raise


def rule_sync_loop():
    """
    每 30s 从云端拉取规则差量（替代原 C# 手工导入按钮）。
    各自创建独立 db_conn，线程内复用。
    """
    while True:
        try:
            db = get_db_conn()
            cloud_url = qval(db,
                "SELECT value FROM sys_config WHERE [key]='cloud_url'") or ''
            current_ver = int(qval(db,
                "SELECT value FROM sys_config WHERE [key]='rule_version'") or 0)
            if cloud_url:
                sync_rules_from_cloud(db, cloud_url, current_ver)
        except Exception as e:
            logger.warning(f"[rule_sync_loop] 同步失败（30s后重试）: {e}")
        time.sleep(30)
