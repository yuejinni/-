"""
JDY 单位管理模块

功能:
  - 按账套缓存基础单位 ID（默认 PAC）
  - 单位初始化：从 JDY 拉取单位列表，识别 PAC/PCS/件 等名称

缓存文件: jdy_register/_unit_cache.json（不提交 git）
缓存格式:
{
  "account1": {
    "units": {"PAC": "12961923739132232", "件": "12961923739132136", ...}
  },
  "account2": {...}
}

使用方式:
  实际业务：只有单一单位 PAC，每件产品备注里写 1PAC=N件。
  无需多计量单位组（unitType）。

使用方式:
    um = UnitManager(cache, cfg_path=_CFG_PATH)
    um.init_base_units('account1')       # 首次启动时扫描基础单位

    # 获取 PCS 单位 ID
    pcs_id = um.get_pcs_unit_id('account1')

    # 获取/创建 PCS+箱 单位组（pcs_per_ctn=120）
    info = um.get_or_create_unit_group('account1', pcs_per_ctn=120)
    # info = {'typeId': '...', 'PCS': '...', 'BOX': '...'}
    # product/add 时:
    #   unit = info['PCS']
    #   unitType = info['typeId']
"""

import json
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jdy_cache import JDYCache

_UNIT_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '_unit_cache.json')
_lock = threading.Lock()

# 已知 JDY 默认单位名称变体（PAC 优先，兼容 PCS/件/个）
_PAC_NAMES = {'pac', 'pack', 'pacs', 'packs'}
_PCS_NAMES = {'pcs', 'piece', 'pieces', '件', '个', '粒', '只'}


class UnitManager:
    def __init__(self, cache: 'JDYCache', cfg_path: str = None):
        self._cache = cache
        self._cfg_path = cfg_path
        self._data: dict = self._load()

    def _load(self) -> dict:
        try:
            if os.path.exists(_UNIT_CACHE_FILE):
                with open(_UNIT_CACHE_FILE, encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save(self):
        tmp = _UNIT_CACHE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _UNIT_CACHE_FILE)

    def _acc(self, account: str) -> dict:
        if account not in self._data:
            self._data[account] = {'units': {}, 'groups': {}}
        return self._data[account]

    # ── 基础单位 ─────────────────────────────────────────────────────────────

    def init_base_units(self, account: str, force: bool = False) -> dict:
        """
        从 JDY 拉取单位列表，识别 PCS / BOX 并缓存。
        返回 {'PCS': id, 'BOX': id, ...}
        """
        with _lock:
            acc = self._acc(account)
            if acc['units'] and not force:
                return acc['units']

            cli = self._cache._get_client(account)
            try:
                result = cli._request('POST', '/jdyscm/unit/list',
                                      body={'filter': {'page': 1, 'pageSize': 200}},
                                      query=cli._api_query(), timeout=10)
                items = result.get('items') or []
            except Exception as e:
                print(f'[UnitManager] {account} 拉取单位失败: {e}')
                return acc.get('units', {})

            units = {}
            for item in items:
                name_raw = str(item.get('name') or '').strip()
                uid = str(item.get('id') or '')
                if not uid:
                    continue
                name_lower = name_raw.lower()
                # PAC 优先（系统默认单位）
                if name_lower in _PAC_NAMES:
                    units['PAC'] = uid
                    print(f'[UnitManager] {account} PAC 单位 ID: {uid} (name={name_raw!r})')
                elif name_lower in _PCS_NAMES:
                    # 没有 PAC 时的备用
                    units.setdefault('PAC', uid)
                    print(f'[UnitManager] {account} PCS/件 单位 ID: {uid} (name={name_raw!r})')
                # 同时存原始名称
                units[name_raw] = uid

            acc['units'] = units
            self._save()
            return units

    def get_pcs_unit_id(self, account: str) -> str | None:
        """返回 PAC 单位 ID（系统默认单位）。若未初始化则先拉取。"""
        with _lock:
            acc = self._acc(account)
            uid = acc.get('units', {}).get('PAC')
        if not uid:
            units = self.init_base_units(account)
            uid = units.get('PAC')
        return uid

    def get_pac_unit_id(self, account: str) -> str | None:
        """get_pcs_unit_id 的别名，语义更清晰"""
        return self.get_pcs_unit_id(account)

    def get_unit_id(self, account: str, name: str) -> str | None:
        """按名称查单位 ID"""
        with _lock:
            acc = self._acc(account)
            return acc.get('units', {}).get(name)

    # ── 多计量单位组 ─────────────────────────────────────────────────────────

    def get_or_create_unit_group(self, account: str,
                                  pcs_per_ctn: int) -> dict | None:
        """
        查询或创建 PCS+箱 多计量单位组。

        返回:
            {'typeId': '...', 'PCS': '...', 'BOX': '...'} 成功时
            None  创建失败时
        """
        if not pcs_per_ctn or pcs_per_ctn <= 0:
            return None

        group_key = f'1:{pcs_per_ctn}'

        with _lock:
            acc = self._acc(account)
            existing = acc.get('groups', {}).get(group_key)
        if existing:
            return existing

        # 需要创建
        pcs_id = self.get_pcs_unit_id(account)
        if not pcs_id:
            print(f'[UnitManager] {account} 未找到 PCS 单位，无法创建单位组')
            return None

        cli = self._cache._get_client(account)
        group_name = f'PCS+箱(1:{pcs_per_ctn})'
        try:
            result = cli._request('POST', '/jdyscm/unit/add',
                                   body={'units': [{
                                       'name': group_name,
                                       'entry': [
                                           {'name': 'PCS', 'rate': 1, 'default': True},
                                           {'name': '箱', 'rate': pcs_per_ctn, 'default': False},
                                       ]
                                   }]},
                                   query=cli._api_query(), timeout=10)
        except Exception as e:
            print(f'[UnitManager] {account} 创建单位组失败: {e}')
            return None

        errcode = result.get('errcode') or result.get('code') or 0
        if errcode and errcode != 0:
            print(f'[UnitManager] {account} 创建单位组失败 errcode={errcode}: '
                  f'{result.get("msg", "")}')
            return None

        data = result.get('data') or result
        # 解析返回的 typeId 和 PCS/BOX unitId
        type_id = ''
        new_pcs_id = pcs_id  # 多计量中的 PCS entry unitId
        new_box_id = ''

        if isinstance(data, dict):
            type_id = str(data.get('id') or data.get('typeId') or '')
            for entry in data.get('entry') or data.get('units') or []:
                n = str(entry.get('name') or '').strip()
                eid = str(entry.get('id') or '')
                if n == 'PCS' and eid:
                    new_pcs_id = eid
                elif n == '箱' and eid:
                    new_box_id = eid

        if not type_id:
            print(f'[UnitManager] {account} 创建单位组返回无 typeId: {data}')
            return None

        info = {'typeId': type_id, 'PCS': new_pcs_id, 'BOX': new_box_id}
        with _lock:
            acc = self._acc(account)
            acc.setdefault('groups', {})[group_key] = info
            self._save()

        print(f'[UnitManager] {account} 创建单位组 {group_name!r} → typeId={type_id}')
        return info

    def get_units_for_product(self, account: str,
                               pcs_per_ctn: int) -> dict:
        """
        返回 product/add 所需的单位字段。

        pcs_per_ctn > 0 → 多计量单位组
        pcs_per_ctn = 0 → 仅 PCS 单位

        返回:
        {
            'unit':     PCS 单位 ID,        # product/add unit 字段（必填）
            'unitType': 单位组 ID 或 None,   # product/add unitType 字段（多计量时）
            'box_unit': 箱单位 ID 或 None,   # purchaseOrder entry unit 字段（按箱采购时）
        }
        """
        pcs_id = self.get_pcs_unit_id(account) or ''
        result = {'unit': pcs_id, 'unitType': None, 'box_unit': None}

        if pcs_per_ctn and pcs_per_ctn > 0:
            group = self.get_or_create_unit_group(account, pcs_per_ctn)
            if group:
                result['unitType'] = group.get('typeId')
                result['unit'] = group.get('PCS') or pcs_id
                result['box_unit'] = group.get('BOX')

        return result

    # ── 购货订单单位推断 ──────────────────────────────────────────────────────

    def infer_order_unit(self, account: str,
                          qty: int, pcs_per_ctn: int) -> dict:
        """
        根据 AI 识别的 qty 和 pcs_per_ctn 推断购货订单中使用的单位和数量。

        逻辑:
          - pcs_per_ctn > 0 且 qty * pcs_per_ctn 更像是件数 → 用箱单位，qty = 箱数
          - 否则用 PCS 单位，qty = 件数

        注意: AI 应直接给出箱数（ctn_qty）和件数（qty），由前端决定传哪个。
              这里仅做推断兜底。

        返回: {'unit_id': '...', 'qty': N}
        """
        pcs_id = self.get_pcs_unit_id(account) or ''
        box_unit = None

        if pcs_per_ctn and pcs_per_ctn > 0:
            group = self.get_or_create_unit_group(account, pcs_per_ctn)
            if group:
                box_unit = group.get('BOX')

        # 如果 qty 像是箱数（小数字）且有箱单位，用箱
        if box_unit and qty and 0 < qty <= 500 and pcs_per_ctn > 0:
            return {'unit_id': box_unit, 'qty': qty}

        return {'unit_id': pcs_id, 'qty': qty}

    def get_all_units(self, account: str) -> list[dict]:
        """返回该账套所有已知单位列表 [{unitId, unitName}]"""
        with _lock:
            acc = self._acc(account)
            units_raw = acc.get('units', {})
        result = []
        seen_ids = set()
        for name, uid in units_raw.items():
            if uid and uid not in seen_ids:
                seen_ids.add(uid)
                result.append({'unitId': uid, 'unitName': name})
        result.sort(key=lambda x: x['unitName'])
        return result
