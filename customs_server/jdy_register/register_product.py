"""
C2: JDY 商品建档入口
封装: 生成编码 → 写入 JDY → 46001 重复编号自动重试（最多3次）

主要函数:
    register_product(account, tax_no, cat_letter, cat_code_2d,
                     product_name, **kwargs) -> dict

kwargs 支持的额外字段（直接透传给 JDY product/add）:
    unit          单位（如 '个'）
    spec          规格（如 '20*10*5cm'）
    remark        备注
    color         颜色（写入 spec 或 remark）
    cost          成本（不在 product/add 字段，忽略）
    category_id   JDY 分类 ID（优先）
    category_name JDY 分类名称（category_id 为空时用）
"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from code_gen import (
    transform_vendor_code, generate_product_number,
    generate_ean13, get_next_seq, rollback_seq,
)
from jdy_cache import JDYCache

# 编号重复错误码
_ERR_DUPLICATE = 46001

# ── 全局 cache 实例（延迟初始化） ─────────────────────────────────────────────
_cache: JDYCache | None = None


def _get_cache(cfg_path=None) -> JDYCache:
    global _cache
    if _cache is None:
        _cache = JDYCache(cfg_path=cfg_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', '..', 'ai_config.json'
        ))
    return _cache


def _build_payload(product_number: str, ean13: str, product_name: str,
                   kwargs: dict) -> dict:
    """
    组装 /jdyscm/product/add 所需的 body。
    只填 JDY 明确支持的字段，忽略未知字段。
    """
    payload: dict = {
        'productNumber': product_number,
        'productName': product_name,
        'barcode': ean13,
    }

    # 分类（优先 ID）
    cat_id = kwargs.get('category_id')
    cat_name = kwargs.get('category_name')
    if cat_id:
        payload['categoryId'] = str(cat_id)
    elif cat_name:
        payload['categoryName'] = str(cat_name)

    # 可选字段
    for field in ('unit', 'spec', 'remark'):
        val = kwargs.get(field)
        if val is not None and str(val).strip():
            payload[field] = str(val).strip()

    # 供应商 ID（defaultSupplierId）
    sup_id = kwargs.get('supplier_id') or kwargs.get('defaultSupplierId')
    if sup_id:
        payload['defaultSupplierId'] = str(sup_id)

    return payload


def register_product(
    account: str,
    tax_no: str,
    cat_letter: str,
    cat_code_2d: str,
    product_name: str,
    cfg_path: str = None,
    max_retry: int = 3,
    **kwargs,
) -> dict:
    """
    完整建档流程：生成编码 → 写入 JDY → 46001 时 seq+1 重试。

    参数:
        account:      'account1' 或 'account2'
        tax_no:       供应商 taxPayerNo（档口号原始值）
        cat_letter:   分类字母（如 'C'）
        cat_code_2d:  分类两位数字码（如 '01'），用于 EAN-13
        product_name: 商品名称
        max_retry:    最大重试次数（默认 3）
        **kwargs:     unit/spec/remark/category_id/category_name/supplier_id 等

    返回:
        {
            'ok':             True / False,
            'product_number': 生成的商品编码,
            'ean13':          EAN-13 条码,
            'seq':            最终使用的序号,
            'transformed':    变换后档口号,
            'jdy_data':       JDY 返回的 data（成功时）,
            'error':          错误信息（失败时）,
            'attempts':       实际尝试次数,
        }
    """
    # 1. 档口号变换
    transformed, err = transform_vendor_code(tax_no)
    if err:
        return {'ok': False, 'error': f'档口号变换失败: {err}',
                'transformed': transformed, 'attempts': 0}

    cache = _get_cache(cfg_path)

    last_error = None
    for attempt in range(1, max_retry + 2):   # 多1次以确保拿到序号再判断
        # 2. 获取序号（自增）
        seq = get_next_seq(account, transformed, cat_letter)

        # 3. 生成编码
        product_number = generate_product_number(cat_letter, transformed, seq)

        try:
            ean13 = generate_ean13(cat_code_2d, seq)
        except Exception as e:
            rollback_seq(account, transformed, cat_letter)
            return {'ok': False, 'error': f'EAN-13 生成失败: {e}',
                    'transformed': transformed, 'product_number': product_number,
                    'seq': seq, 'attempts': attempt}

        # 4. 写入 JDY
        payload = _build_payload(product_number, ean13, product_name, kwargs)
        print(f'[REGISTER] 尝试 #{attempt}: {product_number} ({product_name})')

        result = cache.create_product(account, payload)

        if result['ok']:
            return {
                'ok': True,
                'product_number': product_number,
                'ean13': ean13,
                'seq': seq,
                'transformed': transformed,
                'jdy_data': result.get('data'),
                'attempts': attempt,
                'error': None,
            }

        errcode = result.get('errcode')
        last_error = result.get('msg', '')

        if errcode == _ERR_DUPLICATE:
            # 编号重复：不回滚（序号已被消耗），继续下一个序号
            print(f'[REGISTER] 编号重复({product_number})，尝试下一序号...')
            if attempt >= max_retry:
                break
            continue

        # 其他错误：回滚序号，立即返回
        rollback_seq(account, transformed, cat_letter)
        return {
            'ok': False,
            'error': f'JDY 返回错误 {errcode}: {last_error}',
            'product_number': product_number,
            'ean13': ean13,
            'seq': seq,
            'transformed': transformed,
            'attempts': attempt,
        }

    # 超过最大重试次数
    return {
        'ok': False,
        'error': f'编号重复，已重试 {max_retry} 次仍失败（最后: {last_error}）',
        'transformed': transformed,
        'attempts': max_retry,
    }


# ── 命令行快速测试 ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # 仅测试编码生成逻辑（不实际调用 JDY API）
    from code_gen import transform_vendor_code, generate_product_number, generate_ean13

    print('=== register_product.py 编码生成测试（不写入 JDY）===\n')

    test_cases = [
        ('102629', 'C', '03', '测试商品A'),
        ('F1111',  'A', '01', '测试商品B'),
        ('103090', 'F', '06', '测试商品C'),
    ]
    for tax_no, cat_letter, cat_code_2d, name in test_cases:
        transformed, err = transform_vendor_code(tax_no)
        if err:
            print(f'  ❌ {tax_no} → 变换失败: {err}')
            continue
        seq = 1   # 测试用固定序号
        pno = generate_product_number(cat_letter, transformed, seq)
        ean = generate_ean13(cat_code_2d, seq)
        print(f'  tax={tax_no!r:10} → transformed={transformed!r:10}  '
              f'pno={pno!r:20}  ean13={ean}')
