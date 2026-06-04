# JDY 建档系统 — 项目执行计划

**创建时间**：2026-05-21
**负责人**：Claude Code
**项目目标**：在精斗云（JDY）中实现与 Odoo 快速建档相同的自动化逻辑（AI拍照→解析→生成编码→写入JDY）

---

## 已确认的核心规则

### 商品编码格式
```
{类别字母}.{变换后档口号}{类别字母}-{序号:04d}
示例: C.A12629C-0008
      ↑     ↑↑↑↑↑↑↑↑ ↑  ↑↑↑↑
      分类字母  变换后    分类  序号
```

### 档口号变换规则（取 taxPayerNo 前两位）

| 情况 | 第1位 | 第2位 | 示例 |
|------|-------|-------|------|
| 前两位均为数字 | 数字→字母（1→A, 2→B...9→I, 0→J） | 数字+1（9+1=0） | `102629` → `A12629` |
| 第1位为字母 | 字母→顺序数字（A=1, F=6...） | 数字+1 | `F1111` → `62111` |
| 第1位为字母，第2位为字母 | — | — | ❌ 报错提示 |

### EAN-13 算法（复用 Odoo）
```python
raw12 = f"{date_str_yymmdd}{category_code_2d:02}{seq:04d}"  # 6+2+4 = 12位
check = (10 - (sum(int(raw12[i]) * (1 if i%2==0 else 3) for i in range(12)) % 10)) % 10
ean13 = raw12 + str(check)
```

### 数据源优先级
- 档口号 = JDY 供应商的 `taxPayerNo` 字段
- 分类编码 = 本地 `category_code_map.json`（JDY 分类 ID → letter/code_2d）
- 序号 = 本地 `sequence_counters.json`（按变换后档口号+分类字母维护）

---

## TODO 列表

### 阶段 A：数据准备（数据缓存与分析表生成）

- [ ] **A1** 构建本地产品缓存模块 `jdy_cache.py`
  - 分页拉取两个账套所有商品（祺航饰品 ~30900 个，箱包 N 个）
  - 存入 `_cache/products_acc1.json` / `_cache/products_acc2.json`
  - 增量刷新逻辑：启动时检查 API total count，变了才全量重拉
  - 同步缓存供应商列表 `_cache/suppliers_acc1.json` / `_cache/suppliers_acc2.json`

- [ ] **A2** 生成序号分析表（Excel）
  - 关联：productNumber → defaultSupplierId → taxPayerNo → transformed_code
  - 按 (transformed_code + 分类字母) 分组，取各组最大序号
  - 输出 `序号分析表.xlsx`，列：供应商名 / taxPayerNo / 变换后 / 最大序号 / 你的决定

- [ ] **A3** 用户确认序号分析表
  - 用户在 Excel 中标注"旧/新"
  - 将结果录入 `sequence_counters.json`

- [ ] **A4** 填写 `category_code_map.json`（letter/code_2d）
  - 提供示意格式说明
  - 用户对照 Odoo 分类手动填写

### 阶段 B：供应商大类映射

- [ ] **B1** 生成供应商大类映射表模板 `supplier_category_map.json`
  - 格式：`{transformed_code: {categories: ["A", "B"], note: ""}}`
  - 用途：AI 识别时根据供应商预筛可选分类
  - 提供 Excel 版供用户填写，再导入 JSON

### 阶段 C：核心编码模块

- [ ] **C1** 实现 `code_gen.py`
  - `transform_vendor_code(tax_no)` → 变换后档口号
  - `generate_product_number(cat_letter, transformed, seq)` → 商品编码
  - `generate_ean13(cat_code_2d, seq, date)` → EAN-13
  - `get_next_seq(account, transformed_code, cat_letter)` → 读写 sequence_counters.json

- [ ] **C2** 完善 `jdy_cache.py` 写入接口
  - `create_product(account, product_dict)` → 调 `/jdyscm/product/add`
  - 失败时 46001（编号重复）→ seq+1 重试，最多3次

### 阶段 D：AI 识别与建档 UI

- [ ] **D1** 实现 `ai_helper.py`
  - 接收图片 base64 → 调 Claude claude-sonnet-4-6 API
  - 返回：`{name, category, supplier_code, color, cost}`

- [ ] **D2** 实现 `register_server.py`（Flask 服务，复用现有报关服务框架）
  - `GET  /register`           → 返回建档 HTML 页面
  - `POST /register/preview`   → 接收图片+供应商 → 返回编码预览
  - `POST /register/confirm`   → 确认后写入 JDY
  - `GET  /register/categories` → 返回分类列表（带 code_2d）
  - `GET  /register/suppliers`  → 返回供应商列表（带 transformed_code）

- [ ] **D3** 实现手机端 HTML `templates/register.html`
  - 上传/拍照图片
  - 显示 AI 解析结果 + 编码预览
  - 供应商选择器（含大类过滤）
  - 确认提交

### 阶段 E：沙盒测试

- [ ] **E1** 用测试账套测试 `product/add` 写入
- [ ] **E2** 端对端测试：拍照 → 生成编码 → 写入 JDY → 验证

---

## 当前进度

| 阶段 | 状态 | 说明 |
|------|------|------|
| A1 | ✅ 完成 | `jdy_cache.py` 已实现；供应商缓存已拉取（acc1:896条, acc2:100条）；商品缓存完成（acc1:30900, acc2:22079） |
| A2 | ✅ 完成 | `序号分析表.xlsx` 已生成，acc1找到422个(transformed, cat_letter)组合，acc2因无taxPayerNo为0 |
| A3 | ⏳ 等用户 | 表格生成后用户填写 |
| A4 | ✅ 完成 | `category_code_map.json` 已自动填充：扫描30900个现有产品反推每个分类的 letter；按产品数量降序分配 code_2d 01-99（共99个分类）；29个极低频分类（≤13件）暂留空 |
| B1 | ✅ 完成 | `supplier_category_map.json` + `供应商大类映射表.xlsx` 已生成（acc1:468个, acc2:0个无taxPayerNo） |
| C1 | ✅ 完成 | `code_gen.py` 已实现并测试通过 |
| C2 | ✅ 完成 | `register_product.py` + `jdy_cache.create_product()`；46001 重试逻辑已实现 |
| D1 | ✅ 完成 | `ai_helper.py`：调用千问视觉 API，返回 name/spec/category_hint/cost_hint |
| D2 | ✅ 完成 | `register_server.py`：Flask 5009端口，5个路由（health/categories/suppliers/preview/confirm） |
| D3 | ✅ 完成 | `templates/register.html`：手机端建档页，供应商搜索+分类过滤+拍照+AI预览+确认建档 |
| E1-E2 | 🔴 未开始 | 需沙盒账号 |

### ⚠️ 重要发现
- 祺航箱包账套：100个供应商**全部无 taxPayerNo**，无法用档口号建档体系
  → 需确认：箱包供应商是否需要手动录入 taxPayerNo，或使用其他字段替代

---

## 关键文件位置

| 文件 | 说明 |
|------|------|
| `jdy_register/` | 建档服务目录（本文件所在） |
| `../jdy_api.py` | JDY API 客户端（已有） |
| `../category_code_map.json` | JDY 分类→编码映射（模板已生成，待用户填写） |
| `../sequence_counters.json` | 各分类序号计数器（模板已生成） |
| `_cache/` | 商品/供应商本地缓存 |
| `../../序号分析表.xlsx` | 用户确认序号的分析表 |

---

## 风险与注意事项

| 风险 | 处理方案 |
|------|----------|
| 30,900 商品全量拉取需 5-10 分钟 | 后台线程执行，不阻塞服务启动 |
| 编号重复（46001） | 自动 seq+1 重试，最多3次 |
| 序号并发冲突 | 写文件时加线程锁 |
| 分类编码未填写 | 建档时提示"请先配置 category_code_map.json" |
| 两账套 token 独立 | 各账套独立 JDYClient 实例，已在 jdy_api.py 中实现 |
