"""
D1: JDY 建档 AI 识别辅助
- 接收图片 base64（或 URL）
- 调用千问视觉 API（qianwen_api_key 已在 ai_config.json 中配置）
- 返回: {name, spec, category_hint, color, cost_hint, raw_text}

调用示例:
    from ai_helper import identify_product
    result = identify_product(image_b64="...", supplier_categories=["A","B","C"])
"""

import json
import os
import sys
import ssl
import http.client
import base64
import threading
from urllib.parse import quote as _url_quote, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

_DEFAULT_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '..', '..', 'ai_config.json')

# ── 配置 ──────────────────────────────────────────────────────────────────────

def _load_cfg(cfg_path=None) -> dict:
    path = cfg_path or _DEFAULT_CFG
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


# ── 图片处理 ──────────────────────────────────────────────────────────────────

def _safe_url(url):
    try:
        url.encode('ascii')
        return url
    except UnicodeEncodeError:
        return _url_quote(url, safe=':/?=&%#+@!$,;~*()[]@')


def _http_get(url, depth=0):
    if depth > 3:
        return None
    try:
        parsed = urlparse(_safe_url(url))
        host = parsed.netloc
        path = parsed.path + ('?' + parsed.query if parsed.query else '')
        https = parsed.scheme == 'https'
        ConnCls = http.client.HTTPSConnection if https else http.client.HTTPConnection
        conn = ConnCls(host, timeout=15, context=(_SSL_CTX if https else None))
        try:
            conn.request('GET', path, headers={'User-Agent': 'Mozilla/5.0'})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader('Location', '')
                resp.read()
                return _http_get(location, depth + 1) if location else None
            if resp.status in (200, 206):
                return resp.read()
            return None
        finally:
            conn.close()
    except Exception:
        return None


def _url_to_b64(image_url) -> str | None:
    """在独立线程下载图片，返回 base64 字符串或 None"""
    result = [None]
    def _worker():
        data = _http_get(image_url)
        if data:
            result[0] = base64.b64encode(data).decode('ascii')
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=25)
    return result[0]


# ── Prompt 构建 ───────────────────────────────────────────────────────────────

def _build_prompt(supplier_categories: list[str] | None = None) -> str:
    """
    构建建档识别 prompt。
    supplier_categories: 该供应商可选的分类字母列表，如 ['A', 'B', 'C']
    """
    cat_hint = ''
    if supplier_categories:
        cat_str = '、'.join(supplier_categories)
        cat_hint = (
            f'\n分类选择（从以下字母中选最匹配的一个）：{cat_str}\n'
            '注意：只输出字母本身，不要加括号或其他说明。'
        )
    else:
        cat_hint = '\n分类选择：请根据商品特征自行判断并输出英文大写字母（如A/B/C/...）'

    return (
        '请仔细观察图片中的商品，按以下格式逐行输出，不要输出其他内容：\n\n'
        '商品名称：简洁中文名称（≤12字，如"金属串珠手链"）\n'
        '规格颜色：颜色+规格简述（≤15字，如"金色 约20cm"）\n'
        '参考成本：人民币估价（只写数字，如9.8）\n'
        f'分类字母：{cat_hint.strip()}'
    )


# ── AI 调用（千问视觉）────────────────────────────────────────────────────────

def _call_qianwen(image_b64: str | None, image_url: str | None,
                  api_key: str, model: str, prompt: str) -> str:
    """调用千问视觉 API，返回 AI 回复文本"""
    # 优先下载 URL 为 base64
    if image_url and not image_b64:
        image_b64 = _url_to_b64(image_url)
        if not image_b64:
            raise ValueError('图片下载失败，请确认图片链接可访问')
        image_url = None

    content = []
    if image_b64:
        content.append({'type': 'image_url',
                        'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    elif image_url:
        content.append({'type': 'image_url',
                        'image_url': {'url': _safe_url(image_url)}})
    content.append({'type': 'text', 'text': prompt})

    payload = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': content}],
        'max_tokens': 200,
    }).encode('utf-8')

    conn = http.client.HTTPSConnection('dashscope.aliyuncs.com', timeout=60,
                                        context=_SSL_CTX)
    try:
        conn.request('POST', '/compatible-mode/v1/chat/completions',
                     body=payload,
                     headers={
                         'Authorization': f'Bearer {api_key}',
                         'Content-Type':  'application/json',
                     })
        resp = conn.getresponse()
        data = json.loads(resp.read().decode('utf-8'))
    finally:
        conn.close()

    return data['choices'][0]['message']['content'].strip()


# ── 解析 AI 回复 ──────────────────────────────────────────────────────────────

def _parse_response(ai_text: str) -> dict:
    """
    解析 AI 多行回复。
    返回: {name, spec, cost_hint, category_hint}
    """
    result = {
        'name':          '',
        'spec':          '',
        'cost_hint':     None,
        'category_hint': '',
    }
    for line in ai_text.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        sep = '：' if '：' in line else ':'
        if sep not in line:
            continue
        key_part, val_part = line.split(sep, 1)
        key_part = key_part.strip()
        val_part = val_part.strip()

        if '商品名称' in key_part:
            result['name'] = val_part
        elif '规格' in key_part or '颜色' in key_part:
            result['spec'] = val_part
        elif '参考成本' in key_part or '成本' in key_part:
            try:
                result['cost_hint'] = float(val_part.replace('元', '').strip())
            except (ValueError, AttributeError):
                pass
        elif '分类' in key_part:
            # 提取首个大写字母
            import re
            m = re.search(r'[A-Z]', val_part.upper())
            result['category_hint'] = m.group(0) if m else val_part[:1].upper()

    return result


# ── 公开接口 ──────────────────────────────────────────────────────────────────

def identify_product(
    image_b64: str | None = None,
    image_url: str | None = None,
    supplier_categories: list[str] | None = None,
    cfg_path: str | None = None,
) -> dict:
    """
    识别图片中的商品，返回建档所需字段。

    参数:
        image_b64:           base64 编码的图片（与 image_url 二选一）
        image_url:           图片 URL
        supplier_categories: 供应商可选分类字母列表（用于缩小 AI 判断范围）
        cfg_path:            ai_config.json 路径（默认 ../../ai_config.json）

    返回:
        {
            'name':           str,   商品名称（AI 建议）
            'spec':           str,   规格颜色（可直接写入 JDY spec 字段）
            'cost_hint':      float | None,  参考成本价
            'category_hint':  str,   分类字母建议（如 'C'）
            'raw_text':       str,   AI 原始回复
            'error':          str | None,
        }
    """
    if not image_b64 and not image_url:
        return {'error': '需要提供 image_b64 或 image_url', 'name': '', 'spec': '',
                'cost_hint': None, 'category_hint': '', 'raw_text': ''}

    cfg = _load_cfg(cfg_path)
    api_key = cfg.get('qianwen_api_key', '').strip()
    if not api_key:
        return {'error': '请在 ai_config.json 中配置 qianwen_api_key',
                'name': '', 'spec': '', 'cost_hint': None, 'category_hint': '', 'raw_text': ''}

    model = cfg.get('qianwen_model', 'qwen-vl-max')
    prompt = _build_prompt(supplier_categories)

    try:
        raw_text = _call_qianwen(image_b64, image_url, api_key, model, prompt)
    except Exception as e:
        return {'error': str(e), 'name': '', 'spec': '',
                'cost_hint': None, 'category_hint': '', 'raw_text': ''}

    parsed = _parse_response(raw_text)
    return {
        'name':          parsed['name'],
        'spec':          parsed['spec'],
        'cost_hint':     parsed['cost_hint'],
        'category_hint': parsed['category_hint'],
        'raw_text':      raw_text,
        'error':         None,
    }


# ── 订单照片解析 ──────────────────────────────────────────────────────────────

def _build_order_prompt(supplier_categories: list[str] | None = None) -> str:
    """构建订单解析 prompt"""
    cat_hint = ''
    if supplier_categories:
        cat_hint = '、'.join(supplier_categories)
    else:
        cat_hint = 'A/B/C/...'

    return (
        '识别订单图中的供应商名称和所有产品行，只输出 JSON，不要加说明：\n'
        '{\n'
        '  "supplier_name": "供应商名或档口号",\n'
        '  "products": [\n'
        '    {"line":1, "name":"中文名≤12字", "spec":"颜色规格≤15字",\n'
        f'     "category_hint":"从以下选一个大写字母: {cat_hint}",\n'
        '     "qty":数量, "price":单价,\n'
        '     "ctn_qty":箱数, "pcs_per_ctn":每箱件数}\n'
        '  ]\n'
        '}\n'
        '数字识别不到填0，只输出JSON不加说明。'
    )


def _parse_order_json(ai_text: str) -> dict:
    """
    从 AI 回复中提取 JSON 对象。
    支持 AI 可能在 JSON 前后加说明文字的情况。
    """
    text = ai_text.strip()
    # 尝试找 JSON 块（```json ... ``` 或直接 { ... }）
    import re as _re
    # 去掉 markdown 代码块
    text = _re.sub(r'```(?:json)?\s*', '', text).strip().rstrip('`').strip()
    # 找第一个 { 到最后一个 }
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def parse_order_image(
    image_b64: str | None = None,
    image_url: str | None = None,
    supplier_categories: list[str] | None = None,
    cfg_path: str | None = None,
) -> dict:
    """
    解析订单照片，返回供应商名 + 多行产品。

    参数:
        image_b64:           base64 编码图片
        image_url:           图片 URL
        supplier_categories: 供应商可选分类字母列表（缩小 AI 判断范围）
        cfg_path:            ai_config.json 路径

    返回:
    {
        "supplier_name": "广州XX档口",
        "products": [
            {"line": 1, "name": "珍珠手链", "spec": "金色约20cm",
             "category_hint": "C", "qty": 120, "price": 9.8,
             "ctn_qty": 10, "pcs_per_ctn": 12}
        ],
        "raw_text": "...",
        "error": null
    }
    """
    _empty = {'supplier_name': '', 'products': [], 'raw_text': '', 'error': None}

    if not image_b64 and not image_url:
        return {**_empty, 'error': '需要提供 image_b64 或 image_url'}

    cfg = _load_cfg(cfg_path)
    api_key = cfg.get('qianwen_api_key', '').strip()
    if not api_key:
        return {**_empty, 'error': '请在 ai_config.json 中配置 qianwen_api_key'}

    model = cfg.get('qianwen_model', 'qwen-vl-max')
    prompt = _build_order_prompt(supplier_categories)

    try:
        # 增加 max_tokens 到 1500 以容纳多行 JSON
        content = []
        if image_b64:
            if not image_url:
                content.append({'type': 'image_url',
                                'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
        elif image_url:
            b64 = _url_to_b64(image_url)
            if b64:
                content.append({'type': 'image_url',
                                'image_url': {'url': f'data:image/jpeg;base64,{b64}'}})
            else:
                content.append({'type': 'image_url',
                                'image_url': {'url': _safe_url(image_url)}})
        content.append({'type': 'text', 'text': prompt})

        payload = json.dumps({
            'model': model,
            'messages': [{'role': 'user', 'content': content}],
            'max_tokens': 1500,
        }).encode('utf-8')

        conn = http.client.HTTPSConnection('dashscope.aliyuncs.com', timeout=60,
                                            context=_SSL_CTX)
        try:
            conn.request('POST', '/compatible-mode/v1/chat/completions',
                         body=payload,
                         headers={
                             'Authorization': f'Bearer {api_key}',
                             'Content-Type':  'application/json',
                         })
            resp = conn.getresponse()
            data = json.loads(resp.read().decode('utf-8'))
        finally:
            conn.close()

        raw_text = data['choices'][0]['message']['content'].strip()
    except Exception as e:
        return {**_empty, 'error': str(e)}

    parsed = _parse_order_json(raw_text)
    if not parsed:
        return {**_empty, 'raw_text': raw_text,
                'error': f'JSON 解析失败，AI 原文: {raw_text[:200]}'}

    # 规范化产品列表
    products = []
    for i, item in enumerate(parsed.get('products') or []):
        if not isinstance(item, dict):
            continue
        products.append({
            'line':          item.get('line', i + 1),
            'name':          str(item.get('name') or '').strip()[:12],
            'spec':          str(item.get('spec') or '').strip()[:15],
            'category_hint': str(item.get('category_hint') or '').strip()[:2].upper(),
            'qty':           _to_num(item.get('qty'), 0),
            'price':         _to_num(item.get('price'), 0.0),
            'ctn_qty':       _to_num(item.get('ctn_qty'), 0),
            'pcs_per_ctn':   _to_num(item.get('pcs_per_ctn'), 0),
        })

    return {
        'supplier_name': str(parsed.get('supplier_name') or '').strip(),
        'products':      products,
        'raw_text':      raw_text,
        'error':         None,
    }


def _to_num(val, default):
    """安全转数字"""
    try:
        return type(default)(val)
    except (TypeError, ValueError):
        return default


# ── 快速测试（不实际调用 API）────────────────────────────────────────────────
if __name__ == '__main__':
    sample_text = (
        '商品名称：金属串珠手链\n'
        '规格颜色：金色 约20cm\n'
        '参考成本：9.8\n'
        '分类字母：C'
    )
    parsed = _parse_response(sample_text)
    print('解析测试:')
    for k, v in parsed.items():
        print(f'  {k}: {v!r}')

    print('\n完整返回结构预览:')
    result = {
        'name':          parsed['name'],
        'spec':          parsed['spec'],
        'cost_hint':     parsed['cost_hint'],
        'category_hint': parsed['category_hint'],
        'raw_text':      sample_text,
        'error':         None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
