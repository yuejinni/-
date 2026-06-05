"""
JDY 建档编码生成模块

功能:
  - transform_vendor_code(tax_no)            档口号变换
  - generate_product_number(prefix, transformed, small_code, seq)
                                             商品编码生成（新格式）
  - generate_ean13(code_2d, seq, date)       EAN-13 条码
  - get_next_seq(account)                    获取账套全局下一序号（线程安全）
  - peek_next_seq(account)                   预览下一序号（不自增）
  - rollback_seq(account)                    回滚全局序号

全局序号说明:
  每个账套只有一个全局计数器（key='global'），所有品类+供应商共用。
  sequence_counters.json 结构:
    {"account1": {"global": {"current": 5}}, "account2": {"global": {"current": 3}}}
  旧格式（per-category）数据保留不清除，新产品只使用 global 计数器。
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


def generate_product_number(prefix: str, transformed: str, small_code: str, seq: int) -> str:
    """
    生成商品编码（新格式）
    格式: {prefix}.{transformed}{small_code:02d}-{seq:04d}
    示例: C.A1262902-0001  （prefix=C, transformed=A12629, small_code=02, seq=1）
    示例: LF.A1262901-0001 （prefix=LF, transformed=A12629, small_code=01, seq=1）

    参数:
        prefix:     产品编号点前部分（中类字母，如 'C' 或 'LF'）
        transformed: 变换后档口号（如 'A12629'）
        small_code: Excel 小类顺序码（如 '02'）
        seq:        全局序号整数
    """
    return f'{prefix.upper()}.{transformed.upper()}{str(small_code).zfill(2)}-{seq:04d}'


def generate_ean13(cat_code_2d: str, seq: int, dt: datetime | date_type | None = None) -> str:
    """
    生成 EAN-13 条码（复用 Odoo 算法）
    cat_code_2d: 2位数字字符串，如 '01'（EAN 专用码，全局唯一）
    seq: 序号整数（全局序号）
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

_GLOBAL_KEY = 'global'


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


def _get_global_current(acc_data: dict) -> int:
    """从账套数据中读取 global 计数器当前值"""
    val = acc_data.get(_GLOBAL_KEY)
    if isinstance(val, dict):
        return val.get('current', 0)
    elif isinstance(val, int):
        return val
    return 0


def peek_next_seq(account: str) -> int:
    """预览账套全局下一个序号（不自增，不写文件）"""
    with _counters_lock:
        data = _load_counters()
        acc_data = data.get(account, {})
        return _get_global_current(acc_data) + 1


def get_next_seq(account: str) -> int:
    """
    获取并自增账套全局序号（线程安全）。
    返回本次应使用的序号（从1开始）。
    """
    with _counters_lock:
        data = _load_counters()
        if account not in data:
            data[account] = {}
        acc_data = data[account]
        current = _get_global_current(acc_data)
        next_seq = current + 1
        acc_data[_GLOBAL_KEY] = {'current': next_seq}
        _save_counters(data)
        return next_seq


def rollback_seq(account: str):
    """回滚账套全局序号（创建失败时调用）"""
    with _counters_lock:
        data = _load_counters()
        acc_data = data.get(account, {})
        current = _get_global_current(acc_data)
        if current > 0:
            acc_data[_GLOBAL_KEY] = {'current': current - 1}
            _save_counters(data)


def set_global_seq(account: str, value: int):
    """手动设置账套全局序号（初始化或修正时使用）"""
    with _counters_lock:
        data = _load_counters()
        if account not in data:
            data[account] = {}
        data[account][_GLOBAL_KEY] = {'current': value}
        _save_counters(data)
        print(f'[code_gen] {account} global seq set to {value}')


def set_initial_seq(account: str, value: int):
    """set_global_seq 的别名，保持兼容"""
    set_global_seq(account, value)


# ── 完整建档参数计算 ──────────────────────────────────────────────────────────
def build_product_code(account: str, tax_no: str, prefix: str, small_code: str,
                       cat_code_2d: str, dt: datetime | date_type | None = None) -> dict:
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

    seq = get_next_seq(account)
    product_number = generate_product_number(prefix, transformed, small_code, seq)

    try:
        ean13 = generate_ean13(cat_code_2d, seq, dt)
    except Exception as e:
        rollback_seq(account)
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

    print('\n商品编码测试（新格式）:')
    pno = generate_product_number('C', 'A12629', '02', 1)
    print(f'  prefix=C, transformed=A12629, small_code=02, seq=1 → {pno}')
    pno2 = generate_product_number('LF', 'A12629', '01', 1)
    print(f'  prefix=LF, transformed=A12629, small_code=01, seq=1 → {pno2}')
