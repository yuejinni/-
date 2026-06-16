"""
sorting/agent_api.py — 云端分拣接口路由实现

8 条路由：
  GET  /agent/rules            → 规则差量下发
  POST /agent/events           → 接收扫码事件（event_key 幂等去重）
  GET  /sorting/dashboard      → 看板页
  POST /sorting/rules          → 手工录入规则
  POST /sorting/batch/assign   → 触发 allocate_ports（原始接口）
  POST /sorting/batch/plan     → 从销货单缓存自动构建批次（UI 调用）
  GET  /sorting/batches        → 历史批次列表
  GET  /sorting/events         → 扫码事件列表（带分页）
"""
import json
import logging
import os
import sqlite3
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template_string

logger = logging.getLogger(__name__)

sorting_bp = Blueprint('sorting', __name__)

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 本地 SQLite（复用 customs_server 现有 sales_cache）──────────────────────────
# 分拣规则和扫码事件存入独立的表（不影响现有报关功能）

def _get_conn(db_path: str = None):
    """获取分拣云端 SQLite 连接（WAL 模式，30s 超时）。"""
    if db_path is None:
        db_path = os.path.join(_SERVER_DIR, '_sales_cache', 'sorting_cloud.sqlite3')
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30,
                           isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _get_sales_conn():
    """只读连接到 sales_cache.sqlite3（读销货单和商品尺寸）。"""
    db = os.path.join(_SERVER_DIR, '_sales_cache', 'sales_cache.sqlite3')
    if not os.path.exists(db):
        return None
    conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True,
                           timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _fv(d: dict, keys: list, default=''):
    """从字典中按优先顺序取第一个有值的字段。"""
    for k in keys:
        v = d.get(k)
        if v is not None and v != '':
            return v
    return default


def _num(v, default=0):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return default


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

        CREATE TABLE IF NOT EXISTS cloud_sorting_batches (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            batchno       TEXT UNIQUE NOT NULL,
            ver           INTEGER NOT NULL DEFAULT 0,
            order_numbers TEXT,
            orders_count  INTEGER DEFAULT 0,
            rules_count   INTEGER DEFAULT 0,
            created_at    TEXT DEFAULT (datetime('now','localtime'))
        );
    """)
    # 安全升级：添加新列（SQLite 支持 ADD COLUMN，已存在时静默忽略）
    for ddl in [
        "ALTER TABLE cloud_sorting_batches ADD COLUMN box_count INTEGER DEFAULT 0",
        "ALTER TABLE cloud_sorting_batches ADD COLUMN port_min INTEGER DEFAULT 0",
        "ALTER TABLE cloud_sorting_batches ADD COLUMN port_max INTEGER DEFAULT 0",
        "ALTER TABLE cloud_sorting_batches ADD COLUMN total_qty INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(ddl)
        except Exception:
            pass
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


# ── 箱型建议 ────────────────────────────────────────────────────────────────────
@sorting_bp.get('/sorting/batch/hints')
def sorting_batch_hints():
    """
    GET /sorting/batch/hints?order_numbers=SO001,SO002&box1=500&box2=2000&box3=5000&account=祺航箱包
    返回每张订单的箱型建议：
    - 优先读精斗云客户档案 taxPayerNo，值 == "1" → 大箱（3）
    - 否则按体积计算
    """
    order_numbers = [x.strip() for x in (request.args.get('order_numbers') or '').split(',') if x.strip()]
    if not order_numbers:
        return jsonify({"ok": True, "hints": {}})

    box1 = float(request.args.get('box1', 500) or 500)
    box2 = float(request.args.get('box2', 2000) or 2000)
    box3 = float(request.args.get('box3', 5000) or 5000)
    box_configs = {1: box1, 2: box2, 3: box3}
    account_param = (request.args.get('account') or '').strip()  # 账套名，用于选 JDY client

    sc = _get_sales_conn()
    if not sc:
        return jsonify({"ok": False, "msg": "sales_cache.sqlite3 不存在，请先同步销货单"}), 400

    # 选 JDY client（尽量复用已初始化的单例，不主动初始化）
    import jdy_api
    def _pick_jdy_client(account_name):
        """根据账套名选 JDY client，两个都没有则返回 None。"""
        c2 = jdy_api.get_client_2()
        c1 = jdy_api.get_client()
        if not account_name or account_name == 'all':
            return c2 or c1
        if '箱包' in account_name:
            return c2 or c1
        if '饰品' in account_name:
            return c1 or c2
        return c2 or c1

    jdy_cli = _pick_jdy_client(account_param)
    # 客户 taxPayerNo 缓存（同一次请求内去重）
    _cust_cache: dict = {}

    hints = {}
    for no in order_numbers:
        row = sc.execute(
            "SELECT data_json FROM sales_details WHERE number=? ORDER BY updated_at DESC LIMIT 1",
            (no,)
        ).fetchone()
        if not row:
            hints[no] = {"box_type": 1, "source": "calc", "note": "订单不在缓存中，默认小箱"}
            continue

        order = json.loads(row['data_json'])
        customer_name = str(order.get('customerName') or '').strip()

        # ── 优先：精斗云客户档案 taxPayerNo ───────────────────────────────────
        jdy_box_type = None
        if jdy_cli and customer_name:
            if customer_name not in _cust_cache:
                try:
                    cust = jdy_cli.get_customer_by_name(customer_name)
                    _cust_cache[customer_name] = cust
                except Exception as ex:
                    logger.warning(f"[hints] JDY 客户查询失败 {customer_name}: {ex}")
                    _cust_cache[customer_name] = None
            cust = _cust_cache.get(customer_name)
            if cust and str(cust.get('taxPayerNo') or '').strip() == '1':
                jdy_box_type = 3  # 大箱

        if jdy_box_type is not None:
            hints[no] = {
                "box_type": jdy_box_type,
                "source":   "jdy_pref",
                "note":     f"客户档案 taxPayerNo=1（{customer_name}）",
            }
            continue

        # ── 兜底：按体积计算 ───────────────────────────────────────────────────
        total_vol = 0.0
        for entry in (order.get('entries') or []):
            if not isinstance(entry, dict):
                continue
            barcode = str(_fv(entry, ['barcode', 'barCode', 'productBarcode']) or '').strip()
            goodsno = str(_fv(entry, ['code', 'productNumber', 'number']) or '').strip()
            qty = int(_num(_fv(entry, ['qty', 'quantity', 'baseQty', 'mainQty'], 0)))
            if not barcode or qty <= 0:
                continue
            prod = sc.execute(
                "SELECT length, width, height FROM jdy_products WHERE barcode=? OR product_number=? LIMIT 1",
                (barcode, goodsno)
            ).fetchone()
            if prod:
                total_vol += _num(prod['length']) * _num(prod['width']) * _num(prod['height']) * qty

        if total_vol <= 0:
            hints[no] = {"box_type": 1, "source": "calc", "note": "无尺寸数据，默认小箱"}
        elif total_vol <= box_configs[1]:
            hints[no] = {"box_type": 1, "source": "calc", "note": f"体积 {total_vol:.0f}cm³ ≤ 小箱 {box_configs[1]:.0f}"}
        elif total_vol <= box_configs[2]:
            hints[no] = {"box_type": 2, "source": "calc", "note": f"体积 {total_vol:.0f}cm³ ≤ 中箱 {box_configs[2]:.0f}"}
        elif total_vol <= box_configs[3]:
            hints[no] = {"box_type": 3, "source": "calc", "note": f"体积 {total_vol:.0f}cm³ ≤ 大箱 {box_configs[3]:.0f}"}
        else:
            hints[no] = {"box_type": 3, "source": "calc", "note": f"体积 {total_vol:.0f}cm³ 超出大箱上限"}

    sc.close()
    return jsonify({"ok": True, "hints": hints})


# ── 从销货单缓存构建批次 ────────────────────────────────────────────────────────
@sorting_bp.post('/sorting/batch/plan')
def sorting_batch_plan():
    """
    POST /sorting/batch/plan
    body: {"batchno": str, "order_numbers": [...], "box_configs": {1:vol1, 2:vol2, 3:vol3}}
    从本地销货单缓存查商品尺寸，自动调 allocate_ports，写入云端规则。
    ⚠️ 依赖 sales_cache.sqlite3 已同步，否则商品尺寸为 0（依然可分拣，但分箱不准）。
    """
    data            = request.get_json() or {}
    batchno         = (data.get('batchno') or '').strip()
    order_nos       = data.get('order_numbers') or []
    box_configs     = {int(k): float(v) for k, v in (data.get('box_configs') or {}).items()}
    order_box_types = data.get('order_box_types') or {}  # {order_no: 1/2/3}
    if not batchno:
        return jsonify({"ok": False, "msg": "batchno 不能为空"}), 400
    if not order_nos:
        return jsonify({"ok": False, "msg": "order_numbers 不能为空"}), 400
    if not box_configs:
        box_configs = {1: 500.0, 2: 2000.0, 3: 5000.0}

    sc = _get_sales_conn()
    if not sc:
        return jsonify({"ok": False, "msg": "sales_cache.sqlite3 不存在，请先同步销货单"}), 400

    orders = []
    for no in order_nos:
        row = sc.execute(
            "SELECT data_json FROM sales_details WHERE number=? ORDER BY updated_at DESC LIMIT 1",
            (no,)
        ).fetchone()
        if not row:
            continue
        order = json.loads(row['data_json'])
        customer = str(order.get('customerName') or '').strip()
        goods = []
        for entry in (order.get('entries') or []):
            if not isinstance(entry, dict):
                continue
            barcode  = str(_fv(entry, ['barcode', 'barCode', 'productBarcode']) or '').strip()
            goodsno  = str(_fv(entry, ['code', 'productNumber', 'number']) or '').strip()
            goodsmodel = str(_fv(entry, ['spec', 'specification', 'model', 'goodsModel']) or '').strip()
            qty      = int(_num(_fv(entry, ['qty', 'quantity', 'baseQty', 'mainQty'], 0)))
            if not barcode or qty <= 0:
                continue
            # 查商品尺寸（barcode 优先，product_number 兜底）
            prod = sc.execute(
                "SELECT length, width, height FROM jdy_products "
                "WHERE barcode=? OR product_number=? LIMIT 1",
                (barcode, goodsno)
            ).fetchone()
            l = float(prod['length'] or 0) if prod and prod['length'] else 0.0
            w = float(prod['width']  or 0) if prod and prod['width']  else 0.0
            h = float(prod['height'] or 0) if prod and prod['height'] else 0.0
            goods.append({
                'barcode':    barcode,
                'goodsno':    goodsno,
                'goodsmodel': goodsmodel,
                'customer':   customer,
                'l': l, 'w': w, 'h': h,
                'qty':        qty,
                'serialnum':  0,
                'picktype':   0,
            })
        if goods:
            order_dict = {'orderno': no, 'goods': goods}
            # 支持每张订单的箱型覆盖（前端传入的 order_box_types）
            bt_str = str(order_box_types.get(no, ''))
            if bt_str in ('1', '2', '3'):
                order_dict['box_type_override'] = int(bt_str)
            orders.append(order_dict)
    sc.close()

    if not orders:
        return jsonify({"ok": False, "msg": "所选销货单无有效商品（缓存未同步或条码为空）"}), 400

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
            r['portno'],  r['innerport'],
            r.get('customer'), r.get('goodsno'), r.get('goodsmodel'),
            r['floor'], r['serialnum'],
            r['label_data'], r['box_type'], r.get('picktype', 0)
        ))
    conn.execute("UPDATE cloud_rule_version SET ver=? WHERE id=1", (new_ver,))
    # 计算批次统计字段
    port_set  = {r['portno'] for r in rules}
    box_count = len({r['label_data'] for r in rules})
    port_min  = min(port_set) if port_set else 0
    port_max  = max(port_set) if port_set else 0
    conn.execute("""
        INSERT OR REPLACE INTO cloud_sorting_batches
            (batchno, ver, order_numbers, orders_count, rules_count,
             box_count, port_min, port_max, total_qty, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
    """, (batchno, new_ver, json.dumps(order_nos, ensure_ascii=False),
          len(orders), len(rules), box_count, port_min, port_max, len(rules)))
    conn.close()

    return jsonify({
        "ok":           True,
        "batchno":      batchno,
        "ver":          new_ver,
        "orders_count": len(orders),
        "rules_count":  len(rules),
        "box_count":    box_count,
        "port_min":     port_min,
        "port_max":     port_max,
    })


# ── 历史批次列表 ────────────────────────────────────────────────────────────────
@sorting_bp.get('/sorting/batches')
def sorting_batch_list():
    """GET /sorting/batches  最近 50 批次（含格口统计和订单列表）。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM cloud_sorting_batches ORDER BY id DESC LIMIT 50"
    ).fetchall()
    batches = []
    for r in rows:
        b = dict(r)
        # 从 cloud_sorting_rules 实时计算格口统计（兼容旧批次）
        stats = conn.execute("""
            SELECT COUNT(DISTINCT label_data) as box_count,
                   COUNT(DISTINCT portno)     as port_count,
                   MIN(portno)                as port_min,
                   MAX(portno)                as port_max
            FROM cloud_sorting_rules WHERE batchno = ?
        """, (b['batchno'],)).fetchone()
        if stats:
            b['box_count']  = stats['box_count']  or b.get('box_count', 0)
            b['port_count'] = stats['port_count']  or 0
            b['port_min']   = stats['port_min']    or b.get('port_min', 0)
            b['port_max']   = stats['port_max']    or b.get('port_max', 0)
        # 解析订单列表（供前端展开显示）
        try:
            b['order_list'] = json.loads(b.get('order_numbers') or '[]')
        except Exception:
            b['order_list'] = []
        batches.append(b)
    conn.close()
    return jsonify({"batches": batches})


# ── 扫码事件列表 ────────────────────────────────────────────────────────────────
@sorting_bp.get('/sorting/events')
def sorting_events_api():
    """
    GET /sorting/events?batchno=...&limit=200
    用于 UI 看板展示，不影响 /agent/events 推送路径。
    """
    batchno = request.args.get('batchno', '').strip()
    limit   = request.args.get('limit', 200, type=int)
    conn    = _get_conn()
    if batchno:
        rows = conn.execute(
            "SELECT * FROM cloud_scan_events WHERE batchno=? ORDER BY id DESC LIMIT ?",
            (batchno, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM cloud_scan_events ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return jsonify({"events": [dict(r) for r in rows], "total": len(rows)})


# 初始化表（模块导入时执行）
try:
    _ensure_tables()
except Exception as _e:
    logger.warning(f"[sorting] 初始化表失败: {_e}")
