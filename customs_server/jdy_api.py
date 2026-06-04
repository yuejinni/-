"""
精斗云 (JDY / Kingdee) API 客户端

认证方式（已验证）：
  GET /jdyconnector/app_management/kingdee_auth_token
  params: client_id, app_key, client_secret, app_signature
  app_signature = Base64( HMAC-SHA256(app_key, key=app_secret).hexdigest() )
  其中 app_secret 是每日轮换的沙箱密钥（非 client_secret）

所有业务接口必须携带 X-GW-Router-Addr header（domain 值）
"""
import json
import time
import hmac
import hashlib
import base64
import http.client
import ssl
import re
import os
import sys
from urllib.parse import quote as urlquote, urlencode

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

_API_HOST = 'api.kingdee.com'


def _x_api_signature(method, path, params, ts, nonce, client_secret):
    """
    X-Api-Signature V2 算法：
    sign_text = method\n URL编码path\n 排序双编码params\n x-api-nonce:nonce\n x-api-timestamp:ts\n
    sig = Base64( HMAC-SHA256(sign_text, key=client_secret).hexdigest() )
    """
    import re as _re

    def _enc2(s):
        """两次 URL 编码，%后字母大写"""
        s1 = urlquote(str(s), safe='')
        s2 = urlquote(s1, safe='')
        return _re.sub(r'%[0-9a-fA-F]{2}', lambda m: m.group().upper(), s2)

    encoded_path = urlquote(path, safe='')
    # 参数按 key ASCII 升序，key/value 双编码
    param_str = '&'.join(
        f'{_enc2(k)}={_enc2(v)}'
        for k, v in sorted(params.items())
    ) if params else ''

    # 签名头按字母序小写（nonce 在 timestamp 前）
    sign_text = '\n'.join([
        method.upper(),
        encoded_path,
        param_str,
        f'x-api-nonce:{nonce}',
        f'x-api-timestamp:{ts}',
        '',          # 末尾有换行
    ])
    hex_str = hmac.new(client_secret.encode(), sign_text.encode(), hashlib.sha256).hexdigest()
    sig = base64.b64encode(hex_str.encode()).decode()
    print(f'[JDY] X-Api-Sig sign_text={repr(sign_text[:120])} → {sig[:24]}…')
    return sig


def push_app_authorize(client_id, client_secret, outer_instance_id):
    """
    主动获取授权信息（正式版获取 appKey/appSecret/domain）
    POST /jdyconnector/app_management/push_app_authorize?outerInstanceId=xxx
    返回: [{'appKey':..., 'appSecret':..., 'domain':..., 'accountId':..., 'clientId':...}]
    """
    import time as _time
    import random as _random

    ts    = str(int(_time.time() * 1000))
    nonce = str(_random.randint(100000, 999999999))

    method = 'POST'
    path   = '/jdyconnector/app_management/push_app_authorize'
    params = {'outerInstanceId': str(outer_instance_id)}

    sig = _x_api_signature(method, path, params, ts, nonce, client_secret)

    headers = {
        'Content-Type':      'application/json',
        'X-Api-ClientID':    str(client_id),
        'X-Api-Auth-Version':'2.0',
        'X-Api-TimeStamp':   ts,
        'X-Api-Nonce':       nonce,
        'X-Api-SignHeaders':  'X-Api-TimeStamp,X-Api-Nonce',
        'X-Api-Signature':   sig,
    }
    full_path = f'{path}?outerInstanceId={urlquote(str(outer_instance_id))}'

    conn = http.client.HTTPSConnection(_API_HOST, timeout=30, context=_SSL_CTX)
    try:
        conn.request('POST', full_path, body=None, headers=headers)
        resp = conn.getresponse()
        raw  = resp.read().decode('utf-8')
        print(f'[JDY] push_app_authorize → HTTP {resp.status}: {raw[:300]}')
        return json.loads(raw)
    finally:
        conn.close()


def parse_dimensions(registration_no):
    """
    解析尺寸字符串 → {'l': float, 'w': float, 'h': float, 'vol': float} 或 None
    支持格式: "75*45*25cm" / "75X45X25" / "75×45×25CM"
    """
    if not registration_no:
        return None
    text = str(registration_no).strip().upper()
    text = text.replace('×', '*').replace('X', '*')
    text = re.sub(r'[A-Z]+', '', text)
    m = re.search(r'(\d+(?:\.\d+)?)\*(\d+(?:\.\d+)?)\*(\d+(?:\.\d+)?)', text)
    if m:
        l, w, h = float(m.group(1)), float(m.group(2)), float(m.group(3))
        return {'l': l, 'w': w, 'h': h, 'vol': round(l * w * h, 2)}
    return None


# ──────────────────────────────────────────────────────────────────────────────
class JDYClient:
    """
    精斗云 API 客户端

    认证流程：
      POST /jdyconnector/app_management/kingdee_auth_token
      params: client_id, app_key, app_secret
      → 返回 access_token

    所有业务接口：
      POST https://api.kingdee.com/jdy/{resource}/list
           ?access_token={token}&dbId={dbId}
      headers: Content-Type: application/json
               X-GW-Router-Addr: {domain}
    """

    def __init__(self, client_id, app_key, app_secret, db_id, domain='', app_signature='', client_secret=''):
        """
        client_id:  应用ID（如 344427）
        app_key:    授权 key
        app_secret: 授权密钥或轮换密钥
        db_id:      账套ID（accountId）
        domain:     IDC 域名（如 http://vip1-gd.jdy.com），必填
        """
        self.client_id  = str(client_id)
        self.app_key    = app_key
        self.app_secret = app_secret
        self.db_id      = str(db_id)
        self.domain     = domain.rstrip('/')   # 去掉末尾斜杠

        self.app_signature  = app_signature
        self.client_secret  = client_secret   # 固定的应用密钥，用于 token 请求
        self._access_token  = None
        self._token_expires = 0.0

    # ── HTTP 工具 ─────────────────────────────────────────────────────────────

    def _headers(self):
        """所有请求的通用 headers"""
        h = {'Content-Type': 'application/json'}
        if self.domain:
            h['X-GW-Router-Addr'] = self.domain
        return h

    def _request(self, method, path, body=None, query=None, timeout=30):
        """
        发送 HTTPS 请求
        query: dict，追加到 URL query string
        body:  dict，序列化为 JSON body
        """
        qs = ''
        if query:
            qs = '?' + urlencode(query)
        full_path = path + qs

        payload = (
            json.dumps(body, ensure_ascii=False).encode('utf-8')
            if body is not None else b''
        )

        conn = http.client.HTTPSConnection(_API_HOST, timeout=timeout, context=_SSL_CTX)
        try:
            conn.request(method.upper(), full_path, body=payload or None, headers=self._headers())
            resp = conn.getresponse()
            raw  = resp.read().decode('utf-8')
            safe_path = re.sub(r'(access_token|client_secret|app_signature)=([^&]+)', r'\1=***', full_path)
            print(f'[JDY] {method} {safe_path[:120]} → HTTP {resp.status}')
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                preview = (raw or '').strip().replace('\r', ' ').replace('\n', ' ')[:240]
                if not preview:
                    preview = 'empty response'
                raise RuntimeError(f'JDY API returned non-JSON response: HTTP {resp.status}; {preview}')
            code = result.get('errcode') or result.get('code')
            if code and code != 0:
                print(f'[JDY WARN] {raw[:400]}')
            return result
        finally:
            conn.close()

    def _request_with_retry(self, method, path, body=None, query=None, timeout=30, attempts=3):
        last_error = None
        for attempt in range(max(1, attempts)):
            try:
                return self._request(method, path, body=body, query=query, timeout=timeout)
            except RuntimeError as e:
                last_error = e
                text = str(e)
                if 'HTTP 504' not in text and 'Gateway Time-out' not in text:
                    raise
                if attempt + 1 >= max(1, attempts):
                    raise
                time.sleep(2 + attempt * 3)
        raise last_error

    # ── Token 管理 ────────────────────────────────────────────────────────────

    def _fetch_token(self):
        """
        GET /jdyconnector/app_management/kingdee_auth_token
        沙箱模式：需要 app_signature = Base64(HMAC-SHA256(app_key, key=rotating_app_secret))
        正式模式：app_secret == client_secret 时跳过 app_signature，直接用三个参数
        """
        path = '/jdyconnector/app_management/kingdee_auth_token'
        cs_used = self.client_secret or self.app_secret

        query = {
            'client_id':     self.client_id,
            'app_key':       self.app_key,
            'client_secret': cs_used,
        }

        # 只有当 app_secret 与 client_secret 不同时（沙箱旋转密钥）才附加签名
        signing_key = self.app_secret if (self.app_secret and self.app_secret != self.client_secret) else None
        if signing_key:
            h = hmac.new(signing_key.encode(), self.app_key.encode(), hashlib.sha256)
            computed_sig = base64.b64encode(h.hexdigest().encode()).decode()
            query['app_signature'] = computed_sig
            print(f'[JDY] token请求(沙箱): client_id={self.client_id}, app_key={self.app_key}, client_secret={cs_used[:8]}..., sig={computed_sig[:16]}...')
        else:
            print(f'[JDY] token请求(正式): client_id={self.client_id}, app_key={self.app_key}, client_secret={cs_used[:8]}...')

        result = self._request('GET', path, body=None, query=query)

        # 兼容多种返回格式
        data = result.get('data') or result
        token = None
        expires_in = 7200
        if isinstance(data, dict):
            token      = (data.get('access_token') or data.get('token')
                          or data.get('accessToken'))
            expires_in = int(data.get('expires_in') or data.get('expiresIn') or 7200)
        # 有些接口直接在顶层返回 token
        if not token:
            token = result.get('access_token') or result.get('token')

        if not token:
            raise RuntimeError(
                f'获取 token 失败，响应: {json.dumps(result, ensure_ascii=False)[:400]}'
            )
        return token, expires_in

    def _get_access_token(self, force=False):
        now = time.time()
        if not force and self._access_token and now < self._token_expires - 300:
            return self._access_token
        token, expires_in = self._fetch_token()
        self._access_token  = token
        self._token_expires = now + expires_in
        print(f'[JDY] token 刷新成功，有效 {expires_in}s')
        return token

    @property
    def token(self):
        return self._get_access_token()

    def _api_query(self, extra=None):
        """业务接口公共 query string: access_token + dbId"""
        q = {'access_token': self.token, 'dbId': self.db_id}
        if extra:
            q.update(extra)
        return q

    def test_connection(self):
        try:
            t = self._get_access_token(force=True)
            return {'ok': True, 'msg': f'连接成功，token: {t[:16]}…'}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ── 调拨单接口 ────────────────────────────────────────────────────────────

    def get_transfer_orders(self, page=1, page_size=20, search='', status='',
                            begin_date='', end_date=''):
        """获取调拨单列表
        文档路径: POST /jdyscm/transfer/list
        body.filter: number/checkStatus/beginDate/endDate/pageSize/page
        """
        f = {'page': page, 'pageSize': page_size}
        if search:
            f['number'] = search
        if status != '':
            # 0:未审核 1:已审核 2:所有
            try:
                f['checkStatus'] = int(status)
            except (ValueError, TypeError):
                pass
        if begin_date:
            f['beginDate'] = begin_date
        if end_date:
            f['endDate'] = end_date

        result = self._request('POST', '/jdyscm/transfer/list',
                               body={'filter': f}, query=self._api_query(),
                               timeout=120)
        items = result.get('items') or []
        total = (result.get('records') or result.get('totalsize') or len(items))
        return {'list': items, 'total': total, 'raw': result}

    def get_transfer_order_detail(self, order_id):
        """获取调拨单详情（按单号查单条）"""
        # 用 number 过滤列表来获取详情
        result = self._request('POST', '/jdyscm/transfer/list',
                               body={'filter': {'number': str(order_id), 'pageSize': 1, 'page': 1}},
                               query=self._api_query(), timeout=120)
        items = result.get('items') or []
        return items[0] if items else result

    # ── 销货单接口（只读）────────────────────────────────────────────────────

    def get_sales_orders(self, page=1, page_size=100, search='',
                         begin_date='', end_date=''):
        """
        获取销货单列表（只读）。
        目前按精斗云 SCM 销货单常用路径 /jdyscm/delivery/list 调用；
        如果现场账号接口路径不同，可只调整这里，不影响前端页面。
        """
        f = {'page': page, 'pageSize': page_size, 'checkStatus': 2}
        if search:
            # 精斗云 delivery/list 通常支持 number；客户/商品搜索后续可扩展为更多字段。
            f['number'] = search
        if begin_date:
            f['beginDate'] = begin_date
        if end_date:
            f['endDate'] = end_date
        result = self._request('POST', '/jdyscm/delivery/list',
                               body={'filter': f}, query=self._api_query(),
                               timeout=120)
        items = result.get('items') or result.get('list') or []
        total = result.get('records') or result.get('totalsize') or len(items)
        return {'list': items, 'total': total, 'raw': result}

    def get_sales_order_detail(self, order_no):
        """获取销货单详情（只读，按单号过滤列表取单条）。"""
        result = self._request('POST', '/jdyscm/delivery/list',
                               body={'filter': {'number': str(order_no), 'pageSize': 1, 'page': 1}},
                               query=self._api_query(),
                               timeout=120)
        items = result.get('items') or result.get('list') or []
        return items[0] if items else result

    def get_inventory_by_product(self, product_number, page=1, page_size=50):
        """
        查询商品即时库存（只读）。
        精斗云该接口为 GET，商品编号过滤字段实测为 number。
        """
        result = self._request('GET', '/jdyscm/inventory/list',
                               body=None,
                               query=self._api_query({
                                   'number': product_number,
                                   'page': page,
                                   'pageSize': page_size,
                               }))
        items = result.get('items') or result.get('list') or []
        return {'list': items, 'total': result.get('records') or len(items)}

    # ── 商品接口 ──────────────────────────────────────────────────────────────

    def get_product_by_code(self, code):
        """根据商品编号查询商品（/jdyscm/product/list，filter.productNumber）"""
        result = self._request('POST', '/jdyscm/product/list',
                               body={'filter': {'productNumber': code,
                                                'page': 1, 'pageSize': 10}},
                               query=self._api_query())
        items = (result.get('items') or result.get('list') or [])
        for item in items:
            if str(item.get('productNumber', '')).strip() == str(code).strip():
                return item
        return items[0] if items else None

    def get_products_by_codes(self, codes):
        """批量查询商品（最多30个编号，逗号隔开）"""
        if not codes:
            return []
        batch = codes[:30]
        result = self._request('POST', '/jdyscm/product/list',
                               body={'filter': {'numbers': ','.join(batch),
                                                'page': 1, 'pageSize': 30}},
                               query=self._api_query())
        return result.get('items') or []

    def get_products(self, page=1, page_size=100, product_number='', category_name='', search='', category_id=''):
        """查询商品列表（只读）。可按商品编号和分类名称过滤。"""
        f = {'page': page, 'pageSize': page_size}
        if product_number:
            f['productNumber'] = product_number
        elif search:
            f['productNumber'] = search
        if category_id:
            f['categoryId'] = category_id
        if category_name:
            f['categoryName'] = category_name
        result = self._request('POST', '/jdyscm/product/list',
                               body={'filter': f},
                               query=self._api_query(),
                               timeout=120)
        items = result.get('items') or result.get('list') or []
        total = result.get('records') or result.get('totalsize') or len(items)
        return {'list': items, 'total': total, 'raw': result}

    def get_product_categories(self, page=1, page_size=200):
        """查询商品分类列表（只读）。"""
        result = self._request('GET', '/jdyscm/productCategory/list',
                               body=None,
                               query=self._api_query({'page': page, 'pageSize': page_size}),
                               timeout=60)
        items = result.get('items') or result.get('list') or []
        total = result.get('records') or result.get('totalsize') or len(items)
        return {'list': items, 'total': total, 'raw': result}

    # ── 购货入库单接口 ────────────────────────────────────────────────────────

    def get_purchase_orders(self, page=1, page_size=100, search='',
                            begin_date='', end_date=''):
        """获取购货入库单列表（通用，支持按单号/供应商/日期过滤）"""
        f = {'page': page, 'pageSize': page_size, 'checkStatus': 2}
        if search:
            f['number'] = search
        if begin_date:
            f['beginDate'] = begin_date
        if end_date:
            f['endDate'] = end_date
        result = self._request('POST', '/jdyscm/purchase/list',
                               body={'filter': f}, query=self._api_query())
        items = result.get('items') or []
        total = result.get('records') or len(items)
        return {'list': items, 'total': total}

    def get_purchase_orders_by_product(self, product_number, page=1, page_size=20,
                                       begin_date='', end_date=''):
        """根据商品编号查询购货入库单列表，按日期降序"""
        f = {'productNumber': product_number, 'page': page, 'pageSize': page_size}
        if begin_date:
            f['beginDate'] = begin_date
        if end_date:
            f['endDate'] = end_date
        result = self._request('POST', '/jdyscm/purchase/list',
                               body={'filter': f},
                               query=self._api_query())
        items = result.get('items') or []
        items.sort(key=lambda x: x.get('date') or '', reverse=True)
        return {'list': items, 'total': result.get('records') or len(items)}

    # ── 供应商接口 ────────────────────────────────────────────────────────────

    def update_product_pro_license(self, product_id, pro_license):
        """将 proLicense 写回精斗云商品档案（/jdyscm/product/update）"""
        result = self._request('POST', '/jdyscm/product/update',
                               body={'data': {'id': product_id, 'proLicense': pro_license}},
                               query=self._api_query())
        return result

    def get_purchase_order_requests(self, page=1, page_size=100, search='',
                                    bill_status=None, check_status=None,
                                    begin_date='', end_date='', supplier_number='',
                                    product_number=''):
        """Read purchase orders. billStatus 0 means not converted/received."""
        f = {'page': page, 'pageSize': page_size}
        if search:
            f['number'] = search
        if bill_status is not None:
            f['billStatus'] = bill_status
        if check_status is not None:
            f['checkStatus'] = check_status
        if begin_date:
            f['beginDate'] = begin_date
        if end_date:
            f['endDate'] = end_date
        if supplier_number:
            f['supplierNumber'] = supplier_number
        if product_number:
            f['productNumber'] = product_number
        result = self._request_with_retry('POST', '/jdyscm/purchaseOrder/list',
                                          body={'filter': f},
                                          query=self._api_query(),
                                          timeout=120,
                                          attempts=3)
        items = result.get('items') or result.get('list') or []
        total = result.get('records') or result.get('totalsize') or len(items)
        return {'list': items, 'total': total, 'raw': result}

    def get_suppliers(self, page=1, page_size=50, status=2, search=''):
        """Read suppliers. status: 0=enabled, 1=disabled, 2=all."""
        f = {'page': page, 'pageSize': page_size}
        if status is not None:
            f['status'] = status
        if search:
            f['name'] = search
        result = self._request_with_retry('POST', '/jdyscm/supplier/list',
                                          body={'filter': f},
                                          query=self._api_query(),
                                          timeout=120,
                                          attempts=2)
        items = result.get('items') or result.get('list') or []
        total = result.get('records') or result.get('totalsize') or len(items)
        return {'list': items, 'total': total, 'raw': result}

    def get_supplier_by_number(self, number, status=0):
        """根据供应商编号查询供应商。status: 0=启用, 1=禁用, 2=全部"""
        result = self._request_with_retry('POST', '/jdyscm/supplier/list',
                                          body={'filter': {'number': number,
                                                           'status': status,
                                                           'page': 1, 'pageSize': 10}},
                                          query=self._api_query(),
                                          timeout=120,
                                          attempts=2)
        items = (result.get('items') or result.get('list') or [])
        for item in items:
            if str(item.get('number', '')).strip() == str(number).strip():
                return item
        return items[0] if items else None

    # ── AES 解密（隐私字段，按需调用） ────────────────────────────────────────

    def decrypt_privacy(self, encrypted):
        if not encrypted:
            return encrypted
        try:
            import base64
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import unpad
            key = (self.client_secret or self.app_secret).encode('utf-8')[:32].ljust(32, b'\0')
            iv  = b'5e8y6w45ju8w9jq8'
            raw = base64.b64decode(encrypted)
            cipher = AES.new(key, AES.MODE_CBC, iv)
            return unpad(cipher.decrypt(raw), AES.block_size).decode('utf-8')
        except Exception as e:
            print(f'[JDY] decrypt_privacy error: {e}')
            return encrypted


# ── 全局单例（支持两个账套） ───────────────────────────────────────────────────
_client_instance   = None   # 账套 1（饰品）
_client_instance_2 = None   # 账套 2（箱包）


def get_client():
    return _client_instance


def get_client_2():
    return _client_instance_2


def get_client_by_name(name, name1, name2):
    """根据账套名称返回对应 client"""
    if name == name2:
        return _client_instance_2
    return _client_instance


def init_client(client_id, app_key, app_secret, db_id, domain='', app_signature='', client_secret=''):
    global _client_instance
    _client_instance = JDYClient(client_id, app_key, app_secret, db_id, domain, app_signature, client_secret)
    return _client_instance


def init_client_2(client_id, app_key, app_secret, db_id, domain='', app_signature='', client_secret=''):
    global _client_instance_2
    _client_instance_2 = JDYClient(client_id, app_key, app_secret, db_id, domain, app_signature, client_secret)
    return _client_instance_2
