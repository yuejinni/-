"""
test_phase2.py — 阶段二功能测试脚本（不需要真实 PDF）

用法：
  python3 test_phase2.py  LK00022  /path/to/output

测试流程：
  1. 读取已有的 {invoice_no}_items.json
  2. 用 items.json 中的 HS 编码和单位构造 mock PDF 数据（模拟 PDF 解析结果）
  3. 调用阶段二的三个生成函数
  4. 打印结果路径

说明：
  mock PDF 数据会用 items.json 中已有的单位，等效于"PDF 和报关单完全一致"的情况。
  若要模拟"PDF 单位与基础资料不同"，可在脚本末尾手动修改 mock_hs_data。
"""
import sys
import os
import json

# 加入 customs_server 目录
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from excel_gen import (
    generate_invoice_import,
    update_base_data_units,
    generate_linghang_invoices,
)


def build_mock_hs_data(items_json_path):
    """
    从 items.json 构造 mock pdf_hs_data：
    按 hs_code 聚合 qty_original 和 unit，模拟 PDF 解析结果。
    """
    with open(items_json_path, encoding='utf-8') as f:
        saved = json.load(f)

    hs_data = {}
    for item in saved.get('items', []):
        hs = item.get('hs_code', '')
        if not hs:
            continue
        pcs_per_pkg = float(item.get('pcs_per_pkg', 1) or 1)
        qty_orig = float(item.get('qty', 0)) / pcs_per_pkg
        unit = item.get('unit', 'PCS')

        if hs not in hs_data:
            hs_data[hs] = {'unit': unit, 'qty': 0.0, 'amount_usd': 0.0}
        hs_data[hs]['qty'] += qty_orig

    return hs_data


def build_mock_supplier_map(items_json_path):
    """构造简单的 supplier_map，所有商品归入'测试供应商'。"""
    with open(items_json_path, encoding='utf-8') as f:
        saved = json.load(f)
    return {
        item['code']: {'supplierName': '测试供应商', 'supplierIdx': 1}
        for item in saved.get('items', [])
    }


def main():
    if len(sys.argv) < 3:
        print("用法: python3 test_phase2.py <invoice_no> <output_path>")
        print("例如: python3 test_phase2.py LK00022 /Users/yuejin/Desktop/test_output")
        sys.exit(1)

    invoice_no  = sys.argv[1]
    output_path = sys.argv[2]

    items_json_path = os.path.join(output_path, f'{invoice_no}_items.json')
    if not os.path.exists(items_json_path):
        print(f"❌ 找不到 {items_json_path}")
        print("请先运行一次生成报关单（/generate），会自动创建该文件。")
        sys.exit(1)

    os.makedirs(output_path, exist_ok=True)

    # 构造 mock 数据
    mock_hs_data    = build_mock_hs_data(items_json_path)
    mock_supplier   = build_mock_supplier_map(items_json_path)

    print(f"\n📋 invoice_no:  {invoice_no}")
    print(f"📁 output_path: {output_path}")
    print(f"🔑 mock HS 数据 ({len(mock_hs_data)} 个 HS 编码):")
    for hs, d in mock_hs_data.items():
        print(f"   {hs}  qty={d['qty']:.4f}  unit={d['unit']}")

    # ── 若要自定义 mock HS 数据，在这里修改 mock_hs_data ──────────────────
    # 例如：模拟 PDF 中某个 HS 编码的单位与基础资料不同
    # mock_hs_data['7117190000']['unit'] = '克'
    # ──────────────────────────────────────────────────────────────────────

    print("\n▶ 生成发票开具项目信息导入模板…")
    try:
        path = generate_invoice_import(invoice_no, mock_hs_data, items_json_path, output_path)
        print(f"  ✅ {path}")
    except Exception as e:
        print(f"  ❌ {e}")

    print("\n▶ 检查基础资料 G列差异（使用 mock 数据，通常无差异）…")
    try:
        path = update_base_data_units(invoice_no, mock_hs_data, items_json_path, output_path)
        print(f"  ✅ {path}")
    except Exception as e:
        print(f"  ❌ {e}")

    print("\n▶ 生成凌航发票…")
    try:
        paths = generate_linghang_invoices(
            invoice_no, mock_hs_data, mock_supplier, items_json_path, output_path)
        for p in paths:
            print(f"  ✅ {p}")
    except Exception as e:
        print(f"  ❌ {e}")

    print("\n完成。")


if __name__ == '__main__':
    main()
