"""
JDY 建档可行性探测脚本
运行: python test_jdy_register.py
"""
import json, os, sys, re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jdy_api

_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'ai_config.json')
with open(_cfg_path, encoding='utf-8') as f:
    cfg = json.load(f)


def _init_cli(n=1):
    if n == 1:
        return jdy_api.JDYClient(cfg['jdy_client_id'], cfg['jdy_app_key'], cfg['jdy_app_secret'],
                                  cfg['jdy_db_id'], cfg['jdy_domain'], client_secret=cfg['jdy_client_secret'])
    return jdy_api.JDYClient(cfg['jdy2_client_id'], cfg['jdy2_app_key'], cfg['jdy2_app_secret'],
                              cfg['jdy2_db_id'], cfg['jdy2_domain'], client_secret=cfg['jdy2_client_secret'])


def _parse_seq(pno):
    m = re.search(r'-(\d{3,5})$', str(pno or ''))
    return int(m.group(1)) if m else 0


def run_for(n, account_name):
    cli = _init_cli(n)
    print(f'\n{"#"*60}')
    print(f'账套: {account_name}  (dbId={cli.db_id})')

    # ── 1. 商品列表（取前50个）——观察 categoryId / categoryName 等字段 ──────────
    print('\n[1] 商品列表字段探测...')
    try:
        r = cli._request('POST', '/jdyscm/product/list',
                         body={'filter': {'page': 1, 'pageSize': 50}},
                         query=cli._api_query(), timeout=15)
        products = r.get('items') or []
        total = r.get('records') or len(products)
        print(f'  商品总数: {total}，本次取: {len(products)}')
        if products:
            p0 = products[0]
            print(f'  字段列表: {list(p0.keys())}')
            print(f'  示例商品 (前3):')
            for p in products[:3]:
                print(f'    {json.dumps(p, ensure_ascii=False)[:400]}')

            # 提取所有分类
            cats = {}
            for p in products:
                cid  = p.get('categoryId') or p.get('productCategoryId') or ''
                cname= p.get('categoryName') or p.get('productCategoryName') or ''
                if cid:
                    cats[cid] = cname
            print(f'\n  从商品列表提取的分类:')
            for cid, cname in sorted(cats.items()):
                print(f'    id={cid}  name={cname}')

            # 各供应商最大序号
            print(f'\n  各供应商最大序号 (基于 {len(products)} 个商品):')
            sup_map = {}
            for p in products:
                pno = p.get('productNumber') or p.get('number') or ''
                sups = p.get('suppliers') or p.get('supplierList') or []
                sname = (p.get('supplierName') or p.get('defaultSupplierName') or '未知').strip()
                if sups and isinstance(sups, list):
                    sname = (sups[0].get('supplierName') or sname)
                seq = _parse_seq(pno)
                if sname not in sup_map:
                    sup_map[sname] = {'max': 0, 'count': 0, 'examples': []}
                if seq > sup_map[sname]['max']:
                    sup_map[sname]['max'] = seq
                sup_map[sname]['count'] += 1
                if len(sup_map[sname]['examples']) < 3:
                    sup_map[sname]['examples'].append(pno)
            for sname, info in sorted(sup_map.items()):
                print(f'    {sname}: max_seq={info["max"]:04d}  count={info["count"]}  e.g.={info["examples"]}')
    except Exception as e:
        print(f'  ❌ {e}')

    # ── 2. 尝试分类专用接口 ───────────────────────────────────────────────────
    print('\n[2] 分类接口探测...')
    cat_eps = [
        '/jdyscm/product/category/list',
        '/jdyscm/category/list',
        '/jdyscm/materialcategory/list',
        '/jdyscm/product/categorylist',
        '/jdyscm/product/getcategorylist',
    ]
    for ep in cat_eps:
        try:
            r = cli._request('POST', ep,
                             body={'filter': {'page': 1, 'pageSize': 200}},
                             query=cli._api_query(), timeout=10)
            items = r.get('items') or r.get('list') or r.get('data') or []
            ec = r.get('errcode') or r.get('code', -1)
            if isinstance(items, list) and items:
                print(f'  ✅ {ep}: {len(items)} 条')
                print(f'     字段: {list(items[0].keys())}')
                for it in items[:5]:
                    print(f'     {json.dumps(it, ensure_ascii=False)[:200]}')
                break
            elif ec == 0:
                print(f'  ⚠️  {ep}: 成功但无数据')
            else:
                print(f'  ❌ {ep}: errcode={ec}')
        except Exception as e:
            print(f'  ❌ {ep}: {type(e).__name__}')

    # ── 3. product/save 接口探测（发空 body，只看错误码） ────────────────────
    print('\n[3] product/save 接口探测...')
    for ep in ['/jdyscm/product/save', '/jdyscm/product/add', '/jdyscm/product/create']:
        try:
            r = cli._request('POST', ep, body={}, query=cli._api_query(), timeout=10)
            ec = r.get('errcode') or r.get('code') or 0
            msg = r.get('errmsg') or r.get('msg') or r.get('description_cn') or r.get('description') or ''
            print(f'  {ep}: errcode={ec} → "{msg[:80]}"')
        except Exception as e:
            print(f'  {ep}: {type(e).__name__}: {e}')

    # ── 4. 供应商列表（看 city 字段） ────────────────────────────────────────
    print('\n[4] 供应商列表（取前10，观察城市字段）...')
    try:
        r = cli._request('POST', '/jdyscm/supplier/list',
                         body={'filter': {'page': 1, 'pageSize': 10}},
                         query=cli._api_query(), timeout=15)
        sups = r.get('items') or []
        if sups:
            print(f'  字段列表: {list(sups[0].keys())}')
            for s in sups[:5]:
                # 只打印关键字段
                key_fields = {k: v for k, v in s.items()
                              if k in ('number', 'name', 'city', 'county', 'address',
                                       'contacts', 'contactList', 'phone', 'region')}
                print(f'  {json.dumps(key_fields, ensure_ascii=False)[:300]}')
    except Exception as e:
        print(f'  ❌ {e}')


if __name__ == '__main__':
    for n, name in [(1, cfg.get('jdy_name', '账套1')), (2, cfg.get('jdy2_name', '账套2'))]:
        run_for(n, name)
    print('\n探测完成。')
    input('按 Enter 关闭...')
