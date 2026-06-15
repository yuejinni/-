"""
sync/event_push.py — 扫码事件推送到云端

每 3s 批量推送 scan_events.pushed=0 的记录到云端 POST /agent/events。
网络中断时静默等待，恢复后自动补发（可靠交付，幂等键去重）。
替代原 Redis YJLG 队列。
"""
import time
import logging
import datetime
import requests

from core.db import qval, qall, execute, get_db_conn

logger = logging.getLogger(__name__)


def _serialize(row: dict) -> dict:
    """把 dict 里的 datetime 转成 ISO 字符串，确保 JSON 可序列化。"""
    return {
        k: v.isoformat() if isinstance(v, (datetime.datetime, datetime.date)) else v
        for k, v in row.items()
    }


def event_push_loop():
    """
    每 3s 推送一批未发事件到云端。
    各自创建独立 db_conn，线程内复用。
    """
    while True:
        try:
            db = get_db_conn()
            cloud_url = qval(db,
                "SELECT value FROM sys_config WHERE [key]='cloud_url'") or ''
            if not cloud_url:
                time.sleep(3)
                continue

            rows = qall(db,
                "SELECT TOP 50 id, batchno, barcode, innerport, carno, syno, serialnum, "
                "       length, width, height, weight, is_manual, scanned_at, event_key "
                "FROM scan_events WHERE pushed=0 ORDER BY id")
            if rows:
                resp = requests.post(
                    f"{cloud_url}/agent/events",
                    json={"events": [_serialize(r) for r in rows]},
                    timeout=5
                )
                if resp.status_code == 200:
                    ids = [r["id"] for r in rows]
                    placeholders = ",".join(["?"] * len(ids))
                    execute(db,
                        f"UPDATE scan_events SET pushed=1 WHERE id IN ({placeholders})",
                        ids)
                    db.commit()
                    logger.debug(f"[event_push] 推送 {len(ids)} 条事件成功")
                else:
                    logger.warning(f"[event_push] 推送失败 HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"[event_push_loop] 异常（3s后重试）: {e}")
        time.sleep(3)
