"""
D2: JDY 建档 Flask 服务
端口: 5009

路由:
  GET  /register              → 返回建档手机页
  GET  /register/categories   → 分类列表（带 code_2d, letter）
  GET  /register/suppliers    → 供应商列表（带 transformed_code）
  POST /register/preview      → AI 识别 + 编码预览（不写入 JDY）
  POST /register/confirm      → 确认建档，写入 JDY
"""

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

from jdy_cache import JDYCache
from ai_helper import identify_product
from register_product import register_product
from code_gen import transform_vendor_code, peek_next_seq, generate_product_number

# ── 路径 ──────────────────────────────────────────────────────────────────────
_HERE      = os.path.dirname(os.path.abspath(__file__))
_CFG_PATH  = os.path.join(_HERE, '..', '..', 'ai_config.json')
_CAT_MAP   = os.path.join(_HERE, '..', 'category_code_map.json')
_SUP_MAP   = os.path.join(_HERE, 'supplier_category_map.json')
_TMPL_PATH = os.path.join(_HERE, '..', 'templates', 'register.html')

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

_cat_map: dict = {}     # JDY categoryId → {path, letter, code_2d}
_sup_map: dict = {}     # transformed_code → {sup_name, categories, ...}
_cache: JDYCache | None = None


def _init():
    global _cat_map, _sup_map, _cache
    _cat_map = _load_json(_CAT_MAP)
    _sup_map = _load_json(_SUP_MAP)
    try:
        _cache = JDYCache(cfg_path=_CFG_PATH)
        _cache.ensure_fresh(background=True)
        print('[REGISTER] 缓存刷新后台线程已启动')
    except Exception as e:
        print(f'[WARN] JDYCache 初始化失败: {e}')


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def _get_sup_categories(account: str, transformed: str) -> list[str]:
    """从 supplier_category_map 获取该供应商已配置的分类字母列表"""
    acc_map = _sup_map.get(account, {})
    entry = acc_map.get(transformed.upper(), {})
    return entry.get('categories', [])


def _categories_for_ui(account: str) -> list[dict]:
    """
    返回前端分类下拉列表：过滤掉 letter/code_2d 为空的条目。
    格式: [{id, path, letter, code_2d}, ...]
    """
    result = []
    for cat_id, info in _cat_map.items():
        letter = info.get('letter', '').strip()
        code_2d = info.get('code_2d', '').strip()
        if letter and code_2d:
            result.append({
                'id':      cat_id,
                'path':    info.get('path', ''),
                'letter':  letter,
                'code_2d': code_2d,
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
        "product_number": "C.A12629C-0001",
        "seq_next":       1,
        "ai_result": {name, spec, category_hint, cost_hint},
        "cat_info":  {letter, code_2d, path},
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
        cat_info = _cat_map.get(cat_id, {})
        cat_letter = cat_info.get('letter', '').strip()
        cat_code_2d = cat_info.get('code_2d', '').strip()
        if not cat_letter:
            return jsonify({'error': f'分类 {cat_id!r} 未配置 letter，请先填写 category_code_map.json'}), 400

        # ── 编码预览 ──
        seq_next = peek_next_seq(account, transformed, cat_letter)
        product_number = generate_product_number(cat_letter, transformed, seq_next)

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
            'cat_info':       {'letter': cat_letter, 'code_2d': cat_code_2d,
                               'path': cat_info.get('path', '')},
            'ai_result':      ai_result,
            'error':          None,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/register/confirm', methods=['POST'])
def api_confirm():
    """
    POST /register/confirm
    Body:
    {
        "account":       "account1",
        "tax_no":        "102629",
        "cat_id":        "xxx",
        "product_name":  "金属串珠手链",
        "spec":          "金色 约20cm",   // → JDY spec 字段
        "unit":          "个",
        "remark":        "...",
        "supplier_id":   "...",           // 写入 defaultSupplierId
    }
    返回:
    {
        "ok":             true,
        "product_number": "C.A12629C-0001",
        "ean13":          "2605210300018",
        "seq":            1,
        "attempts":       1,
        "error":          null
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

        cat_info = _cat_map.get(cat_id, {})
        cat_letter = cat_info.get('letter', '').strip()
        cat_code_2d = cat_info.get('code_2d', '').strip()
        if not cat_letter or not cat_code_2d:
            return jsonify({'ok': False, 'error': f'分类 {cat_id!r} 配置不完整'}), 400

        # 透传给 JDY product/add 的额外字段
        extra = {}
        for field in ('unit', 'spec', 'remark'):
            val = (data.get(field) or '').strip()
            if val:
                extra[field] = val
        extra['category_id'] = cat_id
        sup_id = (data.get('supplier_id') or '').strip()
        if sup_id:
            extra['supplier_id'] = sup_id

        result = register_product(
            account=account,
            tax_no=tax_no,
            cat_letter=cat_letter,
            cat_code_2d=cat_code_2d,
            product_name=product_name,
            cfg_path=_CFG_PATH,
            **extra,
        )

        return jsonify(result)

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
