# 待办：同步精斗云客户/商品档案新字段

> 本文件包含两个同步任务，建议一次性完成。

---

# 任务一：客户档案 taxPayerNo（选箱用）

**背景**：分拣批次管理新增了"一键自动选箱"功能。选箱逻辑优先读取精斗云客户档案的
`taxPayerNo`（纳税人识别码）字段，值为 `"1"` 时自动选大箱。
目前该字段**尚未被本地 sales_cache 缓存**，需要在云端同步时一并拉取并落库。

---

## 需要做的事

### 1. 拉取接口

- **接口**：`POST /jdyscm/bd_customer/list`
- **请求 body**（示例）：
  ```json
  {
    "filter": {
      "page": 1,
      "pageSize": 100
    }
  }
  ```
- **目标字段**：`taxPayerNo`（纳税人识别码，string）
- 其余字段（`name`、`number` 等）按现有逻辑保留即可

### 2. 落库位置

写入 `sales_cache.sqlite3` 的**现有 `jdy_customers` 表**（如该表尚不存在，需新建）。

建议表结构（如需新建）：
```sql
CREATE TABLE IF NOT EXISTS jdy_customers (
    number       TEXT PRIMARY KEY,   -- 客户编号
    name         TEXT NOT NULL,      -- 客户名称
    tax_payer_no TEXT DEFAULT '',    -- taxPayerNo（纳税人识别码）
    updated_at   TEXT NOT NULL
);
```

如果已有该表但没有 `tax_payer_no` 列，执行一次迁移：
```sql
ALTER TABLE jdy_customers ADD COLUMN tax_payer_no TEXT DEFAULT '';
```

### 3. 同步时机

随现有销货单/商品同步一起触发即可，不需要单独定时任务。
建议在全量同步或每日增量同步时顺带更新。

---

## 调用方读取方式

`sorting/agent_api.py` 的 `sorting_batch_hints()` 目前直接调 JDY API 实时查客户。
**落库完成后**，可将实时查询改为读本地缓存，速度更快：

```python
# 从 sales_cache 读 tax_payer_no
row = sc.execute(
    "SELECT tax_payer_no FROM jdy_customers WHERE name=? LIMIT 1",
    (customer_name,)
).fetchone()
if row and str(row['tax_payer_no'] or '').strip() == '1':
    # 大箱
```

改完后可以把 `sorting_batch_hints()` 里调 `jdy_cli.get_customer_by_name()` 的部分
替换为上面这段本地查询，并去掉对 `jdy_api` 的依赖。

---

## 验收方式

1. 同步后在 SQLite 里确认：
   ```sql
   SELECT name, tax_payer_no FROM jdy_customers WHERE tax_payer_no != '' LIMIT 20;
   ```
2. 在分拣批次管理页面，点"一键自动选箱"，taxPayerNo=1 的客户订单应显示"大箱"，
   提示来源为 `jdy_pref`（而非 `calc`）。

---

# 任务二：商品档案 brand 字段（分拣上机/手工区分用）

> **状态（2026-06-16 更新）：云端读取侧已完成，JDY 同步写入侧待完成。**
>
> `sorting/agent_api.py` 的 `sorting_batch_plan()` 已实现 brand 读取 + picktype 映射
>（含 `try/except` 容错：`brand` 列不存在时降级为 picktype=0）。
> 剩余工作：让 `server.py` 的商品同步把 brand 字段落库到 `jdy_products`。

**背景**：分拣批次生成时，需要根据每件商品的 `brand` 字段区分"上机"和"手工"两种
分拣方式。目前 JDY 商品同步接口**未拉取该字段**，`jdy_products` 表里也没有该列，
需要补充。

---

## 需要做的事

### 1. 拉取接口

- **接口**：现有商品同步接口（`get_products` / `POST /jdyscm/product/list`）
- **目标字段**：`brand`（品牌，string，值为 `"上机"` 或 `"手工"`）
- 在现有拉取逻辑中，把 `brand` 字段一并取回来

### 2. 落库位置

给 `jdy_products` 表加一列：

```sql
ALTER TABLE jdy_products ADD COLUMN brand TEXT DEFAULT '';
```

> `_ensure_jdy_products_columns()` 函数里已有动态 ALTER TABLE 机制，
> 在 `additions` 列表里加一行 `('brand', 'TEXT')` 即可自动建列（约在 server.py 第 3772 行）。

写入时同步更新：在 `_cache_upsert_jdy_product()` 函数（约第 5746 行）的
INSERT / UPDATE 语句里加入 `brand` 字段，值从 `product.get('brand', '')` 取。

### 3. 同步时机

随现有商品同步一起触发，不需要单独任务。

---

## 调用方读取方式

`sorting/agent_api.py` 的 `sorting_batch_plan()` 在构建商品明细时，需要读取 brand：

```python
prod = sc.execute(
    "SELECT length, width, height, brand FROM jdy_products "
    "WHERE barcode=? OR product_number=? LIMIT 1",
    (barcode, goodsno)
).fetchone()
brand = str(prod['brand'] or '').strip() if prod else ''
# brand == '手工' → picktype=1；brand == '上机' 或空 → picktype=0
picktype = 1 if brand == '手工' else 0
```

---

## 验收方式

1. 同步后在 SQLite 里确认：
   ```sql
   SELECT product_number, product_name, brand
   FROM jdy_products
   WHERE brand != ''
   LIMIT 20;
   ```
2. 确认有 `上机` 和 `手工` 两种值都能查到。
3. 在分拣批次管理页面生成一个普通批次，检查 `cloud_sorting_rules` 表里
   `picktype` 字段是否按 brand 正确填入（手工=1，上机=0）。

---

---

## 附加说明（2026-06-16 补充）

### 已完成的相关工作

- **装箱算法重写**（`sorting/batch_planner.py`）：
  - 移除 `offset=200` 容差，改为硬限制（不超过大箱上限）
  - 不拆分同一 SKU 数量，按订单顺序累积体积
  - 箱型自动选最小合适箱（`_find_box_type`）
  - 默认箱型值更新为 40000 / 85000 / 198000 cm³
- **批次设置 UI**：从大表单改为筛选栏下方紧凑单行条，支持 localStorage 持久化
- **批次详情显示**：`box_configs` 随批次保存并在详情页正确显示，不再硬编码

### JDY 库存数量同步（暂停）

`server.py` 的 `_fetch_sales_quantity_map()` 已临时关闭 JDY API 调用（加早返回）。
后续改从**自有云端**拉取库存数量，届时同时处理此处的恢复逻辑。

*创建于 2026-06-16，对接人：请联系岳进*
