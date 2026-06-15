"""
D2: JDY 建档 Flask 服务
端口: 5009

路由:
  GET  /register              → 返回建档手机页
  GET  /register/health       → 健康检查
  GET  /register/categories   → 分类列表（带 prefix, small_code, code_2d）
  GET  /register/suppliers    → 供应商列表（带 transformed_code）
  GET  /register/units        → 单位列表
  GET  /register/price_history → 历史采购价
  POST /register/preview      → AI 识别 + 编码预览（不写入 JDY）
  POST /register/confirm      → 确认单品建档，写入 JDY
  POST /register/order/parse  → 订单照片 AI 解析
  POST /register/order/confirm → 批量建档 + 生成购货订单
"""

import json
import os
import sqlite3
import sys
import traceback
import threading
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify
from flask_cors import CORS

from jdy_cache import JDYCache
from ai_helper import identify_product, parse_order_image
from register_product import register_product
from code_gen import (transform_vendor_code, peek_next_seq, generate_product_number,
                      get_next_seq, rollback_seq, generate_ean13)
from unit_manager import UnitManager

# ── 路径 ──────────────────────────────────────────────────────────────────────
_HERE      = os.path.dirname(os.path.abspath(__file__))
_CFG_PATH  = os.path.join(_HERE, '..', '..', 'ai_config.json')
_CAT_MAP   = os.path.join(_HERE, '..', 'category_code_map.json')
_SUP_MAP   = os.path.join(_HERE, 'supplier_category_map.json')
_TMPL_PATH = os.path.join(_HERE, '..', 'templates', 'register.html')
_SALES_DB  = os.path.join(_HERE, '..', '_sales_cache', 'sales_cache.sqlite3')

PORT = 5009

# ── Flask 初始化 ───────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

# ── 数据加载（启动时一次性加载） ──────────────────────────────────────────────
def _load_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'[WARN] 加载 {path} 失败: {e}')
        return {}

_cat_map: dict = {}
_sup_map: dict = {}
_cache: JDYCache | None = None
_unit_mgr: UnitManager | None = None


def _init():
    global _cat_map, _sup_map, _cache, _unit_mgr
    _cat_map = _load_json(_CAT_MAP)
    _sup_map = _load_json(_SUP_MAP)
    try:
        _cache = JDYCache(cfg_path=_CFG_PATH)
        _cache.ensure_fresh(background=True)
        _unit_mgr = UnitManager(_cache, cfg_path=_CFG_PATH)
        print('[REGISTER] 缓存刷新后台线程已启动')
    except Exception as e:
        print(f'[WARN] JDYCache 初始化失败: {e}')


# ── 价格历史数据库 ────────────────────────────────────────────────────────────

def _price_db_conn():
    """连接 sales_cache.sqlite3，确保 purchase_order_prices 表存在"""
    os.makedirs(os.path.dirname(_SALES_DB), exist_ok=True)
    conn = sqlite3.connect(_SALES_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS purchase_order_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            order_number TEXT NOT NULL,
            order_date TEXT NOT NULL,
            supplier_name TEXT,
            supplier_number TEXT,
            product_number TEXT NOT NULL,
            price REAL NOT NULL,
            qty REAL DEFAULT 0,
            unit TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_pop_product
        ON purchase_order_prices(account, product_number)
    ''')
    return conn


def _write_price_history(account: str, order_number: str, order_date: str,
                          supplier_name: str, supplier_number: str,
                          entries: list[dict]):
    """批量写入价格历史（订单确认后调用）"""
    try:
        conn = _price_db_conn()
        now = datetime.now().isoformat()
        rows = []
        for e in entries:
            pno = e.get('product_number') or e.get('productNumber', '')
            if not pno:
                continue
            rows.append((account, order_number, order_date,
                         supplier_name, supplier_number,
                         pno, float(e.get('price', 0)),
                         float(e.get('qty', 0)),
                         str(e.get('unit', '')), now))
        conn.executemany('''
            INSERT INTO purchase_order_prices
              (account, order_number, order_date, supplier_name, supplier_number,
               product_number, price, qty, unit, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', rows)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[WARN] 写入价格历史失败: {e}')


def _draft_db_conn():
    """连接 sales_cache.sqlite3，确保 product_drafts 表存在"""
    os.makedirs(os.path.dirname(_SALES_DB), exist_ok=True)
    conn = sqlite3.connect(_SALES_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS product_drafts (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            account                 TEXT NOT NULL,
            tax_no                  TEXT NOT NULL,
            supplier_id             TEXT DEFAULT '',
            supplier_name           TEXT DEFAULT '',
            supplier_jdy_number     TEXT DEFAULT '',
            cat_id                  TEXT NOT NULL,
            cat_prefix              TEXT NOT NULL,
            cat_small_code          TEXT NOT NULL,
            cat_code_2d             TEXT NOT NULL,
            product_name            TEXT NOT NULL,
            spec                    TEXT DEFAULT '',
            unit                    TEXT DEFAULT '',
            remark                  TEXT DEFAULT '',
            ka_barcode              TEXT DEFAULT '',
            batch_group             TEXT DEFAULT '',
            qty                     REAL DEFAULT 0,
            price                   REAL DEFAULT 0,
            ctn_qty                 INTEGER DEFAULT 0,
            pcs_per_ctn             INTEGER DEFAULT 0,
            unit_id                 TEXT DEFAULT '',
            image_b64               TEXT DEFAULT '',
            existing_product_number TEXT DEFAULT '',
            product_number          TEXT DEFAULT '',
            ean13                   TEXT DEFAULT '',
            seq                     INTEGER DEFAULT 0,
            transformed             TEXT DEFAULT '',
            status                  TEXT NOT NULL DEFAULT 'pending',
            jdy_id                  TEXT DEFAULT '',
            error_msg               TEXT DEFAULT '',
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_drafts_account_status
        ON product_drafts(account, status)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_drafts_batch
        ON product_drafts(batch_group)
    ''')
    return conn


def _generate_codes_for_draft(account: str, tax_no: str, prefix: str,
                               small_code: str, cat_code_2d: str):
    """
    仅生成编码（不调 JDY）。
    返回 (codes_dict, error_str)，codes_dict 为 None 时表示失败。
    """
    transformed, err = transform_vendor_code(tax_no)
    if err:
        return None, f'档口号变换失败: {err}'

    seq = get_next_seq(account)
    product_number = generate_product_number(prefix, transformed, small_code, seq)

    try:
        ean13 = generate_ean13(cat_code_2d, seq)
    except Exception as e:
        rollback_seq(account)
        return None, f'EAN-13 生成失败: {e}'

    return {
        'transformed':    transformed,
        'seq':            seq,
        'product_number': product_number,
        'ean13':          ean13,
    }, None


def _get_price_history(account: str, product_number: str, limit: int = 5) -> list:
    """查询产品历史采购价"""
    try:
        conn = _price_db_conn()
        rows = conn.execute('''
            SELECT order_date, price, qty, order_number, supplier_name
            FROM purchase_order_prices
            WHERE account = ? AND product_number = ?
            ORDER BY order_date DESC, id DESC LIMIT ?
        ''', (account, product_number, limit)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f'[WARN] 查询价格历史失败: {e}')
        return []


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def _get_sup_categories(account: str, transformed: str) -> list[str]:
    """从 supplier_category_map 获取该供应商已配置的分类字母列表"""
    acc_map = _sup_map.get(account, {})
    entry = acc_map.get(transformed.upper(), {})
    return entry.get('categories', [])


def _get_cat_info(cat_id: str) -> dict:
    """
    读取分类配置，兼容新旧字段名：
      新: prefix / small_code / code_2d
      旧: letter / code_2d
    返回: {prefix, small_code, code_2d, path}
    """
    info = _cat_map.get(str(cat_id), {})
    prefix = (info.get('prefix') or info.get('letter') or '').strip()
    small_code = (info.get('small_code') or info.get('code_2d') or '').strip()
    code_2d = (info.get('code_2d') or small_code or '').strip()
    return {
        'prefix':     prefix,
        'small_code': small_code,
        'code_2d':    code_2d,
        'path':       info.get('path', ''),
    }


def _categories_for_ui(account: str) -> list[dict]:
    """
    返回前端分类下拉列表：过滤掉 prefix/code_2d 为空的条目。
    格式: [{id, path, prefix, small_code, code_2d}, ...]
    """
    result = []
    for cat_id, info in _cat_map.items():
        if cat_id == '_note':
            continue
        # 兼容旧字段
        prefix = (info.get('prefix') or info.get('letter') or '').strip()
        small_code = (info.get('small_code') or info.get('code_2d') or '').strip()
        code_2d = (info.get('code_2d') or small_code or '').strip()
        if prefix and code_2d:
            result.append({
                'id':         cat_id,
                'path':       info.get('path', ''),
                'prefix':     prefix,
                'small_code': small_code,
                'code_2d':    code_2d,
                # 向后兼容字段
                'letter':     prefix,
            })
    result.sort(key=lambda x: x['path'])
    return result


def _suppliers_for_ui(account: str) -> list[dict]:
    """
    返回前端供应商下拉列表（过滤无 taxPayerNo 的）。
    格式: [{id, name, number, tax_no, transformed, categories}, ...]
    """
    if not _cache:
        return []
    sups = _cache.get_suppliers(account)
    result = []
    for s in sups:
        tax_no = (s.get('taxPayerNo') or '').strip()
        if not tax_no:
            continue
        transformed, err = transform_vendor_code(tax_no)
        cats = _get_sup_categories(account, transformed)
        result.append({
            'id':          str(s.get('id', '')),
            'name':        s.get('name', ''),
            'number':      s.get('number', ''),
            'tax_no':      tax_no,
            'transformed': transformed,
            'categories':  cats,
            'error':       err or '',
        })
    result.sort(key=lambda x: x['name'])
    return result


def _fuzzy_match_supplier(account: str, name_hint: str) -> dict | None:
    """按名称关键词模糊匹配供应商"""
    if not name_hint or not _cache:
        return None
    sups = _cache.get_suppliers(account)
    hint_lower = name_hint.lower()
    best = None
    best_score = 0
    for s in sups:
        s_name = (s.get('name') or '').lower()
        # 计算共同字符数量作为简单相似度
        score = sum(1 for c in hint_lower if c in s_name)
        if score > best_score and score >= 2:
            best_score = score
            best = s
    if not best:
        return None
    tax_no = (best.get('taxPayerNo') or '').strip()
    transformed, err = transform_vendor_code(tax_no) if tax_no else ('', '无taxPayerNo')
    cats = _get_sup_categories(account, transformed) if transformed else []
    return {
        'id':          str(best.get('id', '')),
        'name':        best.get('name', ''),
        'number':      best.get('number', ''),
        'tax_no':      tax_no,
        'transformed': transformed,
        'categories':  cats,
        'error':       err or '',
    }


# ── 路由 ──────────────────────────────────────────────────────────────────────

@app.route('/register/health')
def health():
    return jsonify({'status': 'ok', 'port': PORT})


@app.route('/register')
def register_page():
    """返回手机建档 HTML 页"""
    try:
        with open(_TMPL_PATH, encoding='utf-8') as f:
            html = f.read()
        return html
    except FileNotFoundError:
        return '<h3>templates/register.html 尚未创建</h3>', 404


@app.route('/register/categories')
def api_categories():
    """GET /register/categories?account=account1"""
    account = request.args.get('account', 'account1')
    cats = _categories_for_ui(account)
    return jsonify({'categories': cats, 'total': len(cats)})


@app.route('/register/suppliers')
def api_suppliers():
    """GET /register/suppliers?account=account1"""
    account = request.args.get('account', 'account1')
    sups = _suppliers_for_ui(account)
    return jsonify({'suppliers': sups, 'total': len(sups)})


@app.route('/register/units')
def api_units():
    """
    GET /register/units?account=account1
    从产品缓存中提取所有已用单位，去重排序。
    若 unit_manager 已初始化则从缓存取，否则从产品列表扫描。
    返回: [{unitId, unitName}, ...]
    """
    account = request.args.get('account', 'account1')
    units = []

    if _unit_mgr:
        # 优先从 unit_manager 缓存取
        cached = _unit_mgr.get_all_units(account)
        if cached:
            units = cached

    if not units and _cache:
        # 从产品缓存扫描 unit 字段
        products = _cache.get_products(account)
        seen = {}
        for p in products:
            uid = str(p.get('unitId') or p.get('unit_id') or '')
            uname = str(p.get('unit') or p.get('unitName') or '')
            if uid and uname and uid not in seen:
                seen[uid] = uname
        units = [{'unitId': k, 'unitName': v} for k, v in seen.items()]
        units.sort(key=lambda x: x['unitName'])

    # 始终确保有 PAC 基础单位选项
    pac_in_list = any(u['unitName'].upper() in ('PAC', 'PACK') for u in units)
    if not pac_in_list:
        pac_id = _unit_mgr.get_pcs_unit_id(account) if _unit_mgr else ''
        if pac_id:
            units.insert(0, {'unitId': pac_id, 'unitName': 'PAC'})

    return jsonify({'units': units, 'total': len(units)})


@app.route('/register/price_history')
def api_price_history():
    """GET /register/price_history?account=account1&product_number=C.A1262902-0001"""
    account = request.args.get('account', 'account1')
    pno = request.args.get('product_number', '').strip()
    if not pno:
        return jsonify({'history': []})
    history = _get_price_history(account, pno, limit=5)
    return jsonify({'history': history})


@app.route('/register/preview', methods=['POST'])
def api_preview():
    """
    POST /register/preview
    Body:
    {
        "account":      "account1",
        "supplier_id":  "123456",   // 或 tax_no
        "tax_no":       "102629",   // 供应商 taxPayerNo（与 supplier_id 二选一）
        "cat_id":       "xxx",      // JDY 分类 ID
        "image_b64":    "...",      // 图片（与 image_url 二选一）
        "image_url":    "..."
    }
    返回:
    {
        "transformed":    "A12629",
        "product_number": "C.A1262902-0001",
        "seq_next":       1,
        "ai_result": {name, spec, category_hint, cost_hint},
        "cat_info":  {prefix, small_code, code_2d, path},
        "error":     null
    }
    """
    try:
        data = request.get_json(force=True) or {}
        account   = data.get('account', 'account1')
        image_b64 = data.get('image_b64', '')
        image_url = data.get('image_url', '')
        cat_id    = str(data.get('cat_id', '') or '')

        # ── 供应商信息 ──
        tax_no = (data.get('tax_no') or '').strip()
        if not tax_no:
            sup_id = str(data.get('supplier_id', '') or '')
            if not sup_id or not _cache:
                return jsonify({'error': '需要提供 tax_no 或 supplier_id'}), 400
            sup_map = _cache.get_supplier_map(account)
            sup = sup_map.get(sup_id)
            if not sup:
                return jsonify({'error': f'找不到供应商 ID={sup_id}'}), 400
            tax_no = (sup.get('taxPayerNo') or '').strip()
            if not tax_no:
                return jsonify({'error': '该供应商无 taxPayerNo，无法生成档口号编码'}), 400

        transformed, err = transform_vendor_code(tax_no)
        if err:
            return jsonify({'error': f'档口号变换失败: {err}', 'transformed': transformed}), 400

        # ── 分类信息 ──
        cat_info = _get_cat_info(cat_id)
        if not cat_info['prefix']:
            return jsonify({'error': f'分类 {cat_id!r} 未配置 prefix/letter，请先填写 category_code_map.json'}), 400

        # ── 编码预览 ──
        seq_next = peek_next_seq(account)
        product_number = generate_product_number(
            cat_info['prefix'], transformed, cat_info['small_code'], seq_next)

        # ── AI 识别 ──
        sup_cats = _get_sup_categories(account, transformed)
        ai_result = {}
        if image_b64 or image_url:
            ai_result = identify_product(
                image_b64=image_b64 or None,
                image_url=image_url or None,
                supplier_categories=sup_cats or None,
                cfg_path=_CFG_PATH,
            )

        return jsonify({
            'transformed':    transformed,
            'product_number': product_number,
            'seq_next':       seq_next,
            'cat_info':       cat_info,
            'ai_result':      ai_result,
            'error':          None,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/register/confirm', methods=['POST'])
def api_confirm():
    """
    POST /register/confirm（单品建档 → 保存到草稿箱，不直接写 JDY）
    Body:
    {
        "account":       "account1",
        "tax_no":        "102629",
        "cat_id":        "xxx",
        "product_name":  "金属串珠手链",
        "spec":          "金色 约20cm",
        "unit":          "个",
        "remark":        "...",
        "supplier_id":   "...",
    }
    返回:
    {
        "ok": true, "draft_id": 1, "product_number": "C.A1262902-0001",
        "ean13": "...", "seq": 1, "error": null
    }
    """
    try:
        data = request.get_json(force=True) or {}
        account      = data.get('account', 'account1')
        tax_no       = (data.get('tax_no') or '').strip()
        cat_id       = str(data.get('cat_id', '') or '')
        product_name = (data.get('product_name') or '').strip()

        if not tax_no:
            return jsonify({'ok': False, 'error': '缺少 tax_no'}), 400
        if not product_name:
            return jsonify({'ok': False, 'error': '缺少商品名称'}), 400

        cat_info = _get_cat_info(cat_id)
        if not cat_info['prefix'] or not cat_info['code_2d']:
            return jsonify({'ok': False, 'error': f'分类 {cat_id!r} 配置不完整'}), 400

        # 生成编码（不调 JDY）
        codes, err = _generate_codes_for_draft(
            account, tax_no,
            cat_info['prefix'], cat_info['small_code'], cat_info['code_2d']
        )
        if err:
            return jsonify({'ok': False, 'error': err}), 400

        spec        = (data.get('spec') or '').strip()
        unit        = (data.get('unit') or '').strip()
        remark      = (data.get('remark') or '').strip()
        supplier_id = (data.get('supplier_id') or '').strip()

        now = datetime.now().isoformat()
        conn = _draft_db_conn()
        cur = conn.execute('''
            INSERT INTO product_drafts
              (account, tax_no, supplier_id, cat_id, cat_prefix, cat_small_code, cat_code_2d,
               product_name, spec, unit, remark, batch_group,
               product_number, ean13, seq, transformed, status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)
        ''', (account, tax_no, supplier_id, cat_id,
              cat_info['prefix'], cat_info['small_code'], cat_info['code_2d'],
              product_name, spec, unit, remark, '',
              codes['product_number'], codes['ean13'], codes['seq'], codes['transformed'],
              now, now))
        conn.commit()
        draft_id = cur.lastrowid
        conn.close()

        return jsonify({
            'ok':             True,
            'draft_id':       draft_id,
            'product_number': codes['product_number'],
            'ean13':          codes['ean13'],
            'seq':            codes['seq'],
            'error':          None,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/register/order/parse', methods=['POST'])
def api_order_parse():
    """
    POST /register/order/parse
    Body: {account, supplier_id(可选), image_b64}
    返回: {
        ai_supplier_name, supplier_matched,
        products: [{line, name, spec, category_hint, cat_id, qty, price,
                    ctn_qty, pcs_per_ctn, preview_number}]
    }
    """
    try:
        data = request.get_json(force=True) or {}
        account    = data.get('account', 'account1')
        image_b64  = data.get('image_b64', '')
        image_url  = data.get('image_url', '')
        supplier_id = str(data.get('supplier_id', '') or '')

        # ── 供应商分类提示 ──
        sup_cats = []
        if supplier_id and _cache:
            sup_map = _cache.get_supplier_map(account)
            sup = sup_map.get(supplier_id)
            if sup:
                tax_no = (sup.get('taxPayerNo') or '').strip()
                if tax_no:
                    transformed, _ = transform_vendor_code(tax_no)
                    sup_cats = _get_sup_categories(account, transformed)

        # ── AI 解析 ──
        ai_result = parse_order_image(
            image_b64=image_b64 or None,
            image_url=image_url or None,
            supplier_categories=sup_cats or None,
            cfg_path=_CFG_PATH,
        )

        if ai_result.get('error'):
            return jsonify({'error': ai_result['error']}), 400

        ai_supplier_name = ai_result.get('supplier_name', '')

        # ── 模糊匹配供应商 ──
        supplier_matched = None
        if supplier_id and _cache:
            sup_map = _cache.get_supplier_map(account)
            sup = sup_map.get(supplier_id)
            if sup:
                tax_no = (sup.get('taxPayerNo') or '').strip()
                transformed, err = transform_vendor_code(tax_no) if tax_no else ('', '无taxPayerNo')
                supplier_matched = {
                    'id':          str(sup.get('id', '')),
                    'name':        sup.get('name', ''),
                    'number':      sup.get('number', ''),
                    'tax_no':      tax_no,
                    'transformed': transformed,
                    'error':       err or '',
                }
        elif ai_supplier_name:
            supplier_matched = _fuzzy_match_supplier(account, ai_supplier_name)

        # ── 枚举分类 map（按 prefix 建索引） ──
        prefix_to_cats: dict[str, list] = {}
        for cat_id, info in _cat_map.items():
            if cat_id == '_note':
                continue
            prefix = (info.get('prefix') or info.get('letter') or '').strip().upper()
            if prefix:
                prefix_to_cats.setdefault(prefix, []).append(cat_id)

        # ── 增强产品行 ──
        seq_preview = peek_next_seq(account)
        products = []
        for i, item in enumerate(ai_result.get('products') or []):
            hint = (item.get('category_hint') or '').upper()
            cat_id = ''
            preview_number = ''
            cat_info_item = {}

            # 按 category_hint 匹配第一个分类
            for cid in prefix_to_cats.get(hint, []):
                cat_info_item = _get_cat_info(cid)
                if cat_info_item['prefix']:
                    cat_id = cid
                    break

            if cat_id and supplier_matched:
                tax_no = supplier_matched.get('tax_no', '')
                if tax_no:
                    transformed, _ = transform_vendor_code(tax_no)
                    preview_number = generate_product_number(
                        cat_info_item['prefix'], transformed,
                        cat_info_item['small_code'], seq_preview + i)

            products.append({
                **item,
                'cat_id':         cat_id,
                'cat_info':       cat_info_item,
                'preview_number': preview_number,
            })

        return jsonify({
            'ai_supplier_name': ai_supplier_name,
            'supplier_matched': supplier_matched,
            'products':         products,
            'error':            None,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/register/order/confirm', methods=['POST'])
def api_order_confirm():
    """
    POST /register/order/confirm（批量建档 → 保存到草稿箱，不直接写 JDY）
    Body:
    {
        account, supplier_jdy_number, supplier_id,
        supplier_name, tax_no,
        lines: [
            {name, spec, unit_id, cat_id, qty, price,
             ctn_qty, pcs_per_ctn, remark,
             existing_product_number,   # 非空=已有商品
             image_b64}
        ]
    }
    返回:
    {
        ok, batch_group, total, pending_count, existing_count,
        drafts:[{line, product_number, ean13, status}]
    }
    """
    try:
        data = request.get_json(force=True) or {}
        account          = data.get('account', 'account1')
        supplier_jdy_no  = (data.get('supplier_jdy_number') or '').strip()
        supplier_id      = (data.get('supplier_id') or '').strip()
        supplier_name    = (data.get('supplier_name') or '').strip()
        tax_no           = (data.get('tax_no') or '').strip()
        lines            = data.get('lines') or []

        if not supplier_jdy_no:
            return jsonify({'error': '缺少 supplier_jdy_number'}), 400
        if not lines:
            return jsonify({'error': '没有产品行'}), 400

        batch_group = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        conn = _draft_db_conn()
        drafts_result = []
        pending_count = 0
        existing_count = 0

        for i, line in enumerate(lines):
            existing_pno = (line.get('existing_product_number') or '').strip()
            line_num  = line.get('line', i + 1)
            cat_id    = str(line.get('cat_id') or '')
            qty       = float(line.get('qty') or 0)
            price     = float(line.get('price') or 0)
            unit_id   = str(line.get('unit_id') or '')
            ctn_qty   = int(line.get('ctn_qty') or 0)
            pcs_ctn   = int(line.get('pcs_per_ctn') or 0)
            image_b64 = (line.get('image_b64') or '').strip()
            spec      = str(line.get('spec') or '').strip()
            remark    = str(line.get('remark') or '').strip()
            if not remark and pcs_ctn > 0:
                remark = str(pcs_ctn)
            product_name = (line.get('name') or '').strip()

            pac_id = _unit_mgr.get_pcs_unit_id(account) if _unit_mgr else ''
            cat_info = _get_cat_info(cat_id) if cat_id else {}

            if existing_pno:
                # ── 已有商品 ──
                conn.execute('''
                    INSERT INTO product_drafts
                      (account, tax_no, supplier_id, supplier_name, supplier_jdy_number,
                       cat_id, cat_prefix, cat_small_code, cat_code_2d,
                       product_name, spec, unit, remark, batch_group,
                       qty, price, ctn_qty, pcs_per_ctn, unit_id, image_b64,
                       existing_product_number, product_number,
                       status, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'existing',?,?)
                ''', (account, tax_no, supplier_id, supplier_name, supplier_jdy_no,
                      cat_id, cat_info.get('prefix', ''), cat_info.get('small_code', ''),
                      cat_info.get('code_2d', ''),
                      product_name or existing_pno, spec, pac_id or unit_id, remark, batch_group,
                      qty, price, ctn_qty, pcs_ctn, unit_id, image_b64,
                      existing_pno, existing_pno,
                      now, now))
                conn.commit()
                drafts_result.append({'line': line_num, 'product_number': existing_pno,
                                       'ean13': '', 'status': 'existing'})
                existing_count += 1
                continue

            # ── 新品 ──
            if not cat_info.get('prefix'):
                drafts_result.append({'line': line_num, 'product_number': '',
                                       'ean13': '', 'status': 'error',
                                       'error': f'分类 {cat_id!r} 未配置'})
                continue
            if not tax_no:
                drafts_result.append({'line': line_num, 'product_number': '',
                                       'ean13': '', 'status': 'error',
                                       'error': '未绑定供应商 taxPayerNo'})
                continue

            codes, err = _generate_codes_for_draft(
                account, tax_no,
                cat_info['prefix'], cat_info['small_code'], cat_info['code_2d']
            )
            if err:
                drafts_result.append({'line': line_num, 'product_number': '',
                                       'ean13': '', 'status': 'error', 'error': err})
                continue

            cur = conn.execute('''
                INSERT INTO product_drafts
                  (account, tax_no, supplier_id, supplier_name, supplier_jdy_number,
                   cat_id, cat_prefix, cat_small_code, cat_code_2d,
                   product_name, spec, unit, remark, batch_group,
                   qty, price, ctn_qty, pcs_per_ctn, unit_id, image_b64,
                   product_number, ean13, seq, transformed,
                   status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)
            ''', (account, tax_no, supplier_id, supplier_name, supplier_jdy_no,
                  cat_id, cat_info['prefix'], cat_info['small_code'], cat_info['code_2d'],
                  product_name, spec, pac_id or unit_id, remark, batch_group,
                  qty, price, ctn_qty, pcs_ctn, unit_id, image_b64,
                  codes['product_number'], codes['ean13'], codes['seq'], codes['transformed'],
                  now, now))
            conn.commit()
            drafts_result.append({'line': line_num, 'draft_id': cur.lastrowid,
                                   'product_number': codes['product_number'],
                                   'ean13': codes['ean13'], 'status': 'pending'})
            pending_count += 1

        conn.close()

        return jsonify({
            'ok':             True,
            'batch_group':    batch_group,
            'total':          len(lines),
            'pending_count':  pending_count,
            'existing_count': existing_count,
            'drafts':         drafts_result,
            'error':          None,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── 草稿箱路由 ────────────────────────────────────────────────────────────────

def _submit_one_draft(conn, draft: dict) -> dict:
    """
    提交单条草稿到 JDY，支持 46001 自动重试（最多3次）。
    直接修改 DB；返回 {ok, product_number, ean13, jdy_id, error}
    """
    if not _cache:
        return {'ok': False, 'error': '缓存未初始化'}

    draft_id = draft['id']
    account  = draft['account']
    current_pno = draft['product_number']
    current_ean = draft['ean13']

    for attempt in range(1, 4):
        if attempt > 1:
            # 46001 重复：重新生成编码
            codes, err = _generate_codes_for_draft(
                account, draft['tax_no'],
                draft['cat_prefix'], draft['cat_small_code'], draft['cat_code_2d']
            )
            if err:
                conn.execute(
                    'UPDATE product_drafts SET status=?,error_msg=?,updated_at=? WHERE id=?',
                    ('error', err, datetime.now().isoformat(), draft_id))
                conn.commit()
                return {'ok': False, 'error': err}
            current_pno = codes['product_number']
            current_ean = codes['ean13']
            conn.execute('''
                UPDATE product_drafts
                SET product_number=?,ean13=?,seq=?,transformed=?,updated_at=?
                WHERE id=?
            ''', (current_pno, current_ean, codes['seq'],
                  codes['transformed'], datetime.now().isoformat(), draft_id))
            conn.commit()

        payload = {
            'productNumber': current_pno,
            'productName':   draft['product_name'],
            'barcode':       current_ean,
        }
        if draft.get('cat_id'):
            payload['categoryId'] = str(draft['cat_id'])
        for field in ('spec', 'remark', 'unit'):
            val = (draft.get(field) or '').strip()
            if val:
                payload[field] = val
        if draft.get('supplier_id'):
            payload['defaultSupplierId'] = str(draft['supplier_id'])

        result = _cache.create_product(account, payload)

        if result['ok']:
            jdy_data = result.get('data') or {}
            jdy_id = None
            if isinstance(jdy_data, dict):
                jdy_id = (jdy_data.get('id') or
                          (jdy_data.get('items', [{}]) or [{}])[0].get('id'))
            jdy_id_str = str(jdy_id) if jdy_id else ''
            conn.execute('''
                UPDATE product_drafts
                SET status='done',jdy_id=?,product_number=?,ean13=?,error_msg='',updated_at=?
                WHERE id=?
            ''', (jdy_id_str, current_pno, current_ean,
                  datetime.now().isoformat(), draft_id))
            conn.commit()

            # 异步上传图片
            img = draft.get('image_b64') or ''
            if img and jdy_id_str:
                def _up(acc, pid, pnum, b64):
                    try:
                        _cache.update_product_image(acc, pid, pnum, b64)
                    except Exception as ex:
                        print(f'[WARN] 图片上传失败 {pnum}: {ex}')
                threading.Thread(target=_up,
                                  args=(account, jdy_id_str, current_pno, img),
                                  daemon=True).start()

            return {'ok': True, 'product_number': current_pno,
                    'ean13': current_ean, 'jdy_id': jdy_id_str, 'error': None}

        errcode   = result.get('errcode')
        last_error = result.get('msg', '未知错误')

        if errcode == 46001:
            print(f'[DRAFT] 编号重复({current_pno})，重试 #{attempt}...')
            continue

        # 其他错误：回滚序号，终止
        rollback_seq(account)
        err_msg = f'JDY错误 {errcode}: {last_error}'
        conn.execute(
            'UPDATE product_drafts SET status=?,error_msg=?,updated_at=? WHERE id=?',
            ('error', err_msg, datetime.now().isoformat(), draft_id))
        conn.commit()
        return {'ok': False, 'error': err_msg}

    # 超过最大重试次数
    rollback_seq(account)
    err_msg = '编号重复，重试3次失败'
    conn.execute(
        'UPDATE product_drafts SET status=?,error_msg=?,updated_at=? WHERE id=?',
        ('error', err_msg, datetime.now().isoformat(), draft_id))
    conn.commit()
    return {'ok': False, 'error': err_msg}


@app.route('/register/draft/<int:draft_id>/submit', methods=['POST'])
def api_submit_draft(draft_id):
    """POST /register/draft/<draft_id>/submit  单条提交"""
    try:
        conn = _draft_db_conn()
        row = conn.execute(
            'SELECT * FROM product_drafts WHERE id=?', (draft_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'ok': False, 'error': '草稿不存在'}), 404

        draft = dict(row)
        if draft['status'] not in ('pending', 'error'):
            conn.close()
            return jsonify({'ok': False, 'error': f'草稿状态 {draft["status"]} 不可提交'}), 400

        ret = _submit_one_draft(conn, draft)
        conn.close()
        return jsonify(ret)

    except Exception as e:
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/register/batch/<batch_group>/submit', methods=['POST'])
def api_submit_batch(batch_group):
    """POST /register/batch/<batch_group>/submit  批次整体提交"""
    try:
        conn = _draft_db_conn()
        pending_rows = conn.execute('''
            SELECT * FROM product_drafts
            WHERE batch_group=? AND status IN ('pending','error')
        ''', (batch_group,)).fetchall()

        results = []
        for row in pending_rows:
            draft = dict(row)
            ret = _submit_one_draft(conn, draft)
            results.append({
                'draft_id':       draft['id'],
                'product_number': ret.get('product_number', draft['product_number']),
                'status':         'done' if ret['ok'] else 'error',
                'error':          ret.get('error'),
            })

        # 生成购货订单（done + existing）
        po_number = ''
        po_error  = None
        all_ready = conn.execute('''
            SELECT * FROM product_drafts
            WHERE batch_group=? AND status IN ('done','existing')
        ''', (batch_group,)).fetchall()

        if all_ready and _cache:
            first = dict(all_ready[0])
            account         = first['account']
            supplier_jdy_no = first['supplier_jdy_number']
            supplier_name   = first['supplier_name']
            today = datetime.now().strftime('%Y-%m-%d')

            order_entries = []
            for r in all_ready:
                rd  = dict(r)
                pac = _unit_mgr.get_pcs_unit_id(account) if _unit_mgr else ''
                unit = rd.get('unit_id') or rd.get('unit') or pac or ''
                qty = rd['ctn_qty'] if rd.get('ctn_qty', 0) > 0 else rd.get('qty', 0)
                order_entries.append({
                    'product_number': rd['product_number'],
                    'productNumber':  rd['product_number'],
                    'qty':   qty,
                    'price': rd.get('price', 0),
                    'unit':  unit,
                    'location': '金华仓库',
                })

            if supplier_jdy_no and order_entries:
                po_result = _cache.create_purchase_order(account, {
                    'supplier_number': supplier_jdy_no,
                    'date': today,
                    'entries': [{
                        'productNumber': e['productNumber'],
                        'qty':           e['qty'],
                        'price':         e['price'],
                        'unit':          e['unit'],
                        'location':      e['location'],
                    } for e in order_entries],
                })
                if po_result.get('ok'):
                    po_number = po_result.get('order_number', '')
                    _write_price_history(account, po_number, today,
                                         supplier_name, supplier_jdy_no, order_entries)
                    for e in order_entries:
                        if e.get('price', 0) > 0:
                            def _uprice(acc, pnum, p):
                                try:
                                    _cache.update_product_price(acc, pnum, p)
                                except Exception as ex:
                                    print(f'[WARN] 价格回写失败 {pnum}: {ex}')
                            threading.Thread(target=_uprice,
                                              args=(account, e['product_number'], e['price']),
                                              daemon=True).start()
                else:
                    po_error = po_result.get('msg', '购货订单创建失败')

        conn.close()
        return jsonify({
            'ok':                    True,
            'results':               results,
            'purchase_order_number': po_number,
            'purchase_order_error':  po_error,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/register/drafts')
def api_get_drafts():
    """GET /register/drafts?account=account1&status=pending（默认返回 pending+error）"""
    try:
        account       = request.args.get('account', 'account1')
        status_filter = request.args.get('status', '').strip()

        conn = _draft_db_conn()
        if status_filter:
            rows = conn.execute('''
                SELECT id, product_name, product_number, ean13, supplier_name,
                       status, batch_group, error_msg, created_at, seq
                FROM product_drafts
                WHERE account=? AND status=?
                ORDER BY created_at DESC
            ''', (account, status_filter)).fetchall()
        else:
            rows = conn.execute('''
                SELECT id, product_name, product_number, ean13, supplier_name,
                       status, batch_group, error_msg, created_at, seq
                FROM product_drafts
                WHERE account=? AND status IN ('pending','error')
                ORDER BY created_at DESC
            ''', (account,)).fetchall()
        conn.close()

        drafts = [dict(r) for r in rows]
        return jsonify({'drafts': drafts, 'total': len(drafts)})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/register/drafts/<int:draft_id>', methods=['DELETE'])
def api_delete_draft(draft_id):
    """DELETE /register/drafts/<draft_id>  仅 pending/error 可删"""
    try:
        conn = _draft_db_conn()
        row = conn.execute(
            'SELECT * FROM product_drafts WHERE id=?', (draft_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'ok': False, 'error': '草稿不存在'}), 404

        draft = dict(row)
        if draft['status'] not in ('pending', 'error'):
            conn.close()
            return jsonify({'ok': False,
                            'error': f'已提交的草稿无法删除（status={draft["status"]}）'}), 400

        # 回滚序号
        if draft.get('seq', 0) > 0:
            rollback_seq(draft['account'])

        conn.execute('DELETE FROM product_drafts WHERE id=?', (draft_id,))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── 启动 ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    _init()
    print(f'\n[REGISTER] 建档服务启动，端口 {PORT}')
    print(f'[REGISTER] 手机访问: http://本机IP:{PORT}/register\n')
    try:
        from waitress import serve
        serve(app, host='0.0.0.0', port=PORT, threads=4)
    except ImportError:
        app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
