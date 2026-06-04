"""
JDY 建档编码生成模块

功能:
  - transform_vendor_code(tax_no)       档口号变换
  - generate_product_number(...)        商品编码生成
  - generate_ean13(cat_code_2d, seq, date) EAN-13 条码
  - get_next_seq(account, transformed, cat_letter) 获取下一序号（线程安全）
  - peek_next_seq(account, transformed, cat_letter) 预览下一序号（不自增）
"""

import json
import os
import re
import threading
from datetime import datetime, date as date_type

# ── 档口号变换规则 ─────────────────────────────────────────────────────────────
#   前两位均为数字：第1位→字母(1→A...0→J)，第2位+1(mod 10)
#   前两位第1为字母：字母→顺序数字(A=1...Z=26)，第2位若数字+1，若字母报错
DIGIT_TO_LETTER = {
    '0': 'J', '1': 'A', '2': 'B', '3': 'C', '4': 'D',
    '5': 'E', '6': 'F', '7': 'G', '8': 'H', '9': 'I',
}
LETTER_TO_DIGIT = {chr(ord('A') + i): str(i + 1) for i in range(26)}


def transform_vendor_code(tax_no: str) -> tuple[str, str | None]:
    """
    变换档口号前两位。
    返回 (transformed, error_msg)
    error_msg 为 None 表示成功。
    """
    s = str(tax_no or '').strip().upper()
    if len(s) < 2:
        return s, '档口号长度不足2位'

    c1, c2, rest = s[0], s[1], s[2:]

    if not c1.isalpha() and not c2.isalpha():
        # 前两位均为数字
        nc1 = DIGIT_TO_LETTER.get(c1, c1)
        nc2 = str((int(c2) + 1) % 10)
        return nc1 + nc2 + rest, None

    elif c1.isalpha():
        # 第1位是字母
        nc1 = LETTER_TO_DIGIT.get(c1.upper(), '?')
        if c2.isdigit():
            nc2 = str((int(c2) + 1) % 10)
            return nc1 + nc2 + rest, None
        else:
            return s, f'第二位是字母({c2})，无法处理，请手动设置'

    else:
        # 第1位数字、第2位字母：边界情况，暂报错
        return s, f'首位数字+次位字母({s[:2]})，请手动设置'


def generate_product_number(cat_letter: str, transformed: str, seq: int) -> str:
    """
    生成商品编码
    格式: {cat_letter}.{transformed}{cat_letter}-{seq:04d}
    示例: C.A12629C-0008
    """
    return f'{cat_letter.upper()}.{transformed.upper()}{cat_letter.upper()}-{seq:04d}'


def generate_ean13(cat_code_2d: str, seq: int, dt: datetime | date_type | None = None) -> str:
    """
    生成 EAN-13 条码（复用 Odoo 算法）
    cat_code_2d: 2位数字字符串，如 '01'
    seq: 序号整数
    dt: 日期（默认今天）
    """
    if dt is None:
        dt = datetime.now()
    if isinstance(dt, date_type) and not isinstance(dt, datetime):
        date_str = dt.strftime('%y%m%d')
    else:
        date_str = dt.strftime('%y%m%d')

    raw12 = f'{date_str}{str(cat_code_2d).zfill(2)}{seq:04d}'
    if len(raw12) != 12:
        raise ValueError(f'EAN-13 raw12 长度异常: {raw12!r}')

    total = sum(
        int(raw12[i]) * (1 if i % 2 == 0 else 3)
        for i in range(12)
    )
    check = (10 - (total % 10)) % 10
    return raw12 + str(check)


# ── 序号计数器（线程安全） ────────────────────────────────────────────────────
_COUNTERS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'sequence_counters.json'
)
_counters_lock = threading.Lock()


def _load_counters() -> dict:
    try:
        if os.path.exists(_COUNTERS_FILE):
            with open(_COUNTERS_FILE, encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {'account1': {}, 'account2': {}}


def _save_counters(data: dict):
    tmp = _COUNTERS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _COUNTERS_FILE)


def _counter_key(transformed: str, cat_letter: str) -> str:
    """计数器的 key = 变换后档口号 + 分类字母"""
    return f'{transformed.upper()}_{cat_letter.upper()}'


def peek_next_seq(account: str, transformed: str, cat_letter: str) -> int:
    """预览下一个序号（不自增，不写文件）"""
    with _counters_lock:
        data = _load_counters()
        acc_data = data.get(account, {})
        key = _counter_key(transformed, cat_letter)
        # key 可能是旧格式 {"current": N} 或直接 N
        val = acc_data.get(key)
        if isinstance(val, dict):
            current = val.get('current', 0)
        elif isinstance(val, int):
            current = val
        else:
            current = 0
        return current + 1


def get_next_seq(account: str, transformed: str, cat_letter: str) -> int:
    """
    获取并自增序号（线程安全）。
    返回本次应使用的序号（从1开始）。
    """
    with _counters_lock:
        data = _load_counters()
        if account not in data:
            data[account] = {}
        acc_data = data[account]
        key = _counter_key(transformed, cat_letter)

        val = acc_data.get(key)
        if isinstance(val, dict):
            current = val.get('current', 0)
        elif isinstance(val, int):
            current = val
        else:
            current = 0

        next_seq = current + 1
        acc_data[key] = {'current': next_seq, 'label': f'{transformed}_{cat_letter}'}
        _save_counters(data)
        return next_seq


def rollback_seq(account: str, transformed: str, cat_letter: str):
    """回滚序号（创建失败时调用）"""
    with _counters_lock:
        data = _load_counters()
        acc_data = data.get(account, {})
        key = _counter_key(transformed, cat_letter)
        val = acc_data.get(key)
        if isinstance(val, dict):
            current = val.get('current', 0)
        elif isinstance(val, int):
            current = val
        else:
            current = 0
        if current > 0:
            acc_data[key] = {'current': current - 1, 'label': f'{transformed}_{cat_letter}'}
            _save_counters(data)


def set_initial_seq(account: str, transformed: str, cat_letter: str, initial: int):
    """手动设置某个key的初始序号（从分析表读取后写入）"""
    with _counters_lock:
        data = _load_counters()
        if account not in data:
            data[account] = {}
        key = _counter_key(transformed, cat_letter)
        data[account][key] = {'current': initial, 'label': f'{transformed}_{cat_letter}'}
        _save_counters(data)


# ── 完整建档参数计算 ──────────────────────────────────────────────────────────
def build_product_code(account: str, tax_no: str, cat_letter: str, cat_code_2d: str,
                       dt: datetime | date_type | None = None) -> dict:
    """
    一步生成建档所需的编码信息。
    返回:
    {
      'transformed': 变换后档口号,
      'product_number': 商品编码,
      'ean13': EAN-13,
      'seq': 序号,
      'error': None 或错误描述,
    }
    """
    transformed, err = transform_vendor_code(tax_no)
    if err:
        return {'error': err, 'transformed': transformed}

    seq = get_next_seq(account, transformed, cat_letter)
    product_number = generate_product_number(cat_letter, transformed, seq)

    try:
        ean13 = generate_ean13(cat_code_2d, seq, dt)
    except Exception as e:
        rollback_seq(account, transformed, cat_letter)
        return {'error': f'EAN-13 生成失败: {e}', 'transformed': transformed}

    return {
        'transformed': transformed,
        'product_number': product_number,
        'ean13': ean13,
        'seq': seq,
        'error': None,
    }


# ── 快速测试 ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    test_cases = [
        ('102629', 'C120', None),
        ('102080', 'A1080', None),
        ('F1111',  '62111', None),
        ('211060', 'B21060', None),
        ('3020',   'C120', None),
        ('AB123',  '?',    '期望报错'),
    ]

    print('档口号变换测试:')
    for tax_no, expected, note in test_cases:
        result, err = transform_vendor_code(tax_no)
        status = '✅' if (err is None) == (note is None) else '❌'
        print(f'  {status} {tax_no!r:12} → {result!r:12}  err={err}  {note or ""}')

    print('\nEAN-13 测试:')
    from datetime import date
    ean = generate_ean13('01', 37, date(2025, 5, 1))
    print(f'  cat=01, seq=37, date=250501 → {ean}  (长度={len(ean)})')

    print('\n商品编码测试:')
    pno = generate_product_number('C', 'A12629', 8)
    print(f'  cat=C, transformed=A12629, seq=8 → {pno}')
