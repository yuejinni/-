"""
api/web_api.py — Flask Web API（:5009）

替代原 C# EMSAPI，12 条路由。
⚠️ 每个请求独立获取 DB 连接（Flask 多线程模式下不共享 connection）。
"""
import math
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
    type=0 上机分拣：只返回已上机（innerport>0）的货品，附带 wave_num。
    type=1 手工分拣：返回所有待拣货品（无 sorting_rules 依赖），附带 portno。
    type=2 单票分拣：按 orderno 过滤，不限楼层，附带 portno。
    """
    floor    = request.args.get('floor', type=int, default=0)
    picktype = request.args.get('type',  type=int, default=0)
    db = get_db_conn()
    active = qval(db, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''

    if picktype == 0:
        # 上机分拣：必须有至少一条已上机规则
        rows = qall(db, """
            SELECT pp.barcode, pp.port AS portno, pp.goodsnum, pp.model,
                   pp.unit, pp.quantity, pp.num, pp.anum,
                   (SELECT MIN(sr.queue_seq)
                    FROM sorting_rules sr
                    WHERE sr.batchno = pp.batchno
                      AND sr.barcode = pp.barcode
                      AND sr.innerport > 0
                   ) AS min_queue_seq
            FROM pick_progress pp
            WHERE pp.batchno = ? AND pp.num > pp.anum
              AND pp.floor = ? AND pp.picktype = 0
              AND EXISTS (
                  SELECT 1 FROM sorting_rules sr
                  WHERE sr.batchno = pp.batchno
                    AND sr.barcode = pp.barcode
                    AND sr.innerport > 0
              )
        """, (active, floor))
        result = []
        for r in rows:
            d = dict(r)
            min_qs = d.get('min_queue_seq') or 1
            d['wave_num'] = math.ceil(min_qs / 100) if min_qs > 0 else 1
            result.append(d)
    elif picktype == 2:
        # 单票分拣：按 orderno（label_data 前缀）过滤，不限楼层
        orderno = request.args.get('orderno', '').strip()
        if not orderno or not active:
            return jsonify([])
        pattern = orderno + '-%'
        barcode_rows = qall(db, """
            SELECT DISTINCT barcode FROM sorting_rules
            WHERE batchno=? AND (label_data LIKE ? OR label_data=?)
        """, (active, pattern, orderno))
        barcode_list = [r['barcode'] for r in barcode_rows]
        if not barcode_list:
            return jsonify([])
        placeholders = ','.join('?' * len(barcode_list))
        rows = qall(db, f"""
            SELECT pp.barcode, pp.port AS portno, pp.goodsnum, pp.model,
                   pp.unit, pp.quantity, pp.num, pp.anum,
                   0 AS min_queue_seq
            FROM pick_progress pp
            WHERE pp.batchno=? AND pp.num > pp.anum
              AND pp.barcode IN ({placeholders})
        """, tuple([active] + barcode_list))
        result = [dict(r) for r in rows]
    else:
        # 手工分拣：直接按 pick_progress 返回，不过滤 sorting_rules
        rows = qall(db, """
            SELECT pp.barcode, pp.port AS portno, pp.goodsnum, pp.model,
                   pp.unit, pp.quantity, pp.num, pp.anum,
                   0 AS min_queue_seq
            FROM pick_progress pp
            WHERE pp.batchno = ? AND pp.num > pp.anum
              AND pp.floor = ? AND pp.picktype = 1
        """, (active, floor))
        result = []
        for r in rows:
            d = dict(r)
            d['wave_num'] = 0
            result.append(d)

    result = sorted(result, key=lambda r: _location_sort_key(r.get('model', '')))
    return jsonify(result)


# ── 1d. 单票分拣：订单列表 ────────────────────────────────────────────────────
@app.get('/api/single-ticket/orders')
def single_ticket_orders():
    """单票分拣：返回当前批次所有有剩余货品的订单列表。"""
    db = get_db_conn()
    active = qval(db, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    if not active:
        return jsonify([])

    # 获取 label_data（格式 orderno-N）和 customer
    ld_rows = qall(db, """
        SELECT DISTINCT label_data, COALESCE(customer, '') AS customer
        FROM sorting_rules WHERE batchno=? AND COALESCE(label_data,'') != ''
        ORDER BY label_data
    """, (active,))

    # Python 侧按真实 orderno（去掉最后 -N）去重
    seen = {}
    for r in ld_rows:
        ld = r['label_data'] or ''
        orderno = ld.rsplit('-', 1)[0] if '-' in ld else ld
        if orderno not in seen:
            seen[orderno] = r['customer']

    # 查各订单剩余件数
    result = []
    for orderno, customer in seen.items():
        pattern = orderno + '-%'
        row = qall(db, f"""
            SELECT SUM(pp.num - pp.anum) AS remaining
            FROM pick_progress pp
            WHERE pp.batchno=? AND pp.num > pp.anum
              AND pp.barcode IN (
                  SELECT DISTINCT barcode FROM sorting_rules
                  WHERE batchno=? AND (label_data LIKE ? OR label_data=?)
              )
        """, (active, active, pattern, orderno))
        remaining = int((row[0]['remaining'] or 0) if row else 0)
        if remaining > 0:
            result.append({'orderno': orderno, 'customer': customer, 'remaining': remaining})
    return jsonify(result)


# ── 1b. PDA 撤销上一次拣货扫码 ────────────────────────────────────────────────
@app.post('/api/pda/unscan')
def pda_unscan():
    """
    撤销上一次 PDA 拣货扫码：pick_progress.anum-1，最后一条 status=2 规则改回 status=1。
    body: {"barcode": str, "floor": int}
    """
    body    = request.get_json() or {}
    barcode = body.get('barcode', '').strip()
    floor   = int(body.get('floor', 0))
    if not barcode:
        return jsonify({"ok": False, "msg": "barcode 不能为空"}), 400
    db = get_db_conn()
    active = qval(db, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    if not active:
        return jsonify({"ok": False, "msg": "当前无活跃批次"}), 400
    rows = qall(db,
        "SELECT id, num, anum FROM pick_progress WITH (UPDLOCK, HOLDLOCK) "
        "WHERE batchno=? AND barcode=? AND floor=?",
        (active, barcode, floor))
    if not rows:
        return jsonify({"ok": False, "msg": "条码不在当前批次"})
    r = dict(rows[0])
    anum = int(r.get('anum') or 0)
    if anum <= 0:
        return jsonify({"ok": False, "msg": "该条码尚未拣货，无需撤销"})
    rule_rows = qall(db,
        "SELECT TOP 1 id FROM sorting_rules WITH (UPDLOCK, HOLDLOCK) "
        "WHERE batchno=? AND barcode=? AND status=2 ORDER BY id DESC",
        (active, barcode))
    try:
        execute(db,
            "UPDATE pick_progress SET anum=anum-1, updated_at=GETDATE() "
            "WHERE batchno=? AND barcode=? AND floor=?",
            (active, barcode, floor))
        if rule_rows:
            execute(db, "UPDATE sorting_rules SET status=1 WHERE id=?",
                    (dict(rule_rows[0])['id'],))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"ok": True, "msg": f"已撤销，当前已拣 {anum - 1} 件"})


# ── 1c. 当前活跃批次回退拣货 ───────────────────────────────────────────────────
def _rollback_batch_records(db, batchno: str) -> dict | None:
    """强制回退批次，并清除本地落包事件；调用方负责提交或回滚事务。"""
    rule_count = qval(db,
        "SELECT COUNT(*) FROM sorting_rules WHERE batchno=?",
        (batchno,)) or 0
    if not rule_count:
        return None

    scanned_rules = qval(db,
        "SELECT COUNT(*) FROM sorting_rules WHERE batchno=? AND status>=3",
        (batchno,)) or 0
    scan_events = qval(db,
        "SELECT COUNT(*) FROM scan_events WHERE batchno=?",
        (batchno,)) or 0

    # 可上机、已落包、格口已清全部恢复为等待拣货；已归档(status=5)不回退。
    execute(db,
        "UPDATE sorting_rules SET status=1 "
        "WHERE batchno=? AND status IN (2,3,4)",
        (batchno,))
    execute(db,
        "DELETE FROM scan_events WHERE batchno=?",
        (batchno,))
    execute(db,
        "UPDATE pick_progress SET anum=0, updated_at=GETDATE() WHERE batchno=?",
        (batchno,))

    # 保留格口分配，重置落包数量和超时计时。
    execute(db, """
        UPDATE sort_ports
        SET fj_num=0, modified_at=GETDATE()
        WHERE portno IN (
            SELECT DISTINCT innerport FROM sorting_rules
            WHERE batchno=? AND innerport!=0
        )
    """, (batchno,))

    # 重新统计每个格口应落数量；规则仍完整保留。
    execute(db, """
        UPDATE sp SET sp.init_num = cnt.c
        FROM sort_ports sp
        JOIN (
            SELECT innerport, COUNT(*) AS c
            FROM sorting_rules
            WHERE batchno=? AND status IN (1,2,3,4) AND innerport!=0
            GROUP BY innerport
        ) cnt ON sp.portno = cnt.innerport
    """, (batchno,))
    return {
        "rule_count": rule_count,
        "scanned_rules": scanned_rules,
        "scan_events": scan_events,
    }


@app.post('/api/batch/active/rollback')
def rollback_active_batch():
    """
    活跃批次强制回退：规则恢复等待拣货，清除本地落包事件并重置格口。
    """
    db = get_db_conn()
    active = qval(db, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    if not active:
        return jsonify({"ok": False, "msg": "当前无活跃批次"}), 400
    try:
        result = _rollback_batch_records(db, active)
        if result is None:
            return jsonify({"ok": False, "msg": f"批次 {active} 不存在"}), 404
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({
        "ok": True,
        "msg": (f"批次 {active} 已回退为等待拣货；"
                f"已清除本地落包记录 {result['scan_events']} 条")
    })


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


# ── 3a. 手工：扫货品 → 查目标格口 ────────────────────────────────────────────
@app.get('/api/manual/item-info')
def manual_item_info():
    """手工分拣：扫货品条码 → 返回目标格口号（GKxxx 格式）。"""
    barcode = request.args.get('barcode', '').strip()
    if not barcode:
        return jsonify({"ok": False, "msg": "barcode 不能为空"}), 400
    db = get_db_conn()
    active = qval(db, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    if not active:
        return jsonify({"ok": False, "msg": "当前无活跃批次"}), 400
    rows = qall(db,
        "SELECT goodsnum, model, unit, num, anum, port FROM pick_progress "
        "WHERE batchno=? AND barcode=? AND picktype=1",
        (active, barcode))
    if not rows:
        return jsonify({"ok": False, "msg": "条码不在手工分拣列表"})
    r = dict(rows[0])
    portno = int(r.get('port') or 0)
    return jsonify({
        "ok": True,
        "barcode": barcode,
        "goodsnum": r.get('goodsnum') or '',
        "model": r.get('model') or '',
        "unit": r.get('unit') or '',
        "num": r.get('num') or 0,
        "anum": r.get('anum') or 0,
        "portno": portno,
        "port_label": f"GK{portno:03d}",
    })


# ── 3b. 手工：扫格口 → 查待放货品 ────────────────────────────────────────────
@app.get('/api/manual/port-items')
def manual_port_items():
    """手工分拣：扫格口号（GKxxx）→ 返回该格口所有待放货品。"""
    port_label = request.args.get('port', '').strip().upper()
    if port_label.startswith('GK'):
        try:
            portno = int(port_label[2:])
        except ValueError:
            return jsonify({"ok": False, "msg": "格口号格式错误（应为 GKxxx）"}), 400
    else:
        try:
            portno = int(port_label)
        except ValueError:
            return jsonify({"ok": False, "msg": "格口号格式错误"}), 400
    db = get_db_conn()
    active = qval(db, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    if not active:
        return jsonify({"ok": False, "msg": "当前无活跃批次"}), 400
    rows = qall(db,
        "SELECT barcode, goodsnum, model, unit, num, anum "
        "FROM pick_progress "
        "WHERE batchno=? AND port=? AND picktype=1 AND num > anum",
        (active, portno))
    items = []
    for r in rows:
        d = dict(r)
        d['remaining'] = (d.get('num') or 0) - (d.get('anum') or 0)
        items.append(d)
    return jsonify({
        "ok": True,
        "portno": portno,
        "port_label": f"GK{portno:03d}",
        "items": items,
    })


# ── 3c. 手工：确认放箱 ────────────────────────────────────────────────────────
@app.post('/api/manual/confirm')
def manual_confirm():
    """
    手工分拣：确认一件货品已放入格口（anum+1）。
    body: {"barcode": str, "portno": int}
    """
    body    = request.get_json() or {}
    barcode = body.get('barcode', '').strip()
    portno  = int(body.get('portno') or 0)
    if not barcode or not portno:
        return jsonify({"ok": False, "msg": "barcode 和 portno 不能为空"}), 400
    db = get_db_conn()
    active = qval(db, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    if not active:
        return jsonify({"ok": False, "msg": "当前无活跃批次"}), 400
    rows = qall(db,
        "SELECT num, anum FROM pick_progress "
        "WHERE batchno=? AND barcode=? AND port=? AND picktype=1",
        (active, barcode, portno))
    if not rows:
        return jsonify({"ok": False, "msg": "条码与格口不匹配或不在手工分拣列表"})
    r = dict(rows[0])
    num  = int(r.get('num') or 0)
    anum = int(r.get('anum') or 0)
    if anum >= num:
        return jsonify({"ok": False, "msg": "该货品已全部放入"})
    try:
        execute(db,
            "UPDATE pick_progress SET anum=anum+1, updated_at=GETDATE() "
            "WHERE batchno=? AND barcode=? AND port=? AND picktype=1",
            (active, barcode, portno))
        db.commit()
    except Exception:
        db.rollback()
        raise
    new_anum = anum + 1
    return jsonify({
        "ok": True,
        "done": new_anum >= num,
        "anum": new_anum,
        "num": num,
        "msg": f"已确认 {new_anum}/{num}",
    })


# ── 3d. 查件/异常处理：查询所有待确认货品（含上机+手工） ───────────────────────
@app.get('/api/exception/items')
def exception_items():
    """
    查件面板：返回当前批次所有 anum < num 的货品，覆盖上机（picktype=0）和手工（picktype=1）。
    q: 货号/条码模糊搜索（空=全部）
    floor: 0=全部楼层
    """
    q     = request.args.get('q', '').strip()
    floor = request.args.get('floor', type=int, default=0)
    db    = get_db_conn()
    active = qval(db, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    if not active:
        return jsonify([])

    where = ["pp.batchno = ?", "pp.num > pp.anum"]
    params = [active]
    if floor > 0:
        where.append("pp.floor = ?")
        params.append(floor)
    if q:
        where.append("(pp.goodsnum LIKE ? OR pp.barcode LIKE ?)")
        params.extend([f'%{q}%', f'%{q}%'])

    rows = qall(db, f"""
        SELECT pp.barcode, pp.goodsnum, pp.model, pp.unit,
               pp.num, pp.anum, pp.picktype, pp.port AS portno, pp.floor
        FROM pick_progress pp
        WHERE {" AND ".join(where)}
        ORDER BY pp.picktype, pp.goodsnum
    """, tuple(params))

    result = []
    for r in rows:
        d = dict(r)
        portno = int(d.get('portno') or 0)
        # 上机件：优先取 sorting_rules 里第一条未完成规则的实际 innerport
        if d.get('picktype') == 0:
            actual = qval(db,
                "SELECT TOP 1 innerport FROM sorting_rules "
                "WHERE batchno=? AND barcode=? AND status < 3 AND innerport > 0 "
                "ORDER BY queue_seq ASC, id ASC",
                (active, d['barcode']))
            if actual:
                portno = int(actual)
        d['portno']     = portno
        d['port_label'] = f"GK{portno:03d}" if portno > 0 else "—"
        d['remaining']  = (d.get('num') or 0) - (d.get('anum') or 0)
        result.append(d)

    result = sorted(result, key=lambda r: _location_sort_key(r.get('model', '')))
    return jsonify(result)


# ── 3e. 查件/异常处理：手动确认放入（anum+1，支持两种类型） ────────────────────
@app.post('/api/exception/confirm')
def exception_confirm():
    """
    异常/查件确认：手动将某件货品标记为已放入（anum+1）。
    适用于 picktype=0（上机条码损坏）和 picktype=1（手工）。
    body: {"barcode": str}
    """
    body    = request.get_json() or {}
    barcode = body.get('barcode', '').strip()
    if not barcode:
        return jsonify({"ok": False, "msg": "barcode 不能为空"}), 400
    db = get_db_conn()
    active = qval(db, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    if not active:
        return jsonify({"ok": False, "msg": "当前无活跃批次"}), 400
    rows = qall(db,
        "SELECT num, anum FROM pick_progress WHERE batchno=? AND barcode=?",
        (active, barcode))
    if not rows:
        return jsonify({"ok": False, "msg": "条码不在当前批次"})
    r = dict(rows[0])
    num  = int(r.get('num') or 0)
    anum = int(r.get('anum') or 0)
    if anum >= num:
        return jsonify({"ok": False, "msg": "该货品已全部确认放入"})
    try:
        execute(db,
            "UPDATE pick_progress SET anum=anum+1, updated_at=GETDATE() "
            "WHERE batchno=? AND barcode=?",
            (active, barcode))
        db.commit()
    except Exception:
        db.rollback()
        raise
    new_anum = anum + 1
    return jsonify({
        "ok": True,
        "done": new_anum >= num,
        "anum": new_anum,
        "num": num,
        "msg": f"已确认 {new_anum}/{num}",
    })


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
    stats["active_batchno"] = qval(
        db, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
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
            SUM(CASE WHEN status >= 2 THEN 1 ELSE 0 END) AS picked_qty,
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
            SUM(CASE WHEN status >= 2 THEN 1 ELSE 0 END) AS picked,
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
            "picked": row["picked"] or 0,
            "scanned": row["scanned"] or 0,
        })

    ports_by_order: dict[str, list[int]] = {}
    for row in ports_rows:
        ports_by_order.setdefault(row["orderno"], []).append(row["innerport"])

    orders = []
    for row in rows:
        total = row["total_qty"] or 0
        picked = row["picked_qty"] or 0
        scanned = row["scanned_qty"] or 0
        pending_pick = max(total - picked, 0)
        pending_scan = max(total - scanned, 0)
        if scanned >= total and total:
            status = "done"
        elif scanned > 0:
            status = "partial"
        elif picked >= total and total:
            status = "picked"
        elif picked > 0:
            status = "picking"
        elif row["overflow_qty"]:
            status = "missing"
        else:
            status = "waiting"
        orders.append({
            "orderno": row["orderno"],
            "customer": row["customer"] or "--",
            "floor": row["floor"],
            "ports": ports_by_order.get(row["orderno"], []),
            "total_qty": total,
            "picked_qty": picked,
            "scanned_qty": scanned,
            "pending_pick_qty": pending_pick,
            "pending_scan_qty": pending_scan,
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


# ── 11. 回退拣货完成 ───────────────────────────────────────────────────────────
@app.post('/api/batch/<batchno>/rollback')
def rollback_batch(batchno):
    """
    将批次强制回退为“等待拣货”：
      ① sorting_rules status=2/3/4 → status=1（保留规则）
      ② pick_progress anum → 0
      ③ 删除该批次本地 scan_events
      ④ 保留格口分配，重置该批次相关格口的已落数量和计时

    注意：已经推送到云端的历史事件不会由此接口自动删除。
    """
    db = get_db_conn()
    try:
        result = _rollback_batch_records(db, batchno)
        if result is None:
            return jsonify({"ok": False, "msg": f"批次 {batchno} 不存在"}), 404
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({
        "ok": True,
        "msg": (f"批次 {batchno} 已回退为等待拣货，订单和规则均已保留；"
                f"已清除本地落包记录 {result['scan_events']} 条")
    })


# ── 12. 管理界面页面 ───────────────────────────────────────────────────────────
@app.get('/')
@app.get('/dashboard')
def dashboard():
    return render_template('dashboard.html')


@app.get('/rules')
def rules_page():
    return render_template('rules.html')


# ── 13. PDA 拣货 H5 页面 ───────────────────────────────────────────────────────
@app.get('/pda')
@app.get('/pick')
def pda():
    return render_template('pda.html')


def run_flask_app(port: int = 5010):
    from waitress import serve
    logger.info(f"[Flask] 启动 waitress，监听 :{port}")
    serve(app, host='0.0.0.0', port=port)
