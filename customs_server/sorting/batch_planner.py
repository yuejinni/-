"""
sorting/batch_planner.py — 分拣批次规划与分箱算法

替代原存储过程：
  Proc_Importordergoodsinfo + Proc_Producttoport + Proc_Sendsorting

在云端（customs_server）运行，生成 sorting_rules 批次供本地 Agent 同步。
"""
def floor_from_goodsmodel(goodsmodel: str) -> int:
    """楼层推算（goodsmodel 第二段首字母，对应原系统 Proc_Sendsorting IF/ELSE 逻辑）。"""
    _FLOOR_MAP = {}
    for c in 'ABCDabcd': _FLOOR_MAP[c] = 1
    for c in 'EFGefg':   _FLOOR_MAP[c] = 2
    for c in 'HIJhij':   _FLOOR_MAP[c] = 3
    for c in 'KLMNklmn': _FLOOR_MAP[c] = 4
    segs = (goodsmodel or '').split()
    if len(segs) < 2:
        return 0
    return _FLOOR_MAP.get(segs[1][0], 0)


def _next_port(p: int) -> int:
    """格口号递增，跳过格口 51（PLC 程序中 51 已注释）。"""
    p += 1
    return p + 1 if p == 51 else p


def allocate_ports(orders: list, box_configs: dict, offset: int = 200) -> list[dict]:
    """
    批次分箱分格口算法（替代 Proc_Importordergoodsinfo + Proc_Producttoport）。

    orders: [
        {
            'orderno': str,
            'goods': [
                {
                    'barcode': str,       # JDY 产品条码
                    'goodsno': str,       # 货号
                    'goodsmodel': str,    # 规格（用于推算楼层）
                    'customer': str,      # 客户名
                    'l': float,           # 长 cm（jdy_products.length）
                    'w': float,           # 宽 cm
                    'h': float,           # 高 cm
                    'qty': int,           # 数量
                    'serialnum': int,     # 喷码号（导入时 @synid）
                    'picktype': int,      # 0=自动 1=手工（对应 column3）
                }
            ]
        }
    ]
    box_configs: {1: small_max_vol, 2: medium_max_vol, 3: large_max_vol}  ← cm³
    offset: 体积容差（超出上限+offset 才换格口，默认200）

    返回: list[dict]，每个 SKU qty=N 展开为 N 行（slot_seq 1..N），含 label_data 字段。

    关键规则（来自原系统 script.sql）：
    - 订单按总数量降序（大单优先，格口号靠前）
    - 新订单开始新格口
    - 体积超出当前箱型上限+offset → 换格口（box_num++）
    - 当前箱装不下但 total_vol <= max_vol*1.5 → 箱型升级
    - 格口 51 跳过
    - 格口 <= 102 → innerport=portno；格口 > 102 → innerport=0（分拨溢出）
    - ⚠️ label_data = "orderno-box_num"（对应 Wcs_goods.column4，账号-CTN序号）
    """
    curr_port = 0
    rules = []

    # 大单优先
    sorted_orders = sorted(
        orders,
        key=lambda o: sum(g['qty'] for g in o['goods']),
        reverse=True
    )

    for order in sorted_orders:
        curr_port = _next_port(curr_port)   # 新订单 → 新格口
        box_num, box_type, curr_vol = 1, 1, 0
        total_vol = sum(
            float(g.get('l') or 0) * float(g.get('w') or 0) * float(g.get('h') or 0) * g['qty']
            for g in order['goods']
        )

        for g in order['goods']:
            item_vol = (
                float(g.get('l') or 0) *
                float(g.get('w') or 0) *
                float(g.get('h') or 0) *
                g['qty']
            )
            max_vol = box_configs.get(box_type, 0)

            if max_vol > 0 and curr_vol + item_vol > max_vol + offset:
                curr_port = _next_port(curr_port)   # 体积超限 → 新箱 → 新格口
                box_num += 1
                curr_vol = item_vol
                # 箱型升级条件
                if max_vol < total_vol <= max_vol * 1.5:
                    box_type = min(box_type + 1, 3)
            else:
                curr_vol += item_vol

            innerport = curr_port if curr_port <= 102 else 0
            floor     = floor_from_goodsmodel(g.get('goodsmodel', ''))
            box_no    = f"{order['orderno']}-{box_num}"

            # ⚠️ 1:N 展开：qty=N → N 行（slot_seq 1..N）
            for slot in range(1, g['qty'] + 1):
                rules.append({
                    'barcode':   g['barcode'],
                    'slot_seq':  slot,
                    'portno':    curr_port,
                    'innerport': innerport,
                    'floor':     floor,
                    'orderno':   order['orderno'],
                    'goodsno':   g.get('goodsno', ''),
                    'goodsmodel': g.get('goodsmodel', ''),
                    'customer':  g.get('customer', ''),
                    'box_no':    box_no,
                    'box_type':  box_type,
                    'serialnum': g.get('serialnum', 0),
                    'picktype':  g.get('picktype', 0),
                    # ⚠️ label_data：账号-CTN序号（对应 Wcs_goods.column4）
                    'label_data': box_no,
                })

    return rules
