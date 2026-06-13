"""
api/web_api.py — Flask Web API（:5009）

替代原 C# EMSAPI，12 条路由。
⚠️ 每个请求独立获取 DB 连接（Flask 多线程模式下不共享 connection）。
"""
import os
import logging
from flask import Flask, request, jsonify, render_template

from core.db import qval, qall, execute, get_db_conn
from core.scan_handler import handle_pda_scan, handle_manual_scan
from core.port_manager import auto_update_port, get_port_status

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates')


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
        "WHERE batchno=? AND num>anum AND floor=? AND picktype=? "
        "ORDER BY model ASC",
        (active, floor, picktype))
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
    return jsonify({"ports": ports, "cars": cars, "alerts": alerts, "stats": stats})


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
