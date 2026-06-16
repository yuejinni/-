# 待办：同步精斗云客户档案 taxPayerNo 字段

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

*创建于 2026-06-16，对接人：请联系岳进*
