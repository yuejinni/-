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


def _find_box_type(vol: float, box_configs: dict) -> int:
    """
    找能装下 vol 的最小箱型（1/2/3）。
    若所有箱型都装不下，返回最大箱型（3）——仍会分配格口，体积溢出标注。
    """
    for bt in sorted(box_configs.keys()):
        if box_configs[bt] >= vol:
            return bt
    return max(box_configs.keys()) if box_configs else 3


def _emit_box(rules: list, order: dict, goods_list: list,
              port: int, box_num: int, box_type: int) -> None:
    """将 goods_list 中的商品展开为 rules 行（qty=N → slot_seq 1..N）。"""
    box_no    = f"{order['orderno']}-{box_num}"
    innerport = port if port <= 102 else 0
    for g in goods_list:
        floor = floor_from_goodsmodel(g.get('goodsmodel', ''))
        for slot in range(1, g['qty'] + 1):
            rules.append({
                'barcode':    g['barcode'],
                'slot_seq':   slot,
                'portno':     port,
                'innerport':  innerport,
                'floor':      floor,
                'orderno':    order['orderno'],
                'goodsno':    g.get('goodsno', ''),
                'goodsmodel': g.get('goodsmodel', ''),
                'customer':   g.get('customer', ''),
                'box_no':     box_no,
                'box_type':   box_type,
                'serialnum':  g.get('serialnum', 0),
                'picktype':   g.get('picktype', 0),
                'entry_id':   g.get('entry_id', 0),
                # label_data：账号-CTN序号（对应 Wcs_goods.column4）
                'label_data': box_no,
            })


def allocate_ports(orders: list, box_configs: dict) -> list[dict]:
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
                    'l': float,           # 长 cm
                    'w': float,           # 宽 cm
                    'h': float,           # 高 cm
                    'qty': int,           # 数量（整体放入同一箱，不拆分）
                    'serialnum': int,     # 喷码号
                    'picktype': int,      # 0=自动 1=手工
                }
            ],
            'box_type_override': int,     # 可选，手工指定箱型 1/2/3
        }
    ]
    box_configs: {1: small_max_vol, 2: medium_max_vol, 3: large_max_vol}  ← cm³

    返回: list[dict]，每个 SKU qty=N 展开为 N 行（slot_seq 1..N），含 label_data 字段。

    装箱规则：
    - 订单按总数量降序（大单优先，格口号靠前）
    - 新订单开始新格口
    - 按订单内商品顺序依次累积体积（不拆分同一 SKU 的数量）
    - 当前已累积商品 + 本商品 > 当前箱型上限 → 封当前箱，本商品起新箱新格口
      （当前箱型 = 能装下已累积体积的最小箱型，即"装满一箱再开另一箱"）
    - 每箱的箱型 = 能装下该箱合计体积的最小箱型（装不下任何箱则用大箱）
    - 格口 51 跳过
    - 格口 <= 102 → innerport=portno；格口 > 102 → innerport=0（分拨溢出）
    - ⚠️ label_data = "orderno-box_num"（对应 Wcs_goods.column4，账号-CTN序号）
    """
    curr_port  = 0
    rules      = []
    max_large  = max(box_configs.values()) if box_configs else 0
    medium_cap = box_configs.get(2, max_large)

    # 大单优先
    sorted_orders = sorted(
        orders,
        key=lambda o: sum(g['qty'] for g in o['goods']),
        reverse=True
    )

    for order in sorted_orders:
        _override = order.get('box_type_override')
        # 封箱门槛：有指定箱型用对应容量，否则默认中箱容量
        split_cap = box_configs.get(_override, max_large) if _override in (1, 2, 3) else medium_cap

        # ── 阶段 1：按封箱门槛把商品分组成若干箱 ──────────────────────────────
        order_boxes = []   # [{'goods': [...], 'vol': float}, ...]
        for g in order['goods']:
            item_vol = (
                float(g.get('l') or 0) *
                float(g.get('w') or 0) *
                float(g.get('h') or 0) *
                g['qty']
            )
            if order_boxes and split_cap > 0 and order_boxes[-1]['vol'] + item_vol > split_cap:
                order_boxes.append({'goods': [g], 'vol': item_vol})
            else:
                if not order_boxes:
                    order_boxes.append({'goods': [g], 'vol': item_vol})
                else:
                    order_boxes[-1]['goods'].append(g)
                    order_boxes[-1]['vol'] += item_vol

        if not order_boxes:
            continue

        # ── 阶段 2：最后一箱体积 < 中箱 50% → 合并回前一箱 ──────────────────
        if len(order_boxes) >= 2 and order_boxes[-1]['vol'] < medium_cap * 0.5:
            last = order_boxes.pop()
            order_boxes[-1]['goods'].extend(last['goods'])
            order_boxes[-1]['vol'] += last['vol']

        # ── 阶段 3：逐箱 emit ────────────────────────────────────────────────
        curr_port = _next_port(curr_port)   # 新订单 → 新格口
        for box_num, box in enumerate(order_boxes, 1):
            box_type = _override if _override in (1, 2, 3) else _find_box_type(box['vol'], box_configs)
            _emit_box(rules, order, box['goods'], curr_port, box_num, box_type)
            if box_num < len(order_boxes):
                curr_port = _next_port(curr_port)

    return rules
