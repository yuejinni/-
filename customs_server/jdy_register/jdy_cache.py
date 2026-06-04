"""
JDY 本地数据缓存
- 商品列表（全量，分页拉取）
- 供应商列表（全量）
- 增量刷新：API total count 变化时重拉

用法:
    cache = JDYCache(cfg_path='../ai_config.json')
    cache.ensure_fresh()           # 启动时调用，若过期则后台刷新
    products = cache.get_products('account1')
    suppliers = cache.get_suppliers('account1')
"""

import json
import os
import sys
import threading
import time
from datetime import datetime

# 确保能 import 上级目录的 jdy_api
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jdy_api

_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_cache')


def _load_json(path):
    try:
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


class JDYCache:
    """
    管理两个账套的商品/供应商缓存。
    缓存文件：
        _cache/products_account1.json
        _cache/products_account2.json
        _cache/suppliers_account1.json
        _cache/suppliers_account2.json
    每个文件格式：
        {
          "items": [...],
          "total": 30900,
          "sync_time": "2026-05-21T10:00:00",
          "account": "account1"
        }
    """

    def __init__(self, cfg_path=None, cfg=None):
        """
        cfg_path: ai_config.json 路径
        cfg: 直接传入 config dict（二选一）
        """
        if cfg is None:
            if cfg_path is None:
                cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        '..', '..', 'ai_config.json')
            with open(cfg_path, encoding='utf-8') as f:
                cfg = json.load(f)
        self.cfg = cfg
        self._clients = {}
        self._lock = threading.Lock()

    def _get_client(self, account: str) -> jdy_api.JDYClient:
        if account not in self._clients:
            if account == 'account1':
                c = jdy_api.JDYClient(
                    self.cfg['jdy_client_id'], self.cfg['jdy_app_key'],
                    self.cfg['jdy_app_secret'], self.cfg['jdy_db_id'],
                    self.cfg['jdy_domain'], client_secret=self.cfg['jdy_client_secret'],
                )
            else:
                c = jdy_api.JDYClient(
                    self.cfg['jdy2_client_id'], self.cfg['jdy2_app_key'],
                    self.cfg['jdy2_app_secret'], self.cfg['jdy2_db_id'],
                    self.cfg['jdy2_domain'], client_secret=self.cfg['jdy2_client_secret'],
                )
            self._clients[account] = c
        return self._clients[account]

    # ── API 实际总数 ──────────────────────────────────────────────────────────

    def _api_total(self, account: str, resource: str) -> int:
        """快速查 API 当前记录总数（只取1条）"""
        cli = self._get_client(account)
        endpoint = f'/jdyscm/{resource}/list'
        try:
            r = cli._request('POST', endpoint,
                             body={'filter': {'page': 1, 'pageSize': 1}},
                             query=cli._api_query(), timeout=10)
            return int(r.get('records') or r.get('totalsize') or 0)
        except Exception as e:
            print(f'[CACHE] {account} {resource} total 查询失败: {e}')
            return -1

    # ── 是否需要刷新 ──────────────────────────────────────────────────────────

    def _needs_refresh(self, account: str, resource: str) -> bool:
        path = os.path.join(_CACHE_DIR, f'{resource}_{account}.json')
        cached = _load_json(path)
        if not cached:
            return True
        api_total = self._api_total(account, resource)
        if api_total < 0:
            return False   # API 不可用，用缓存
        return api_total != cached.get('total', -1)

    # ── 全量拉取 ──────────────────────────────────────────────────────────────

    def _fetch_all(self, account: str, resource: str,
                   page_size: int = 100, on_progress=None) -> list:
        """分页拉取所有记录，返回 list"""
        cli = self._get_client(account)
        endpoint = f'/jdyscm/{resource}/list'
        all_items = []
        page = 1
        total = None

        while True:
            try:
                r = cli._request('POST', endpoint,
                                 body={'filter': {'page': page, 'pageSize': page_size}},
                                 query=cli._api_query(), timeout=20)
            except Exception as e:
                print(f'[CACHE] {account} {resource} page={page} 失败: {e}，等待重试...')
                time.sleep(2)
                continue

            items = r.get('items') or []
            all_items.extend(items)

            if total is None:
                total = int(r.get('records') or r.get('totalsize') or len(all_items))

            if on_progress:
                on_progress(len(all_items), total)

            if not items or len(all_items) >= total:
                break
            page += 1

        return all_items

    def _refresh(self, account: str, resource: str, background: bool = False):
        """拉取并保存缓存"""
        def _do():
            print(f'[CACHE] 开始全量拉取 {account}/{resource}...')
            t0 = time.time()
            total_before = self._api_total(account, resource)

            def on_prog(got, total):
                if got % 500 == 0:
                    print(f'[CACHE] {account}/{resource}: {got}/{total}')

            items = self._fetch_all(account, resource, on_progress=on_prog)
            path = os.path.join(_CACHE_DIR, f'{resource}_{account}.json')
            _save_json(path, {
                'items': items,
                'total': len(items),
                'api_total_at_sync': total_before,
                'sync_time': datetime.now().isoformat(),
                'account': account,
            })
            elapsed = time.time() - t0
            print(f'[CACHE] ✅ {account}/{resource} 缓存完成: {len(items)} 条，耗时 {elapsed:.1f}s')

        if background:
            t = threading.Thread(target=_do, daemon=True)
            t.start()
            return t
        else:
            _do()

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    def ensure_fresh(self, background: bool = True):
        """
        启动时调用：检查两个账套的商品/供应商缓存是否过期，
        过期则在后台线程中刷新。
        """
        for account in ['account1', 'account2']:
            for resource in ['product', 'supplier']:
                if self._needs_refresh(account, resource):
                    print(f'[CACHE] {account}/{resource} 需要刷新，启动{"后台" if background else ""}拉取...')
                    self._refresh(account, resource, background=background)
                else:
                    print(f'[CACHE] {account}/{resource} 缓存有效，跳过')

    def get_products(self, account: str) -> list:
        """获取商品列表（优先缓存）"""
        path = os.path.join(_CACHE_DIR, f'product_{account}.json')
        cached = _load_json(path)
        if cached:
            return cached.get('items', [])
        # 缓存不存在则同步拉取
        print(f'[CACHE] {account}/product 无缓存，同步拉取...')
        self._refresh(account, 'product', background=False)
        cached = _load_json(path)
        return cached.get('items', []) if cached else []

    def get_suppliers(self, account: str) -> list:
        """获取供应商列表（优先缓存）"""
        path = os.path.join(_CACHE_DIR, f'supplier_{account}.json')
        cached = _load_json(path)
        if cached:
            return cached.get('items', [])
        print(f'[CACHE] {account}/supplier 无缓存，同步拉取...')
        self._refresh(account, 'supplier', background=False)
        cached = _load_json(path)
        return cached.get('items', []) if cached else []

    def get_supplier_map(self, account: str) -> dict:
        """
        返回 {supplier_id: supplier_dict}，方便按 defaultSupplierId 查供应商
        """
        sups = self.get_suppliers(account)
        return {str(s['id']): s for s in sups if s.get('id')}

    def force_refresh(self, account: str = None, resource: str = None):
        """强制重拉指定账套/资源（同步）"""
        accounts = ['account1', 'account2'] if account is None else [account]
        resources = ['product', 'supplier'] if resource is None else [resource]
        for acc in accounts:
            for res in resources:
                self._refresh(acc, res, background=False)

    # ── 商品写入接口 ──────────────────────────────────────────────────────────

    def create_product(self, account: str, product_dict: dict) -> dict:
        """
        调用 POST /jdyscm/product/add 新建商品。
        返回 {'ok': True, 'data': {...}} 或 {'ok': False, 'errcode': N, 'msg': '...'}
        """
        cli = self._get_client(account)
        result = cli._request('POST', '/jdyscm/product/add',
                              body=product_dict, query=cli._api_query())
        errcode = result.get('errcode') or result.get('code') or 0
        if errcode and errcode != 0:
            return {'ok': False, 'errcode': errcode,
                    'msg': result.get('msg') or result.get('message') or str(result)}
        return {'ok': True, 'data': result.get('data') or result}

    def cache_info(self) -> dict:
        """返回各缓存的状态信息"""
        info = {}
        for account in ['account1', 'account2']:
            for resource in ['product', 'supplier']:
                path = os.path.join(_CACHE_DIR, f'{resource}_{account}.json')
                cached = _load_json(path)
                key = f'{account}/{resource}'
                if cached:
                    info[key] = {
                        'total': cached.get('total', 0),
                        'sync_time': cached.get('sync_time', ''),
                        'file': path,
                    }
                else:
                    info[key] = {'total': 0, 'sync_time': '', 'file': path}
        return info


# ── 命令行运行（手动触发全量缓存）──────────────────────────────────────────────
if __name__ == '__main__':
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', '..', 'ai_config.json')
    cache = JDYCache(cfg_path=cfg_path)

    import argparse
    p = argparse.ArgumentParser(description='JDY 缓存管理工具')
    p.add_argument('--force', action='store_true', help='强制全量重拉')
    p.add_argument('--info',  action='store_true', help='显示缓存状态')
    p.add_argument('--account', default=None, help='指定账套 account1/account2')
    args = p.parse_args()

    if args.info:
        info = cache.cache_info()
        print('\n缓存状态:')
        for k, v in info.items():
            print(f'  {k}: {v["total"]} 条  最后同步: {v["sync_time"] or "从未"}')
    elif args.force:
        cache.force_refresh(account=args.account)
    else:
        cache.ensure_fresh(background=False)   # 命令行模式用同步
