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
import sys
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template_string

logger = logging.getLogger(__name__)

sorting_bp = Blueprint('sorting', __name__)

if getattr(sys, 'frozen', False):
    # PyInstaller unpacks modules under Temp; runtime data belongs next to server.exe.
    _SERVER_DIR = os.path.dirname(sys.executable)
else:
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


def _get_sales_conn_rw():
    """可写连接到 sales_cache.sqlite3（用于回写商品尺寸等）。"""
    db = os.path.join(_SERVER_DIR, '_sales_cache', 'sales_cache.sqlite3')
    if not os.path.exists(db):
        return None
    conn = sqlite3.connect(db, timeout=10, check_same_thread=False)
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
            entry_id    INTEGER DEFAULT 0,
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

        CREATE TABLE IF NOT EXISTS cloud_rush_batches (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            batchno       TEXT UNIQUE NOT NULL,
            order_numbers TEXT,
            orders_count  INTEGER DEFAULT 0,
            items_count   INTEGER DEFAULT 0,
            status        TEXT DEFAULT 'pending',
            created_at    TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS cloud_rush_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            batchno     TEXT NOT NULL,
            orderno     TEXT NOT NULL,
            barcode     TEXT NOT NULL,
            goodsno     TEXT,
            goodsmodel  TEXT,
            customer    TEXT,
            qty         INTEGER DEFAULT 1,
            scanned_qty INTEGER DEFAULT 0,
            status      TEXT DEFAULT 'pending',
            updated_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS IX_cri_batchno ON cloud_rush_items(batchno);
        CREATE INDEX IF NOT EXISTS IX_cri_orderno ON cloud_rush_items(batchno, orderno);

        CREATE TABLE IF NOT EXISTS cloud_rush_orders (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            orderno       TEXT UNIQUE NOT NULL,
            customer_name TEXT,
            total_qty     INTEGER DEFAULT 0,
            status        TEXT DEFAULT 'pending',
            added_at      TEXT DEFAULT (datetime('now','localtime')),
            done_at       TEXT
        );
        CREATE INDEX IF NOT EXISTS IX_cro_status ON cloud_rush_orders(status);

        CREATE TABLE IF NOT EXISTS store_replenishment_tasks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            task_no       TEXT UNIQUE NOT NULL,
            source_order  TEXT NOT NULL,
            account       TEXT DEFAULT '',
            customer_name TEXT DEFAULT '',
            date          TEXT DEFAULT '',
            batch_no      TEXT DEFAULT '',
            status        TEXT DEFAULT 'pending',
            total_qty     INTEGER DEFAULT 0,
            created_at    TEXT DEFAULT (datetime('now','localtime')),
            updated_at    TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS IX_srt_status ON store_replenishment_tasks(status);
        CREATE INDEX IF NOT EXISTS IX_srt_date   ON store_replenishment_tasks(date);

        CREATE TABLE IF NOT EXISTS store_replenishment_items (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            task_no        TEXT NOT NULL,
            barcode        TEXT NOT NULL,
            product_number TEXT DEFAULT '',
            goodsno        TEXT DEFAULT '',
            goodsmodel     TEXT DEFAULT '',
            location       TEXT DEFAULT '',
            qty            INTEGER DEFAULT 1,
            box_no         TEXT DEFAULT '',
            created_at     TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS IX_sri_task ON store_replenishment_items(task_no);
    """)
    # 安全升级：添加新列（SQLite 支持 ADD COLUMN，已存在时静默忽略）
    for ddl in [
        "ALTER TABLE cloud_sorting_batches ADD COLUMN box_count INTEGER DEFAULT 0",
        "ALTER TABLE cloud_sorting_batches ADD COLUMN port_min INTEGER DEFAULT 0",
        "ALTER TABLE cloud_sorting_batches ADD COLUMN port_max INTEGER DEFAULT 0",
        "ALTER TABLE cloud_sorting_batches ADD COLUMN total_qty INTEGER DEFAULT 0",
        # 回退保护字段
        "ALTER TABLE cloud_sorting_batches ADD COLUMN agent_synced_at TEXT",      # Agent 首次拉取时间
        "ALTER TABLE cloud_sorting_batches ADD COLUMN revoke_requested_at TEXT",  # 云端请求撤回时间
        "ALTER TABLE cloud_sorting_batches ADD COLUMN revoke_confirmed_at TEXT",  # Agent 确认撤回时间
        # 箱型配置（生成批次时记录，详情展示用）
        "ALTER TABLE cloud_sorting_batches ADD COLUMN box_configs_json TEXT",
        # 单据行序号
        "ALTER TABLE cloud_sorting_rules ADD COLUMN entry_id INTEGER DEFAULT 0",
        # 配送类型（仓配-仓送/仓店-仓送/店配）
        "ALTER TABLE cloud_sorting_rules ADD COLUMN store_delivery INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(ddl)
        except Exception:
            pass
    conn.close()


def _next_task_no(conn) -> str:
    """生成店补任务编号：SR-YYYYMMDD-NNN"""
    prefix = 'SR-' + datetime.now().strftime('%Y%m%d') + '-'
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM store_replenishment_tasks WHERE task_no LIKE ?",
        (prefix + '%',)
    ).fetchone()
    seq = (row['cnt'] if row else 0) + 1
    return f'{prefix}{seq:03d}'


def _create_store_replenishment_task(conn, source_order: str, account: str,
                                      customer: str, date: str,
                                      store_goods: list, batch_no: str) -> str:
    """将店面配货的明细写入店补任务表，返回 task_no。"""
    task_no   = _next_task_no(conn)
    total_qty = sum(g['qty'] for g in store_goods)
    conn.execute("""
        INSERT INTO store_replenishment_tasks
            (task_no, source_order, account, customer_name, date, batch_no, status, total_qty)
        VALUES (?,?,?,?,?,?,?,?)
    """, (task_no, source_order, account, customer, date, batch_no, 'pending', total_qty))
    for g in store_goods:
        conn.execute("""
            INSERT INTO store_replenishment_items
                (task_no, barcode, product_number, goodsno, goodsmodel, location, qty)
            VALUES (?,?,?,?,?,?,?)
        """, (task_no, g['barcode'], g.get('goodsno',''), g.get('goodsno',''),
              g.get('goodsmodel',''), g.get('location',''), g['qty']))
    return task_no


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
    # Agent 拉到新规则时，标记对应批次为"已下发"
    if rows:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for bn in {r['batchno'] for r in rows}:
            try:
                conn.execute(
                    "UPDATE cloud_sorting_batches SET agent_synced_at=? "
                    "WHERE batchno=? AND agent_synced_at IS NULL",
                    (now, bn)
                )
            except Exception:
                pass
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
                 customer, goodsno, goodsmodel, floor, serialnum, label_data, box_type, picktype, entry_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            new_ver, batchno,
            r.get('barcode'), r.get('slot_seq', 1),
            r.get('portno', 0), r.get('innerport', 0),
            r.get('customer'), r.get('goodsno'), r.get('goodsmodel'),
            r.get('floor', 0), r.get('serialnum', 0),
            r.get('label_data'), r.get('box_type', 1), r.get('picktype', 0), r.get('entry_id', 0)
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
                 customer, goodsno, goodsmodel, floor, serialnum, label_data, box_type, picktype, entry_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            new_ver, batchno,
            r['barcode'], r['slot_seq'],
            r['portno'], r['innerport'],
            r.get('customer'), r.get('goodsno'), r.get('goodsmodel'),
            r['floor'], r['serialnum'],
            r['label_data'], r['box_type'], r.get('picktype', 0), r.get('entry_id', 0)
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
            hints[no] = {"box_type": 2, "source": "default", "note": "订单不在缓存中，默认中箱"}
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
                "note":     f"客户档案 taxPayerNo=1（{customer_name}）→ 大箱",
            }
        else:
            # 兜底：中箱（小箱仅手工指定）
            hints[no] = {"box_type": 2, "source": "default", "note": "默认中箱"}

    sc.close()
    return jsonify({"ok": True, "hints": hints})


# ── 销货订单列表（分拣选单用）────────────────────────────────────────────────────
@sorting_bp.get('/sorting/order-requests')
def sorting_order_requests():
    """
    GET /sorting/order-requests?date=YYYY-MM-DD&account=all&search=&limit=500
    返回已审核销货订单（XHDD），过滤掉「店配-店送」，标记仓店单的 store_delivery。
    """
    date_str = (request.args.get('date') or '').strip()
    account  = (request.args.get('account') or 'all').strip()
    search   = (request.args.get('search') or '').strip().lower()
    limit    = max(1, min(int(request.args.get('limit') or 500), 1000))

    sc = _get_sales_conn()
    if not sc:
        return jsonify({"ok": True, "list": [], "count": 0,
                        "note": "sales_cache 不存在"}), 200

    try:
        clauses = [
            "json_extract(data_json, '$.checkStatus') = 1",
            "COALESCE(delivery_type, '') != '店配-店送'",
        ]
        params = []
        if date_str:
            clauses.append("date = ?")
            params.append(date_str[:10])
        if account and account != 'all':
            clauses.append("account = ?")
            params.append(account)
        where = " AND ".join(clauses)

        rows = sc.execute(
            f"SELECT number, date, account, customer_name, total_qty, total_amount, "
            f"delivery_type, data_json, updated_at "
            f"FROM sales_order_requests WHERE {where} "
            f"ORDER BY date DESC, number DESC LIMIT ?",
            params + [limit]
        ).fetchall()
    except Exception as e:
        sc.close()
        return jsonify({"ok": False, "error": str(e)}), 500

    result = []
    for r in rows:
        dj = json.loads(r['data_json'] or '{}')
        entries = dj.get('entries') or []
        # 判断是否含非新大仓库的货（仓店-仓送时需标记）
        delivery_type = r['delivery_type'] or ''
        has_store_loc = (
            delivery_type == '仓店-仓送' and
            any(str(e.get('location') or '') != '新大仓库' for e in entries)
        )
        item = {
            'number':       r['number'],
            'date':         r['date'],
            'account':      r['account'],
            'customerName': r['customer_name'],
            'totalQty':     r['total_qty'],
            'totalAmount':  r['total_amount'],
            'deliveryType': delivery_type,
            'storeDelivery': 1 if has_store_loc else 0,
            'updatedAt':    r['updated_at'],
        }
        if search:
            text = f"{item['number']} {item['customerName']} {item['account']}".lower()
            if search not in text:
                continue
        result.append(item)

    sc.close()
    return jsonify({"ok": True, "list": result, "count": len(result)})


# ── 手动触发 JDY 销货订单同步 ────────────────────────────────────────────────────
@sorting_bp.post('/sorting/sync-order-requests')
def sorting_sync_order_requests():
    """
    POST /sorting/sync-order-requests
    body: {"date": "YYYY-MM-DD", "account": "祺航饰品"}
    手动从 JDY 拉取当日（或指定日期）已审核销货订单，写入 sales_order_requests。
    """
    from server import _refresh_sales_order_request_from_jdy, _sales_cache_conn
    data    = request.get_json() or {}
    date_str = (data.get('date') or '').strip()
    account  = (data.get('account') or '').strip()

    # 先查本地库，找到当日所有已知单号
    sc = _get_sales_conn()
    if not sc:
        return jsonify({"ok": False, "msg": "sales_cache 不存在"}), 400

    results = {"written": 0, "skipped": 0, "errors": []}

    # 查出当日已有的单号
    existing_numbers = []
    if date_str:
        rows = sc.execute(
            "SELECT number FROM sales_order_requests WHERE date=? AND (account=? OR ?='')",
            (date_str, account, account)
        ).fetchall()
        existing_numbers = [r['number'] for r in rows]
    sc.close()

    # 对每个单号刷新（或直接搜索 JDY 当日列表）
    for no in existing_numbers:
        try:
            res = _refresh_sales_order_request_from_jdy(
                account=account,
                query=no,
                mode='number',
                endpoint='/jdyscm/saleOrder/list',
            )
            if res.get('written'):
                results['written'] += res['written']
            else:
                results['skipped'] += 1
        except Exception as e:
            results['errors'].append(f"{no}: {e}")

    return jsonify({"ok": True, **results})


# ── 检查缺失尺寸商品 ─────────────────────────────────────────────────────────────
@sorting_bp.post('/sorting/batch/check-dims')
def sorting_batch_check_dims():
    """
    POST /sorting/batch/check-dims
    body: {"order_numbers": [...]}
    返回所选订单中缺少尺寸（l/w/h 任意为 0）的商品列表，用于前端弹窗补录。
    同一条码只返回一次（跨订单去重）。
    """
    data      = request.get_json() or {}
    order_nos = data.get('order_numbers') or []

    sc = _get_sales_conn()
    if not sc:
        return jsonify({"ok": False, "msg": "sales_cache 不存在"}), 400

    missing      = []
    seen_barcodes = set()

    for no in order_nos:
        # 优先查销货订单（XHDD），兜底查销货单（XH）
        row = sc.execute(
            "SELECT data_json FROM sales_order_request_details WHERE number=? ORDER BY updated_at DESC LIMIT 1",
            (no,)
        ).fetchone()
        if not row:
            row = sc.execute(
                "SELECT data_json FROM sales_details WHERE number=? ORDER BY updated_at DESC LIMIT 1",
                (no,)
            ).fetchone()
        if not row:
            continue
        order = json.loads(row['data_json'])
        for entry in (order.get('entries') or []):
            if not isinstance(entry, dict):
                continue
            goodsno  = str(_fv(entry, ['code', 'productNumber', 'number']) or '').strip()
            goodsmodel = str(_fv(entry, ['spec', 'specification', 'model', 'goodsModel']) or '').strip()
            qty      = int(_num(_fv(entry, ['qty', 'quantity', 'baseQty', 'mainQty'], 0)))
            barcode  = str(_fv(entry, ['barcode', 'barCode', 'productBarcode']) or '').strip()
            if not barcode and goodsno:
                prod_b = sc.execute(
                    "SELECT barcode FROM jdy_products WHERE product_number=? AND barcode!='' LIMIT 1",
                    (goodsno,)
                ).fetchone()
                if prod_b:
                    barcode = prod_b['barcode']
            if not barcode or qty <= 0 or barcode in seen_barcodes:
                continue
            prod = sc.execute(
                "SELECT length, width, height FROM jdy_products WHERE barcode=? OR product_number=? LIMIT 1",
                (barcode, goodsno)
            ).fetchone()
            l = float(prod['length'] or 0) if prod and prod['length'] else 0.0
            w = float(prod['width']  or 0) if prod and prod['width']  else 0.0
            h = float(prod['height'] or 0) if prod and prod['height'] else 0.0
            if not (l > 0 and w > 0 and h > 0):
                seen_barcodes.add(barcode)
                missing.append({'barcode': barcode, 'goodsno': goodsno, 'goodsmodel': goodsmodel})

    sc.close()
    return jsonify({"ok": True, "missing": missing})


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
    order_box_types = data.get('order_box_types') or {}   # {order_no: 1/2/3}
    dim_overrides   = data.get('dim_overrides') or {}     # {barcode: {l, w, h}}
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
    _store_replen_orders = []   # [{source_order, account, customer, date, goods:[...]}]
    for no in order_nos:
        # 优先查销货订单（XHDD），兜底查销货单（XH）
        row = sc.execute(
            "SELECT data_json FROM sales_order_request_details WHERE number=? ORDER BY updated_at DESC LIMIT 1",
            (no,)
        ).fetchone()
        if not row:
            row = sc.execute(
                "SELECT data_json FROM sales_details WHERE number=? ORDER BY updated_at DESC LIMIT 1",
                (no,)
            ).fetchone()
        if not row:
            continue
        order = json.loads(row['data_json'])
        customer      = str(order.get('customerName') or '').strip()
        delivery_type = str(order.get('deliveryType') or '').strip()
        order_date    = str(order.get('date') or '')[:10]
        machine_goods = []   # → 分拣机
        store_goods   = []   # → 店补系统
        raw_entries = order.get('_raw', {}).get('entries', [])
        for i, entry in enumerate(order.get('entries') or []):
            if not isinstance(entry, dict):
                continue
            goodsno    = str(_fv(entry, ['code', 'productNumber', 'number']) or '').strip()
            goodsmodel = str(_fv(entry, ['spec', 'specification', 'model', 'goodsModel']) or '').strip()
            qty        = int(_num(_fv(entry, ['qty', 'quantity', 'baseQty', 'mainQty'], 0)))
            location   = str(entry.get('location') or '').strip()
            barcode    = str(_fv(entry, ['barcode', 'barCode', 'productBarcode']) or '').strip()
            # XHDD 条码可能为空，按货号从 jdy_products 查
            if not barcode and goodsno:
                prod_b = sc.execute(
                    "SELECT barcode FROM jdy_products WHERE product_number=? AND barcode!='' LIMIT 1",
                    (goodsno,)
                ).fetchone()
                if prod_b:
                    barcode = prod_b['barcode']
            if not barcode or qty <= 0:
                continue
            entry_id = raw_entries[i].get('entryId', i + 1) if i < len(raw_entries) else (i + 1)
            # 仓店-仓送 + 非新大仓库 → 店补
            is_store = (delivery_type == '仓店-仓送' and location and location != '新大仓库')
            if is_store:
                store_goods.append({
                    'barcode': barcode, 'goodsno': goodsno,
                    'goodsmodel': goodsmodel, 'location': location, 'qty': qty,
                })
                continue
            # 查商品尺寸 + brand（barcode 优先，product_number 兜底）
            try:
                prod = sc.execute(
                    "SELECT length, width, height, brand FROM jdy_products "
                    "WHERE barcode=? OR product_number=? LIMIT 1",
                    (barcode, goodsno)
                ).fetchone()
                brand = str((prod['brand'] if prod else '') or '').strip()
            except Exception:
                prod = sc.execute(
                    "SELECT length, width, height FROM jdy_products "
                    "WHERE barcode=? OR product_number=? LIMIT 1",
                    (barcode, goodsno)
                ).fetchone()
                brand = ''
            picktype = 1 if brand == '手工' else 0
            if barcode in dim_overrides:
                ov = dim_overrides[barcode]
                l = float(ov.get('l') or 0)
                w = float(ov.get('w') or 0)
                h = float(ov.get('h') or 0)
            else:
                l = float(prod['length'] or 0) if prod and prod['length'] else 0.0
                w = float(prod['width']  or 0) if prod and prod['width']  else 0.0
                h = float(prod['height'] or 0) if prod and prod['height'] else 0.0
            machine_goods.append({
                'barcode': barcode, 'goodsno': goodsno, 'goodsmodel': goodsmodel,
                'customer': customer, 'l': l, 'w': w, 'h': h,
                'qty': qty, 'serialnum': 0, 'picktype': picktype,
                'entry_id': entry_id, 'store_delivery': 0,
            })
        # 有店补货 → 暂存，稍后建任务
        if store_goods:
            _store_replen_orders.append({
                'source_order': no, 'account': data.get('account') or '',
                'customer': customer, 'date': order_date, 'goods': store_goods,
            })
        if machine_goods:
            order_dict = {'orderno': no, 'goods': machine_goods}
            bt_str = str(order_box_types.get(no, ''))
            if bt_str in ('1', '2', '3'):
                order_dict['box_type_override'] = int(bt_str)
            orders.append(order_dict)
    sc.close()

    # 将补录的尺寸回写到 jdy_products（下次生成无需再补）
    if dim_overrides:
        sc_rw = _get_sales_conn_rw()
        if sc_rw:
            for bc, ov in dim_overrides.items():
                sc_rw.execute(
                    "UPDATE jdy_products SET length=?, width=?, height=? WHERE barcode=?",
                    (float(ov.get('l') or 0), float(ov.get('w') or 0), float(ov.get('h') or 0), bc)
                )
            sc_rw.commit()
            sc_rw.close()

    if not orders:
        return jsonify({"ok": False, "msg": "所选销货单无有效商品（缓存未同步或条码为空）"}), 400

    # ── 服务端自动箱型决策：taxPayerNo==1 → 大箱(3)；其余默认中箱(2，由 batch_planner 兜底）─
    # 仅对未手工指定箱型的订单生效
    try:
        import jdy_api
        account_param = (data.get('account') or '').strip()
        def _pick_cli(acc):
            c2 = jdy_api.get_client_2()
            c1 = jdy_api.get_client()
            if '饰品' in acc: return c1 or c2
            return c2 or c1
        jdy_cli = _pick_cli(account_param)
        if jdy_cli:
            _cust_cache = {}
            for order_dict in orders:
                if 'box_type_override' in order_dict:
                    continue  # 已手工指定，不覆盖
                customer = order_dict['goods'][0]['customer'] if order_dict['goods'] else ''
                if not customer:
                    continue
                if customer not in _cust_cache:
                    try:
                        _cust_cache[customer] = jdy_cli.get_customer_by_name(customer)
                    except Exception:
                        _cust_cache[customer] = None
                cust = _cust_cache.get(customer)
                if cust and str(cust.get('taxPayerNo') or '').strip() == '1':
                    order_dict['box_type_override'] = 3  # 大箱
                # 否则不设 override → batch_planner 用默认中箱(2)
    except Exception as ex:
        logger.warning(f"[batch_plan] JDY 客户箱型检测失败（将用默认中箱）: {ex}")

    from sorting.batch_planner import allocate_ports
    rules = allocate_ports(orders, box_configs)

    conn = _get_conn()
    ver_row = conn.execute("SELECT ver FROM cloud_rule_version WHERE id=1").fetchone()
    new_ver = (ver_row['ver'] if ver_row else 0) + 1

    for r in rules:
        conn.execute("""
            INSERT INTO cloud_sorting_rules
                (ver, batchno, barcode, slot_seq, portno, innerport,
                 customer, goodsno, goodsmodel, floor, serialnum, label_data, box_type, picktype, entry_id, store_delivery)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            new_ver, batchno,
            r['barcode'], r['slot_seq'],
            r['portno'],  r['innerport'],
            r.get('customer'), r.get('goodsno'), r.get('goodsmodel'),
            r['floor'], r['serialnum'],
            r['label_data'], r['box_type'], r.get('picktype', 0), r.get('entry_id', 0),
            r.get('store_delivery', 0)
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
             box_count, port_min, port_max, total_qty, box_configs_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
    """, (batchno, new_ver, json.dumps(order_nos, ensure_ascii=False),
          len(orders), len(rules), box_count, port_min, port_max, len(rules),
          json.dumps(box_configs)))

    # ── 创建店补任务（非新大仓库的货）────────────────────────────────────────────
    replen_task_nos = []
    for sr in _store_replen_orders:
        task_no = _create_store_replenishment_task(
            conn, sr['source_order'], sr['account'],
            sr['customer'], sr['date'], sr['goods'], batchno
        )
        replen_task_nos.append(task_no)

    conn.close()

    return jsonify({
        "ok":                True,
        "batchno":           batchno,
        "ver":               new_ver,
        "orders_count":      len(orders),
        "rules_count":       len(rules),
        "box_count":         box_count,
        "port_min":          port_min,
        "port_max":          port_max,
        "replen_tasks":      replen_task_nos,
        "replen_count":      len(replen_task_nos),
    })


# ── 批次规则详情 ─────────────────────────────────────────────────────────────────
@sorting_bp.get('/sorting/batch/<batchno>/rules')
def sorting_batch_rules(batchno):
    """
    GET /sorting/batch/<batchno>/rules
    返回该批次所有规则，按箱（label_data）分组，每箱内聚合商品数量。
    """
    conn = _get_conn()
    rows = conn.execute("""
        SELECT label_data, portno, barcode, goodsno, goodsmodel, customer,
               COUNT(*) as qty, box_type, picktype, MIN(entry_id) as entry_id
        FROM cloud_sorting_rules
        WHERE batchno=?
        GROUP BY label_data, barcode
        ORDER BY portno, label_data, entry_id
    """, (batchno,)).fetchall()
    batch_row = conn.execute(
        "SELECT * FROM cloud_sorting_batches WHERE batchno=?", (batchno,)
    ).fetchone()
    conn.close()

    boxes = {}
    for r in rows:
        ldata = r['label_data']
        if ldata not in boxes:
            boxes[ldata] = {
                'label_data': ldata,
                'portno': r['portno'],
                'box_type': r['box_type'],
                'items': []
            }
        boxes[ldata]['items'].append({
            'barcode':    r['barcode'],
            'goodsno':    r['goodsno'],
            'goodsmodel': r['goodsmodel'],
            'customer':   r['customer'],
            'qty':        r['qty'],
            'picktype':   r['picktype'],
            'entry_id':   r['entry_id'],
        })

    # 按格口排序
    box_list = sorted(boxes.values(), key=lambda b: (b['portno'], b['label_data']))

    # 补查商品尺寸（jdy_products），算单品体积 + 每箱总体积
    sc = _get_sales_conn()
    if sc:
        all_barcodes = list({it['barcode'] for bx in box_list for it in bx['items']})
        dim_map = {}
        for bc in all_barcodes:
            row = sc.execute(
                "SELECT length, width, height FROM jdy_products WHERE barcode=? LIMIT 1",
                (bc,)
            ).fetchone()
            if row:
                l = float(row['length'] or 0)
                w = float(row['width']  or 0)
                h = float(row['height'] or 0)
                # jdy_products.length/width/height 单位为 cm，直接得 cm³
                dim_map[bc] = {'l': l, 'w': w, 'h': h, 'unit_vol': round(l * w * h, 2)}
        sc.close()

        for bx in box_list:
            bx_vol = 0.0
            for it in bx['items']:
                dim = dim_map.get(it['barcode'], {})
                it['l']         = dim.get('l', 0)
                it['w']         = dim.get('w', 0)
                it['h']         = dim.get('h', 0)
                it['unit_vol']  = dim.get('unit_vol', 0)
                it['total_vol'] = round(dim.get('unit_vol', 0) * it['qty'], 1)
                bx_vol         += it['total_vol']
            bx['box_vol'] = round(bx_vol, 1)

    # 解析存储的箱型配置（用于前端显示进度条）
    box_configs = {}
    if batch_row and batch_row['box_configs_json']:
        try:
            raw = json.loads(batch_row['box_configs_json'])
            box_configs = {int(k): float(v) for k, v in raw.items()}
        except Exception:
            pass
    if not box_configs:
        box_configs = {1: 500.0, 2: 2000.0, 3: 5000.0}

    return jsonify({
        "ok":          True,
        "batchno":     batchno,
        "batch":       dict(batch_row) if batch_row else {},
        "boxes":       box_list,
        "box_count":   len(box_list),
        "total_lines": len(rows),
        "box_configs": box_configs,
    })


# ── 管理员密码验证（独立读 auth_users.sqlite3）───────────────────────────────────
def _verify_admin_password(username: str, password: str) -> bool:
    """验证是否为 active 状态的 admin 用户，不依赖 server.py 的 session。"""
    import hashlib
    import hmac as _hmac
    auth_db = os.path.join(_SERVER_DIR, 'auth_users.sqlite3')
    if not os.path.exists(auth_db):
        return False
    try:
        conn = sqlite3.connect(auth_db, timeout=5)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT password_hash FROM users WHERE username=? AND role='admin' AND status='active'",
            (username,)
        ).fetchone()
        conn.close()
        if not row:
            return False
        stored = str(row['password_hash'] or '')
        parts = stored.split('$')
        if len(parts) == 3 and parts[0] == 'pbkdf2_sha256':
            digest = hashlib.pbkdf2_hmac(
                'sha256', (password or '').encode(), parts[1].encode(), 120000
            )
            expected = f'pbkdf2_sha256${parts[1]}${digest.hex()}'
            return _hmac.compare_digest(expected, stored)
        return False
    except Exception as ex:
        logger.error(f"[_verify_admin_password] {ex}")
        return False


# ── 批次状态查询（用于回退前的三档检查）──────────────────────────────────────────
@sorting_bp.get('/sorting/batch/<batchno>/status')
def sorting_batch_status(batchno):
    """
    GET /sorting/batch/<batchno>/status
    返回：agent_synced / revoke_requested / revoke_confirmed / picking_count
    """
    conn = _get_conn()
    batch = conn.execute(
        "SELECT agent_synced_at, revoke_requested_at, revoke_confirmed_at "
        "FROM cloud_sorting_batches WHERE batchno=?", (batchno,)
    ).fetchone()
    picking_count = conn.execute(
        "SELECT COUNT(*) FROM cloud_scan_events WHERE batchno=?", (batchno,)
    ).fetchone()[0]
    conn.close()
    if not batch:
        return jsonify({"ok": False, "msg": "批次不存在"}), 404
    return jsonify({
        "ok":               True,
        "batchno":          batchno,
        "agent_synced":     batch['agent_synced_at'] is not None,
        "agent_synced_at":  batch['agent_synced_at'],
        "revoke_requested": batch['revoke_requested_at'] is not None,
        "revoke_confirmed": batch['revoke_confirmed_at'] is not None,
        "picking_count":    picking_count,
    })


# ── 申请 Agent 撤回规则 ─────────────────────────────────────────────────────────
@sorting_bp.post('/sorting/batch/<batchno>/revoke-request')
def sorting_batch_revoke_request(batchno):
    """POST → 在云端标记"已申请撤回"，Agent 轮询后自行清除本地规则并回调确认。"""
    conn = _get_conn()
    conn.execute(
        "UPDATE cloud_sorting_batches SET revoke_requested_at=datetime('now','localtime') WHERE batchno=?",
        (batchno,)
    )
    conn.close()
    return jsonify({"ok": True, "batchno": batchno})


# ── Agent 轮询待撤回列表 ────────────────────────────────────────────────────────
@sorting_bp.get('/agent/revoke-requests')
def agent_revoke_requests():
    """
    GET /agent/revoke-requests
    Agent 每次轮询调用，返回已申请撤回但尚未确认的批次列表。
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT batchno, revoke_requested_at FROM cloud_sorting_batches "
        "WHERE revoke_requested_at IS NOT NULL AND revoke_confirmed_at IS NULL"
    ).fetchall()
    conn.close()
    return jsonify({"requests": [dict(r) for r in rows]})


# ── Agent 确认已完成撤回 ────────────────────────────────────────────────────────
@sorting_bp.post('/agent/revoke-confirm')
def agent_revoke_confirm():
    """
    POST /agent/revoke-confirm
    body: {"batchno": "..."}
    Agent 清除本地规则后回调，云端记录确认时间，解锁回退操作。
    """
    data = request.get_json() or {}
    batchno = (data.get('batchno') or '').strip()
    if not batchno:
        return jsonify({"ok": False, "msg": "batchno 必填"}), 400
    conn = _get_conn()
    conn.execute(
        "UPDATE cloud_sorting_batches SET revoke_confirmed_at=datetime('now','localtime') WHERE batchno=?",
        (batchno,)
    )
    conn.close()
    logger.info(f"[revoke_confirm] Agent 已确认撤回 {batchno}")
    return jsonify({"ok": True, "batchno": batchno})


# ── 删除批次（回退）────────────────────────────────────────────────────────────
@sorting_bp.delete('/sorting/batch/<batchno>')
def sorting_batch_delete(batchno):
    """
    DELETE /sorting/batch/<batchno>
    删除该批次所有分拣规则 + 批次记录，使相关订单重新变为"未生成批次"状态。
    ⚠️ 本地 Agent 已下发的规则不会自动撤销，需重启 Agent 或等下次覆盖同步。
    """
    data           = request.get_json(silent=True) or {}
    admin_username = (data.get('admin_username') or '').strip()
    admin_password = (data.get('admin_password') or '').strip()
    has_admin      = bool(admin_username and admin_password and
                          _verify_admin_password(admin_username, admin_password))

    conn = _get_conn()
    batch = conn.execute(
        "SELECT agent_synced_at, revoke_requested_at, revoke_confirmed_at "
        "FROM cloud_sorting_batches WHERE batchno=?", (batchno,)
    ).fetchone()
    picking_count = conn.execute(
        "SELECT COUNT(*) FROM cloud_scan_events WHERE batchno=?", (batchno,)
    ).fetchone()[0]

    # ── 三档保护 ──────────────────────────────────────────────────────────────
    # 档1：已开始配货（扫码事件存在）→ 必须管理员密码
    if picking_count > 0 and not has_admin:
        conn.close()
        return jsonify({
            "ok": False,
            "reason": "picking_started",
            "picking_count": picking_count,
            "msg": f"已有 {picking_count} 件商品完成配货扫码，回退需要管理员密码。",
        }), 403

    # 档2：已下发 PC 且未完成撤回 → 需要先撤回或管理员密码
    if batch and batch['agent_synced_at'] and not batch['revoke_confirmed_at'] and not has_admin:
        conn.close()
        return jsonify({
            "ok": False,
            "reason": "agent_synced",
            "revoke_requested": batch['revoke_requested_at'] is not None,
            "msg": "批次规则已下发到仓库PC，请先申请撤回并等待PC端确认，或使用管理员密码强制回退。",
        }), 403

    # 档3（或管理员强制）：执行删除
    try:
        conn.execute("BEGIN")
        rules_del = conn.execute(
            "DELETE FROM cloud_sorting_rules WHERE batchno=?", (batchno,)
        ).rowcount
        batch_del = conn.execute(
            "DELETE FROM cloud_sorting_batches WHERE batchno=?", (batchno,)
        ).rowcount
        conn.execute("COMMIT")
        conn.close()
        if has_admin and picking_count > 0:
            logger.warning(f"[batch_delete] 管理员 {admin_username!r} 强制回退已配货批次 {batchno}（{picking_count}件）")
        return jsonify({"ok": True, "batchno": batchno,
                        "rules_deleted": rules_del, "batch_deleted": batch_del})
    except Exception as e:
        try: conn.execute("ROLLBACK")
        except Exception: pass
        conn.close()
        logger.error(f"[batch_delete] 回退失败 {batchno}: {e}")
        return jsonify({"ok": False, "msg": str(e)}), 500


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


# ── 加急单列表：获取 ──────────────────────────────────────────────────────────
@sorting_bp.get('/sorting/rush/orders')
def sorting_rush_orders_get():
    """
    GET /sorting/rush/orders?status=pending|done|all
    返回加急单列表（不含批次概念，单票管理）。
    """
    status = request.args.get('status', 'all').strip()
    conn   = _get_conn()
    if status in ('pending', 'done'):
        rows = conn.execute(
            "SELECT * FROM cloud_rush_orders WHERE status=? ORDER BY id DESC",
            (status,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM cloud_rush_orders ORDER BY status ASC, id DESC"
        ).fetchall()
    conn.close()
    orders = [dict(r) for r in rows]
    # 按状态分组方便前端
    pending = [o for o in orders if o['status'] == 'pending']
    done    = [o for o in orders if o['status'] == 'done']
    return jsonify({"ok": True, "pending": pending, "done": done, "total": len(orders)})


# ── 加急单列表：添加 ──────────────────────────────────────────────────────────
@sorting_bp.post('/sorting/rush/order')
def sorting_rush_order_add():
    """
    POST /sorting/rush/order
    body: {"orderno": str, "customer_name": str, "total_qty": int}
    添加一张单到加急列表（已存在则更新信息并重置为 pending）。
    """
    data          = request.get_json() or {}
    orderno       = (data.get('orderno') or '').strip()
    customer_name = (data.get('customer_name') or '').strip()
    total_qty     = int(_num(data.get('total_qty', 0)))
    if not orderno:
        return jsonify({"ok": False, "msg": "orderno 不能为空"}), 400

    conn = _get_conn()
    conn.execute("""
        INSERT INTO cloud_rush_orders (orderno, customer_name, total_qty, status, added_at)
        VALUES (?,?,?,'pending',datetime('now','localtime'))
        ON CONFLICT(orderno) DO UPDATE SET
            customer_name=excluded.customer_name,
            total_qty=excluded.total_qty,
            status='pending',
            added_at=datetime('now','localtime'),
            done_at=NULL
    """, (orderno, customer_name, total_qty))
    conn.close()
    return jsonify({"ok": True, "orderno": orderno})


# ── 加急单列表：标记完成 ───────────────────────────────────────────────────────
@sorting_bp.post('/sorting/rush/order/<orderno>/done')
def sorting_rush_order_done(orderno):
    """POST /sorting/rush/order/<orderno>/done  标记该单已完成配货。"""
    conn = _get_conn()
    conn.execute(
        "UPDATE cloud_rush_orders SET status='done', done_at=datetime('now','localtime') WHERE orderno=?",
        (orderno,)
    )
    conn.close()
    return jsonify({"ok": True, "orderno": orderno})


# ── 加急单列表：删除 ──────────────────────────────────────────────────────────
@sorting_bp.delete('/sorting/rush/order/<orderno>')
def sorting_rush_order_delete(orderno):
    """
    DELETE /sorting/rush/order/<orderno>  从加急列表移除（不影响原始订单）。

    回退保护：
      - status=pending → 直接删除
      - status=done（已完成配货）→ 需要管理员密码
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT status FROM cloud_rush_orders WHERE orderno=?", (orderno,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "msg": "加急单不存在"}), 404

    if row['status'] == 'done':
        data         = request.get_json(silent=True) or {}
        admin_user   = (data.get('admin_username') or '').strip()
        admin_pass   = (data.get('admin_password') or '').strip()
        if not _verify_admin_password(admin_user, admin_pass):
            conn.close()
            return jsonify({
                "ok":    False,
                "reason": "picking_done",
                "msg":   "该加急单已完成配货，删除需要管理员密码"
            }), 403

    conn.execute("DELETE FROM cloud_rush_orders WHERE orderno=?", (orderno,))
    conn.close()
    return jsonify({"ok": True, "orderno": orderno})


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


# ── 店补任务接口 ─────────────────────────────────────────────────────────────────
@sorting_bp.get('/sorting/replenishment/tasks')
def replenishment_tasks():
    """
    GET /sorting/replenishment/tasks?status=pending&date=YYYY-MM-DD&limit=200
    """
    status  = request.args.get('status', '').strip()
    date_s  = request.args.get('date', '').strip()
    limit   = request.args.get('limit', 200, type=int)
    conn    = _get_conn()
    clauses, params = [], []
    if status:
        clauses.append("status=?"); params.append(status)
    if date_s:
        clauses.append("date=?"); params.append(date_s[:10])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM store_replenishment_tasks {where} "
        f"ORDER BY created_at DESC LIMIT ?",
        params + [limit]
    ).fetchall()
    conn.close()
    return jsonify({"ok": True, "tasks": [dict(r) for r in rows], "total": len(rows)})


@sorting_bp.get('/sorting/replenishment/task/<task_no>')
def replenishment_task_detail(task_no):
    """
    GET /sorting/replenishment/task/<task_no>
    返回任务主表 + 明细列表。
    """
    conn = _get_conn()
    task = conn.execute(
        "SELECT * FROM store_replenishment_tasks WHERE task_no=?", (task_no,)
    ).fetchone()
    if not task:
        conn.close()
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    items = conn.execute(
        "SELECT * FROM store_replenishment_items WHERE task_no=? ORDER BY id",
        (task_no,)
    ).fetchall()
    conn.close()
    return jsonify({
        "ok":    True,
        "task":  dict(task),
        "items": [dict(r) for r in items],
    })


@sorting_bp.patch('/sorting/replenishment/task/<task_no>/status')
def replenishment_task_status(task_no):
    """
    PATCH /sorting/replenishment/task/<task_no>/status
    body: {"status": "packing"|"packed"|"dispatched"}
    """
    VALID = {'pending', 'packing', 'packed', 'dispatched'}
    body   = request.get_json() or {}
    status = (body.get('status') or '').strip()
    if status not in VALID:
        return jsonify({"ok": False, "error": f"status 须为 {VALID}"}), 400
    conn = _get_conn()
    conn.execute(
        "UPDATE store_replenishment_tasks SET status=?, updated_at=datetime('now','localtime') WHERE task_no=?",
        (status, task_no)
    )
    conn.close()
    return jsonify({"ok": True, "task_no": task_no, "status": status})


# 初始化表（模块导入时执行）
try:
    _ensure_tables()
except Exception as _e:
    logger.warning(f"[sorting] 初始化表失败: {_e}")
