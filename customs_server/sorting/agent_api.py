"""
sorting/agent_api.py — 云端分拣接口路由实现

5 条路由供本地分拣机 Agent 调用（注册到 customs_server/server.py）：
  GET  /agent/rules          → 规则差量下发
  POST /agent/events         → 接收扫码事件（event_key 幂等去重）
  GET  /sorting/dashboard    → 看板页
  POST /sorting/rules        → 手工录入规则
  POST /sorting/batch/assign → 触发 allocate_ports
"""
import json
import logging
import sqlite3
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template_string

logger = logging.getLogger(__name__)

sorting_bp = Blueprint('sorting', __name__)

# ── 本地 SQLite（复用 customs_server 现有 sales_cache）──────────────────────────
# 分拣规则和扫码事件存入独立的表（不影响现有报关功能）

def _get_conn(db_path: str = None):
    """获取 SQLite 连接（WAL 模式，30s 超时）。"""
    import os
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), '..', '_sales_cache',
                               'sorting_cloud.sqlite3')
    conn = sqlite3.connect(db_path, timeout=30,
                           isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables():
    """初始化云端分拣表（首次调用时建表）。"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cloud_sorting_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ver         INTEGER NOT NULL DEFAULT 0,
            batchno     TEXT NOT NULL,
            barcode     TEXT NOT NULL,
            slot_seq    INTEGER DEFAULT 1,
            portno      INTEGER NOT NULL,
            innerport   INTEGER DEFAULT 0,
            customer    TEXT,
            goodsno     TEXT,
            goodsmodel  TEXT,
            floor       INTEGER DEFAULT 0,
            serialnum   INTEGER DEFAULT 0,
            label_data  TEXT,
            box_type    INTEGER DEFAULT 1,
            picktype    INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS IX_csr_barcode ON cloud_sorting_rules(barcode);
        CREATE INDEX IF NOT EXISTS IX_csr_ver ON cloud_sorting_rules(ver);

        CREATE TABLE IF NOT EXISTS cloud_scan_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key   TEXT UNIQUE,
            batchno     TEXT,
            barcode     TEXT NOT NULL,
            innerport   INTEGER NOT NULL,
            carno       INTEGER NOT NULL,
            syno        INTEGER NOT NULL,
            serialnum   INTEGER NOT NULL,
            length      INTEGER DEFAULT 0,
            width       INTEGER DEFAULT 0,
            height      INTEGER DEFAULT 0,
            weight      INTEGER DEFAULT 0,
            is_manual   INTEGER DEFAULT 0,
            scanned_at  TEXT,
            received_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS IX_cse_batchno ON cloud_scan_events(batchno);

        CREATE TABLE IF NOT EXISTS cloud_rule_version (
            id      INTEGER PRIMARY KEY CHECK(id=1),
            ver     INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO cloud_rule_version(id, ver) VALUES (1, 0);
    """)
    conn.close()


# ── 规则差量下发 ────────────────────────────────────────────────────────────────
@sorting_bp.get('/agent/rules')
def agent_get_rules():
    """
    GET /agent/rules?since_ver=N
    返回版本号大于 since_ver 的规则（差量下发）。
    本地 Agent 每 30s 轮询，仅拉取新版本。
    """
    since_ver = request.args.get('since_ver', type=int, default=0)
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM cloud_sorting_rules WHERE ver > ? ORDER BY ver, id",
        (since_ver,)
    ).fetchall()
    conn.close()
    return jsonify({
        "rules": [dict(r) for r in rows],
        "count": len(rows)
    })


# ── 接收扫码事件 ────────────────────────────────────────────────────────────────
@sorting_bp.post('/agent/events')
def agent_post_events():
    """
    POST /agent/events
    body: {"events": [...]}
    ⚠️ event_key 唯一约束去重（UNIQUE，重推时忽略重复）。
    """
    data = request.get_json() or {}
    events = data.get('events', [])
    if not events:
        return jsonify({"ok": True, "inserted": 0})

    conn = _get_conn()
    inserted = 0
    for e in events:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO cloud_scan_events
                    (event_key, batchno, barcode, innerport, carno, syno, serialnum,
                     length, width, height, weight, is_manual, scanned_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                e.get('event_key'), e.get('batchno'), e.get('barcode'),
                e.get('innerport'), e.get('carno'), e.get('syno'), e.get('serialnum'),
                e.get('length', 0), e.get('width', 0),
                e.get('height', 0), e.get('weight', 0),
                e.get('is_manual', 0), e.get('scanned_at')
            ))
            if conn.execute("SELECT changes()").fetchone()[0] > 0:
                inserted += 1
        except Exception as ex:
            logger.warning(f"[agent_events] 写入失败: {ex} event={e.get('event_key')}")
    conn.close()
    return jsonify({"ok": True, "inserted": inserted, "total": len(events)})


# ── 看板页 ────────────────────────────────────────────────────────────────────
@sorting_bp.get('/sorting/dashboard')
def sorting_dashboard():
    """简单看板：最近 200 条扫码事件。"""
    conn = _get_conn()
    events = conn.execute(
        "SELECT * FROM cloud_scan_events ORDER BY id DESC LIMIT 200"
    ).fetchall()
    conn.close()
    rows = [dict(r) for r in events]
    html = """
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <title>分拣看板</title>
    <style>body{font-family:Arial;font-size:13px;padding:16px}
    table{border-collapse:collapse;width:100%}
    th,td{border:1px solid #ccc;padding:4px 8px;text-align:left}
    th{background:#eee}</style></head><body>
    <h2>分拣扫码事件（最近 200 条）</h2>
    <table><tr>
    <th>ID</th><th>批次</th><th>条码</th><th>格口</th><th>小车</th>
    <th>手工</th><th>扫码时间</th>
    </tr>
    {% for r in rows %}
    <tr>
      <td>{{r.id}}</td><td>{{r.batchno}}</td><td>{{r.barcode}}</td>
      <td>{{r.innerport}}</td><td>{{r.carno}}</td>
      <td>{{'是' if r.is_manual else '否'}}</td><td>{{r.scanned_at}}</td>
    </tr>
    {% endfor %}
    </table></body></html>
    """
    return render_template_string(html, rows=rows)


# ── 手工录入规则 ────────────────────────────────────────────────────────────────
@sorting_bp.post('/sorting/rules')
def sorting_post_rules():
    """
    POST /sorting/rules
    body: {"batchno": str, "rules": [...]}
    手工将分拣规则写入云端（不经 allocate_ports 算法）。
    """
    data = request.get_json() or {}
    batchno = data.get('batchno', '')
    rules   = data.get('rules', [])
    if not batchno or not rules:
        return jsonify({"ok": False, "msg": "batchno 和 rules 不能为空"}), 400

    conn = _get_conn()
    ver_row = conn.execute("SELECT ver FROM cloud_rule_version WHERE id=1").fetchone()
    new_ver = (ver_row['ver'] if ver_row else 0) + 1

    for r in rules:
        r['ver'] = new_ver
        r['batchno'] = batchno
        conn.execute("""
            INSERT INTO cloud_sorting_rules
                (ver, batchno, barcode, slot_seq, portno, innerport,
                 customer, goodsno, goodsmodel, floor, serialnum, label_data, box_type, picktype)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            new_ver, batchno,
            r.get('barcode'), r.get('slot_seq', 1),
            r.get('portno', 0), r.get('innerport', 0),
            r.get('customer'), r.get('goodsno'), r.get('goodsmodel'),
            r.get('floor', 0), r.get('serialnum', 0),
            r.get('label_data'), r.get('box_type', 1), r.get('picktype', 0)
        ))
    conn.execute("UPDATE cloud_rule_version SET ver=? WHERE id=1", (new_ver,))
    conn.close()
    return jsonify({"ok": True, "ver": new_ver, "inserted": len(rules)})


# ── 触发分箱算法 ────────────────────────────────────────────────────────────────
@sorting_bp.post('/sorting/batch/assign')
def sorting_batch_assign():
    """
    POST /sorting/batch/assign
    body: {"batchno": str, "orders": [...], "box_configs": {1:vol1, 2:vol2, 3:vol3}}
    触发 allocate_ports 算法，生成 sorting_rules 并写入云端。
    """
    data        = request.get_json() or {}
    batchno     = data.get('batchno', '')
    orders      = data.get('orders', [])
    box_configs = data.get('box_configs', {})

    if not batchno or not orders:
        return jsonify({"ok": False, "msg": "batchno 和 orders 不能为空"}), 400

    # box_configs key 可能是字符串（JSON），转为 int
    box_configs = {int(k): v for k, v in box_configs.items()}

    from sorting.batch_planner import allocate_ports
    rules = allocate_ports(orders, box_configs)

    conn = _get_conn()
    ver_row = conn.execute("SELECT ver FROM cloud_rule_version WHERE id=1").fetchone()
    new_ver = (ver_row['ver'] if ver_row else 0) + 1

    for r in rules:
        conn.execute("""
            INSERT INTO cloud_sorting_rules
                (ver, batchno, barcode, slot_seq, portno, innerport,
                 customer, goodsno, goodsmodel, floor, serialnum, label_data, box_type, picktype)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            new_ver, batchno,
            r['barcode'], r['slot_seq'],
            r['portno'], r['innerport'],
            r.get('customer'), r.get('goodsno'), r.get('goodsmodel'),
            r['floor'], r['serialnum'],
            r['label_data'], r['box_type'], r.get('picktype', 0)
        ))
    conn.execute("UPDATE cloud_rule_version SET ver=? WHERE id=1", (new_ver,))
    conn.close()

    return jsonify({
        "ok": True,
        "batchno": batchno,
        "ver": new_ver,
        "rules_count": len(rules)
    })


# 初始化表（模块导入时执行）
try:
    _ensure_tables()
except Exception as _e:
    logger.warning(f"[sorting] 初始化表失败: {_e}")
