"""
api/web_api.py — Flask Web API（:5009）

替代原 C# EMSAPI，12 条路由。
⚠️ 每个请求独立获取 DB 连接（Flask 多线程模式下不共享 connection）。
"""
import os
import logging
import json
from flask import Flask, request, jsonify, render_template

from core.db import qval, qall, execute, get_db_conn
from core.scan_handler import handle_pda_scan, handle_manual_scan
from core.port_manager import auto_update_port, get_port_status

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates')


_LOC_CHARS = set('ABCDabcdEFGefgHIJhijKLMNklmn')

def _location_sort_key(model: str) -> str:
    """从 model 字段提取库位码用于排序（首个以楼层字母开头的段，大写统一比较）。"""
    for seg in (model or '').split():
        if seg and seg[0] in _LOC_CHARS:
            return seg.upper()
    return model or ''


# ── 1. PDA 拣货列表 ────────────────────────────────────────────────────────────
@app.get('/api/pick')
def get_pick_list():
    """
    PDA 拣货列表（替代 GET /GetPickInfo?floor=N&type=N）
    type 参数对应 Wcs_goods.column3：0=自动分拣（默认），1=手工分拣
    """
    floor    = request.args.get('floor', type=int, default=0)
    picktype = request.args.get('type',  type=int, default=0)
    db = get_db_conn()
    active = qval(db, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    rows = qall(db,
        "SELECT barcode, port AS portno, goodsnum, model, unit, quantity, num, anum "
        "FROM pick_progress "
        "WHERE batchno=? AND num>anum AND floor=? AND picktype=? ",
        (active, floor, picktype))
    rows = sorted(rows, key=lambda r: _location_sort_key(r.get('model', '')))
    return jsonify(rows)


# ── 2. PDA 扫码确认拣货 ────────────────────────────────────────────────────────
@app.get('/api/pda/scan')
def pda_scan():
    """PDA 扫码确认拣货（替代 GET /GetScancodeInfo）"""
    barcode = request.args.get('barcode', '')
    floor   = request.args.get('floor', type=int, default=0)
    if not barcode:
        return jsonify({"ok": False, "msg": "barcode 不能为空"}), 400
    result = handle_pda_scan(get_db_conn(), barcode, floor)
    return jsonify(result)


# ── 3. 手工分拣 ────────────────────────────────────────────────────────────────
@app.post('/api/manual-sort')
def manual_sort():
    """
    手工分拣扫码（替代 Proc_GetManualportinfo）
    body: {"barcode": str, "carno": int, "length": int, "width": int,
           "height": int, "weight": int, "dwsno": int}
    ⚠️ 字段名对齐前端 manual.html：success/port（非 ok/portno）
    """
    body    = request.get_json() or {}
    barcode = body.get('barcode', '')
    if not barcode:
        return jsonify({"success": False, "port": 0, "autoRunning": False,
                        "msg": "barcode 不能为空"}), 400
    carno  = body.get('carno', 0)
    length = body.get('length', 0)
    width  = body.get('width', 0)
    height = body.get('height', 0)
    weight = body.get('weight', 0)
    dwsno  = body.get('dwsno', 0)

    db = get_db_conn()
    auto_running = bool(int(qval(db,
        "SELECT value FROM sys_config WHERE [key]='auto_running'") or 0))
    portno = handle_manual_scan(db, carno, barcode, length, width, height, weight, dwsno)
    if portno == 0:
        return jsonify({"success": False, "port": 0, "autoRunning": auto_running,
                        "msg": "条码不存在或非手工分拣商品"})
    return jsonify({"success": True, "port": portno, "autoRunning": auto_running,
                    "msg": f"请放入格口 {portno}"})


# ── 4. 看板数据 ────────────────────────────────────────────────────────────────
@app.get('/api/status')
def get_status():
    """看板数据：格口占用 + 小车状态 + 告警格口 + 统计计数"""
    db = get_db_conn()
    ports  = qall(db, "SELECT * FROM sort_ports WHERE init_num!=0")
    cars   = qall(db, "SELECT * FROM car_status ORDER BY carno")
    alerts = get_port_status(db)
    stats  = {r['key']: r['value'] for r in qall(db,
        "SELECT [key], value FROM sys_config WHERE [key] LIKE 'stat_%'")}
    orders = _get_order_summaries(db)
    return jsonify({
        "ports": ports,
        "cars": cars,
        "alerts": alerts,
        "stats": stats,
        "orders": orders,
    })


def _order_no_expr() -> str:
    return "COALESCE(NULLIF(label_data, ''), batchno + '-' + barcode)"


def _get_order_summaries(db) -> list[dict]:
    active = qval(db,
        "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    if not active:
        return []

    order_expr = _order_no_expr()
    rows = qall(db, f"""
        SELECT
            {order_expr} AS orderno,
            COALESCE(MAX(customer), '') AS customer,
            MAX(floor) AS floor,
            COUNT(*) AS total_qty,
            SUM(CASE WHEN status >= 3 THEN 1 ELSE 0 END) AS scanned_qty,
            SUM(CASE WHEN innerport = 0 THEN 1 ELSE 0 END) AS overflow_qty
        FROM sorting_rules
        WHERE batchno=?
        GROUP BY {order_expr}
        ORDER BY orderno
    """, (active,))

    goods_rows = qall(db, f"""
        SELECT
            {order_expr} AS orderno,
            COALESCE(goodsno, barcode) AS goodsno,
            COALESCE(goodsmodel, '') AS spec,
            MAX(innerport) AS innerport,
            COUNT(*) AS total,
            SUM(CASE WHEN status >= 3 THEN 1 ELSE 0 END) AS scanned
        FROM sorting_rules
        WHERE batchno=?
        GROUP BY {order_expr}, COALESCE(goodsno, barcode), COALESCE(goodsmodel, '')
        ORDER BY orderno, goodsno
    """, (active,))

    ports_rows = qall(db, f"""
        SELECT DISTINCT {order_expr} AS orderno, innerport
        FROM sorting_rules
        WHERE batchno=? AND innerport != 0
        ORDER BY orderno, innerport
    """, (active,))

    goods_by_order: dict[str, list[dict]] = {}
    for row in goods_rows:
        goods_by_order.setdefault(row["orderno"], []).append({
            "goodsno": row["goodsno"],
            "spec": row["spec"],
            "innerport": row["innerport"],
            "total": row["total"] or 0,
            "scanned": row["scanned"] or 0,
        })

    ports_by_order: dict[str, list[int]] = {}
    for row in ports_rows:
        ports_by_order.setdefault(row["orderno"], []).append(row["innerport"])

    orders = []
    for row in rows:
        total = row["total_qty"] or 0
        scanned = row["scanned_qty"] or 0
        missing = max(total - scanned, 0)
        if scanned >= total and total:
            status = "done"
        elif scanned > 0:
            status = "partial"
        elif row["overflow_qty"]:
            status = "missing"
        else:
            status = "partial"
        orders.append({
            "orderno": row["orderno"],
            "customer": row["customer"] or "--",
            "floor": row["floor"],
            "ports": ports_by_order.get(row["orderno"], []),
            "total_qty": total,
            "scanned_qty": scanned,
            "missing_qty": missing,
            "overflow_qty": row["overflow_qty"] or 0,
            "status": status,
            "goods": goods_by_order.get(row["orderno"], []),
        })
    return orders


@app.get('/api/scan_log')
def get_scan_log():
    """Recent conveyor scan events for the dashboard side panel."""
    db = get_db_conn()
    events = qall(db, """
        SELECT TOP 50 id, batchno, barcode, innerport, carno, syno, serialnum,
               is_manual, pushed, scanned_at
        FROM scan_events
        ORDER BY id DESC
    """)
    for event in events:
        scanned_at = event.get("scanned_at")
        if hasattr(scanned_at, "isoformat"):
            event["scanned_at"] = scanned_at.isoformat(sep=" ")
            event["time"] = scanned_at.strftime("%H:%M:%S")
        else:
            event["time"] = ""
        event["ok"] = event.get("innerport") not in (0, 51)
    return jsonify({"events": events})


@app.get('/api/system')
def get_system_status():
    """System and sync status displayed by the dashboard modal."""
    db = get_db_conn()
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config.json')
    try:
        with open(config_path, encoding='utf-8') as f:
            config = json.load(f)
    except Exception:
        config = {}

    active_batchno = qval(
        db, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    cloud_url = qval(
        db, "SELECT value FROM sys_config WHERE [key]='cloud_url'") or config.get("cloud_url", "")
    rule_version = qval(
        db, "SELECT value FROM sys_config WHERE [key]='rule_version'") or '0'
    last_sync = qval(db, "SELECT MAX(synced_at) FROM sorting_rules")
    if hasattr(last_sync, "isoformat"):
        last_sync = last_sync.isoformat(sep=" ")

    push_backlog = qval(
        db, "SELECT COUNT(*) FROM scan_events WHERE pushed=0") or 0
    rules_total = qval(
        db, "SELECT COUNT(*) FROM sorting_rules WHERE batchno=?",
        (active_batchno,)) if active_batchno else 0

    return jsonify({
        "plc_ip": config.get("plc_ip", ""),
        "plc_connected": True,
        "cloud_url": cloud_url,
        "cloud_ok": bool(cloud_url),
        "active_batchno": active_batchno,
        "rule_version": rule_version,
        "last_sync": last_sync,
        "rules_total": rules_total or 0,
        "push_backlog": push_backlog,
        "tcp_clients": 0,
        "printer_name": config.get("printer_name", ""),
        "printer_ok": bool(config.get("printer_name")),
    })


@app.post('/api/sync/force')
def force_sync():
    """Manually trigger one cloud rule sync pass."""
    db = get_db_conn()
    cloud_url = qval(db,
        "SELECT value FROM sys_config WHERE [key]='cloud_url'") or ''
    current_ver = int(qval(db,
        "SELECT value FROM sys_config WHERE [key]='rule_version'") or 0)
    if not cloud_url:
        return jsonify({"ok": False, "msg": "cloud_url is empty"}), 400

    from sync.rule_sync import sync_rules_from_cloud
    sync_rules_from_cloud(db, cloud_url, current_ver)
    new_ver = qval(db,
        "SELECT value FROM sys_config WHERE [key]='rule_version'") or current_ver
    return jsonify({"ok": True, "rule_version": new_ver})


# ── 5. 锁格/解锁 ───────────────────────────────────────────────────────────────
@app.post('/api/port/<int:portno>/remark')
def set_port_remark(portno):
    """锁格/解锁（替代 Proc_UpdatePortRemark）"""
    remark = (request.get_json() or {}).get('remark', 0)
    db = get_db_conn()
    try:
        execute(db, "UPDATE sort_ports SET remark=? WHERE portno=?", (remark, portno))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"ok": True})


# ── 6. 格口重分配 ──────────────────────────────────────────────────────────────
@app.post('/api/port/<int:portno>/auto-update')
def auto_update(portno):
    """按钮触发格口重分配（替代 Proc_AutoUpdatePort）"""
    result = auto_update_port(get_db_conn(), portno)
    return jsonify({"ok": True, "reassigned": result})


# ── 7. 一键上机 ────────────────────────────────────────────────────────────────
@app.post('/api/batch/onekey')
def onekey():
    """
    一键上机（替代 Proc_OneKey）
    两步操作：① sorting_rules status 1→2  ② pick_progress anum=num
    ⚠️ 必须先验证 active_batchno 非空，否则 WHERE batchno='' 静默成功但实际无效。
    """
    active = qval(get_db_conn(),
                  "SELECT value FROM sys_config WHERE [key]='active_batchno'")
    if not active:
        return jsonify({"ok": False,
                        "msg": "未设置当前活跃批次（active_batchno）"}), 400
    db = get_db_conn()
    try:
        execute(db,
            "UPDATE sorting_rules SET status=2 WHERE batchno=? AND status=1",
            (active,))
        execute(db,
            "UPDATE pick_progress SET anum=num, updated_at=GETDATE() WHERE batchno=?",
            (active,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"ok": True})


# ── 8. 批次完成归档 ────────────────────────────────────────────────────────────
@app.post('/api/batch/<batchno>/truncate')
def truncate_batch(batchno):
    """
    完成当前批次并归档（替代 Proc_Truncatesorting）
    sorting_rules status→5 + 删 pick_progress + 清 sort_ports
    """
    db = get_db_conn()
    try:
        execute(db,
            "UPDATE sorting_rules SET status=5 WHERE batchno=? AND status IN (1,2,3)",
            (batchno,))
        execute(db, "DELETE FROM pick_progress WHERE batchno=?", (batchno,))
        execute(db,
            "UPDATE sort_ports SET init_num=0, fj_num=0, remark=0, modified_at=GETDATE()")
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"ok": True, "msg": f"批次 {batchno} 已完成归档，格口已清零"})


# ── 9. 取消格口分配（存根） ────────────────────────────────────────────────────
@app.post('/api/batch/<batchno>/cancel')
def cancel_batch(batchno):
    """
    取消格口分配（替代 Proc_Canceltoport）
    ⚠️ TODO：Proc_Canceltoport 在 script.sql 中未找到实现，待确认后补全
    """
    return jsonify({"ok": False,
                    "msg": "cancel 接口待实现（Proc_Canceltoport 逻辑待确认）"}), 501


# ── 10. 批次硬删除 ─────────────────────────────────────────────────────────────
@app.delete('/api/batch/<batchno>/hard')
def hard_delete_batch(batchno):
    """
    硬删除批次（替代 Proc_Deletesorting）
    ⚠️ 不可逆，彻底清除该批次所有数据
    """
    db = get_db_conn()
    try:
        execute(db, "DELETE FROM sorting_rules WHERE batchno=?", (batchno,))
        execute(db, "DELETE FROM pick_progress WHERE batchno=?", (batchno,))
        execute(db, "DELETE FROM scan_events WHERE batchno=?", (batchno,))
        execute(db, "DELETE FROM sort_batches WHERE batchno=?", (batchno,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"ok": True, "msg": f"批次 {batchno} 已彻底删除"})


# ── 11. 批次回滚 ───────────────────────────────────────────────────────────────
@app.post('/api/batch/<batchno>/rollback')
def rollback_batch(batchno):
    """
    按批次回滚（替代 Proc_Rollbacksorting）
    ① 删除 status>=2 的规则行（保留 status=1 待拣）
    ② pick_progress anum 清零
    ③ sort_ports 格口计数按本批次重置
    scan_events 保留（审计用）
    """
    db = get_db_conn()
    try:
        execute(db,
            "DELETE FROM sorting_rules WHERE batchno=? AND status>=2",
            (batchno,))
        execute(db,
            "UPDATE pick_progress SET anum=0, updated_at=GETDATE() WHERE batchno=?",
            (batchno,))
        # 只清本批次相关格口（⚠️ 加 WHERE，避免误清其他批次数据）
        execute(db, """
            UPDATE sort_ports SET fj_num=0, modified_at=GETDATE()
            WHERE portno IN (
                SELECT DISTINCT innerport FROM sorting_rules
                WHERE batchno=? AND innerport!=0
            )
        """, (batchno,))
        # 重新统计 init_num（按回滚后剩余 status=1 的行数）
        execute(db, """
            UPDATE sp SET sp.init_num = cnt.c
            FROM sort_ports sp
            JOIN (
                SELECT innerport, COUNT(*) AS c
                FROM sorting_rules WHERE batchno=? AND status=1 AND innerport!=0
                GROUP BY innerport
            ) cnt ON sp.portno = cnt.innerport
        """, (batchno,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"ok": True,
                    "msg": f"批次 {batchno} 回滚完成（已上机记录已删，待拣记录保留）"})


# ── 12. 管理界面首页 ───────────────────────────────────────────────────────────
@app.get('/')
def dashboard():
    return render_template('dashboard.html')


# ── 13. PDA 拣货 H5 页面 ───────────────────────────────────────────────────────
@app.get('/pda')
def pda():
    return render_template('pda.html')


def run_flask_app(port: int = 5010):
    from waitress import serve
    logger.info(f"[Flask] 启动 waitress，监听 :{port}")
    serve(app, host='0.0.0.0', port=port)
