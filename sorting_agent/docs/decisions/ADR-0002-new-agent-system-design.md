# ADR-0002：Python Agent 新系统方案设计

## 背景

原系统由三部分组成：
- `yc_line_wcs`：C# WinForms 程序（PLC控制 + TCP扫码枪）
- SQL Server `EmsSort` + **15条**存储过程（分拣核心业务逻辑）
- `EMSAPI`：C# ASP.NET Web API（PDA拣货接口）

新方案：Python Agent 替代全部 C# 部分，业务逻辑从 SQL Server 存储过程迁移到 Python。
数据库沿用 **SQL Server**（同实例，新建独立数据库 `SortingAgent`），与原 `EmsSort` 隔离，不共享表。

> **数据来源**：`script.sql`（2026-06-12 从 EmsSort 完整导出，UTF-16LE 编码，1598行）

---

## ⚠️ 重要修正（基于 script.sql 完整分析）

以下内容是第一版分析的错误或遗漏，已在本文档中修正：

| 项目 | 原分析 | 修正后 |
|------|--------|--------|
| 存储过程总数 | 11条 | **15条**（另有 AutoUpdatePort、GetManualportinfo、PortStatus、UpdatePortRemark、Truncatesorting） |
| 格口表名 | `Port_table` | **`Wcs_port`**（字段：PortNo/InitNum/FJNum/Remark/IsEnable） |
| PLC写入用哪个格口字段 | `portno` | **`innerport`**（port>102的溢出包裹 innerport=0，等分配后才有值） |
| serialnum 来源 | 模糊 | **从 @synid（导入时传入）→ Wcs_goods.synno → Wcs_iparcel.serialnum** |
| 导入操作是否增量 | 增量 | **每次调用 Proc_Importordergoodsinfo 都先 TRUNCATE Wcs_iparcel 和 Wcs_pick** |
| 分箱体积容差 | 无 | **@offset=200**（体积超出箱型上限+200才换格口） |
| 格口空闲判断 | isused=0 | **InitNum=FJNum 且 InitNum!=0**（全部落包才算满，再由按钮触发清空） |
| 超时告警 | 无 | **ModifiedDate < 40分钟前 且 FJNum!=0 且 InitNum!=FJNum**（Proc_PortStatus） |
| 手工分拣 | 无此功能 | **Proc_GetManualportinfo**（DWS台称扫描，带尺寸重量，column3=1的商品专用） |
| 已发现新表 | — | **Wcs_batch**（批次）、**Wcs_dws**（扫描台）、**Wcs_tczyb**（操作员） |
| 楼层来源 | 商品编号首字母 | **goodsmodel 第二段首字母**（如 `"RED E12"` → 第二段 `"E12"` → 首字母 `'E'` → 楼层2）；与 product_number 无关 |
| synno 并发碰撞 | 未发现 | **同一秒内两次扫码 synno 相同**（`'1'+HHMMSS+'1'` 精度只到秒）；新系统加毫秒或线程安全计数器 |
| Wcs_ngoods 1:N 展开 | 一条码一行 | **quantity=N 时 Proc_Producttoport 创建 N 行 Wcs_ngoods**；sorting_rules 需含 slot_seq，不可用 UNIQUE(batchno,barcode) |
| 拣货进度表缺失 | 无 | **需对应 Wcs_pick**（barcode/num/anum）；PDA 每次扫码 anum++ 并检查 anum<num 才允许继续拣 |
| 统计计数器缺失 | 无 | **原 C# 内存维护 total_num / day_num / day_ng_num**；新系统需落库（sys_config 或 sort_stats 表） |
| DWS 尺寸来源变化 | 无 | **原 Proc_GetManualportinfo 从 DWS 台称实时读 length/width/height**；新系统改用静态值 `jdy_products.length/width/height` |
| 回滚范围 | 有条件 | **Proc_Rollbacksorting 分两段**：① Wcs_order/Wcs_goods/Wcs_ngoods 只改 isenable=2→0（条件更新）；② **Wcs_iparcel 和 Wcs_pick 无条件 TRUNCATE（全清）**；新系统：sorting_rules 按批次删 status>=2 记录 + pick_progress 按批次全清 anum |
| Redis 用途 | 未发现 | **原 C# 用 Redis（本地 127.0.0.1:6379）存计数器（total_num/day_num/day_ng_num）和扫码追踪队列（SynNo Hash、YJLG List）**；新系统**不引入 Redis**，计数器落 sys_config，扫码记录落 scan_events |
| PDA 拣货 type 参数 | 未发现 | **EMSAPI GET /GetPickInfo?floor=N&type=N**，type 对应 Wcs_goods.column3（0=自动分拣,1=手工分拣）；新系统 /api/pick 需支持 type 参数，pick_progress 需含 picktype 字段 |
| scan_events 缺字段 | 仅基础字段 | **scan_events 需补 workstatus（0=待确认/1=已落包/2=已打印）、packagetime（手工+1min/自动NULL）、dwsno（DWS台号）**，对应 Wcs_tparcelb 三个字段；workstatus 驱动打印工作流 |
| manual-sort 缺 autoRunning | 未发现 | **前端 manual.html 期望 `autoRunning` 字段**（用于在自动上机期间禁用手工扫码按钮）；新系统从 `sys_config.auto_running` 读取并返回 |
| PDA 并发扫码竞态 | 未考虑 | **两台 PDA 同时扫同一条码，可能同时读到 anum<num 并双重推进**；`handle_pda_scan` 的 `pick_progress` 和 `sorting_rules` SELECT 均需加 `WITH (UPDLOCK, ROWLOCK)` |
| column4 面单数据未存储 | 未发现 | **Wcs_goods.column4 存面单数据（格式：账号-CTN序号）**，用于打印；`sorting_rules` 需增 `label_data NVARCHAR(200)` 字段，`allocate_ports` 返回时携带 |
| main.py 参数传错 | 未发现 | `read_car_status_loop(plc)` 少传 `db_conn`；`start_tcp_server(plc, port=8888)` 少传 `db_conn`；运行即崩溃 |
| event_push_loop 函数未定义 | 未发现 | main.py 调用了 `event_push_loop` 但正文无实现；运行即崩溃 |
| /api/manual-sort 字段名不一致 | 未发现 | 前端 manual.html 期望 `{success, port, autoRunning}`，ADR 返回 `{ok, portno, autoRunning}`；字段名不匹配前端调用报错 |
| handle_manual_scan syno 精度不一致 | 未发现 | `handle_scan` 已修为毫秒精度，但 `handle_manual_scan` 仍用秒级 `'1'+HHMMSS+'1'`；同秒两次手工扫同条码 event_key 碰撞（UNIQUE 冲突） |
| handle_pda_scan 锁提示应用 HOLDLOCK | ROWLOCK | `WITH(UPDLOCK, ROWLOCK)` 在 SQL Server 中语句结束后即释放锁；应用 `WITH(UPDLOCK, HOLDLOCK)` 持锁到事务提交，防并发竞态 |
| try_reassign_overflow 无持锁 | 未考虑 | 检查 `pending` 和后续 UPDATE 之间无 HOLDLOCK，两次查询可见不同快照；改为 SELECT WITH(UPDLOCK, HOLDLOCK) |
| scan_events 缺 batchno 字段 | 未设计 | `print_port_label` 查询 JOIN ON `se.barcode=sr.barcode` 跨批次匹配；根本原因是 scan_events 无 batchno；需加字段并在写入时携带 |
| read_button_loop 同步打印阻塞 | 未考虑 | `print_port_label` 是同步调用（含 PDF 生成+打印，3-10s），在 500ms 轮询循环内阻塞，导致后续按钮事件丢失；改为 daemon thread |
| rollback_batch 清格口无 WHERE | 未发现 | `UPDATE sort_ports SET fj_num=0` 无 WHERE 条件，清零全部格口；应只清本批次相关格口 |
| onekey 未验证 active_batchno | 未发现 | `qval(...) or ''` 将 NULL 降级为空字符串；若 active_batchno 未设置，UPDATE WHERE batchno='' 无效但静默成功 |
| port_lights 表只定义未使用 | 未发现 | DDL 有 `port_lights` 表，但代码只写 PLC 不写此表，表永远为空；属冗余定义 |
| sort_ports/port_lights 无初始行 | 未发现 | DDL 只有 CREATE TABLE，无初始 INSERT；`UPDATE sort_ports WHERE portno=?` 若行不存在则 0 行静默失败；需在 DDL 后加 1-200 行初始化 |
| allocate_ports 缺 label_data | 未发现 | `allocate_ports()` 返回 dict 无 `label_data` 字段，但 rule_sync INSERT 用 `r.get("label_data")` 读取；云端不返回则永远 NULL；需在 allocate_ports 中拼接 `label_data=f"{orderno}-{box_num}"` |
| Flask 缺 cancel/hard-delete 路由 | 未发现 | 对照表列出 `POST /api/batch/:batchno/cancel` 和 `DELETE /api/batch/:batchno/hard`，但 Flask API 实现中两个路由均缺失 |
| rule_sync_loop/port_monitor_loop 循环壳缺失 | 未发现 | main.py 调用 `rule_sync_loop` 和 `port_monitor_loop`，但正文只有 `sync_rules_from_cloud()` 和 `get_port_status()` 函数，缺 while True 外壳 |
| _update_stats 失败路径遗漏 | 未发现 | PLC 写入或 SQL 提交失败时，`_update_stats(ok=False)` 不会被调用（在 try 外）；失败扫码不计入 day_ng_num 统计 |
| sorting_rules.synced_at 未写入 | 未发现 | DDL 定义了 `synced_at DATETIME`，但 rule_sync INSERT 未包含此字段，规则同步时间永远为 NULL |
| overflow innerport=0 未拦截 | 未发现 | 溢出条码 innerport=0 时，handle_scan 查到后调用 `_write_plc(port=0, ...)`，写 port=0 给 PLC 行为未定义；需在写 PLC 前加 innerport==0 检查，返回 errorport=51 |

---

## 新系统整体架构

```
┌─────────────────────────────────────────────────────┐
│              Python Sorting Agent                   │
│                (Windows 本地)                        │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ TCP监听  │  │ PLC读写  │  │  Flask Web API   │  │
│  │ :8888    │  │ S7-1200  │  │  :5009           │  │
│  └────┬─────┘  └────┬─────┘  └────────┬─────────┘  │
│       │              │                 │            │
│  ┌────▼─────────────▼─────────────────▼──────────┐  │
│  │          业务逻辑层 (Python)                   │  │
│  │  scan_handler / port_manager / batch_assign   │  │
│  └────────────────────┬──────────────────────────┘  │
│                       │                             │
│  ┌────────────────────▼──────────────────────────┐  │
│  │     SQL Server: SortingAgent DB               │  │
│  └───────────────────────────────────────────────┘  │
│                       │                             │
│  ┌────────────────────▼──────────────────────────┐  │
│  │     云端同步模块（30秒轮询）                   │  │
│  │  拉取规则 ← gongdashuai.top:5008              │  │
│  │  推送看板 → gongdashuai.top:5008              │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

---

## Redis 决策（不引入）

**原系统 Redis 用途（wcs_main.cs 确认）：**

| Key | 类型 | 用途 |
|-----|------|------|
| `total_num` | String | 全局扫码总件数计数器 |
| `day_num` | String | 今日正常件数计数器 |
| `day_ng_num` | String | 今日异常件数计数器 |
| `SynNo` | Hash | synid → Tbparcel（扫码后临时缓存，等待 DB201 落包确认） |
| `keys_with_time` | Hash | synid → 时间戳（用于超时检测） |
| `YJLG` | List | 已确认落包的包裹队列 |
| `YJSM` | List | 另一种队列（每日清零） |

**新系统决策：不引入 Redis，全部用 SQL Server 替代**

| 原 Redis 用途 | 新系统替代方案 |
|---------------|----------------|
| 计数器（total_num/day_num/day_ng_num） | `sys_config` 表，`_update_stats()` 函数 |
| SynNo Hash（扫码结果临时缓存） | **无需缓存**：新系统扫码成功即写 PLC + 写 `scan_events`，不依赖 DB201 落包确认（已确认无物理落包传感器） |
| keys_with_time（超时检测） | `scan_events.scanned_at` 字段，60s 内未二次确认的逻辑由 `port_manager` 监控 |
| YJLG / YJSM 队列 | `scan_events` 表查询替代 |

> **理由**：原系统用 Redis 做 PLC 写入→落包确认的中间缓冲，是因为 DB201 读到落包信号后才算完成。
> 新系统已确认**扫码成功即视为落包**（无物理传感器），整个缓冲层不再需要，SQL Server 直接落盘即可。

---

## 一、数据库设计（SQL Server，数据库名：SortingAgent）

**部署方式：** 与原 EmsSort 同一 SQL Server 实例，新建独立数据库 `SortingAgent`，两库数据完全隔离。
**Python 连接库：** `pyodbc`（Windows Auth，无需密码配置）

```python
# core/db.py
import pyodbc

_CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"        # 同机，也可填实例名如 .\SQLEXPRESS
    "DATABASE=SortingAgent;"
    "Trusted_Connection=yes;"  # Windows 身份验证
)

def get_db_conn() -> pyodbc.Connection:
    """
    ⚠️ 每次调用返回新连接，各线程启动时各自调用一次，不要跨线程共享同一 connection 对象。
    pyodbc Connection 对象不是线程安全的。
    """
    conn = pyodbc.connect(_CONN_STR, autocommit=False)
    conn.setdecoding(pyodbc.SQL_CHAR, encoding='utf-8')
    conn.setencoding(encoding='utf-8')
    return conn

# ── 查询辅助函数（统一 cursor 管理，避免重复样板代码）──────────────────────────
def qone(conn, sql, params=()):
    """查询首行，返回 dict 或 None。"""
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    return dict(zip([c[0] for c in cur.description], row)) if row else None

def qval(conn, sql, params=()):
    """查询首行首列标量值，或 None。"""
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None

def qall(conn, sql, params=()):
    """查询所有行，返回 list[dict]。"""
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

def execute(conn, sql, params=()):
    """执行写语句，不自动提交，调用方负责 conn.commit() / conn.rollback()。"""
    conn.cursor().execute(sql, params)
```

### 表设计（SQL Server DDL）

```sql
-- ============================================================
-- 数据库：SortingAgent（同实例，独立于 EmsSort）
-- ============================================================

-- 批次表（替代 Wcs_batch）
CREATE TABLE sort_batches (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    batchno     NVARCHAR(100) NOT NULL UNIQUE,
    box_type    INT DEFAULT 0,
    status      INT DEFAULT 0,           -- 0=准备中 1=已分配 2=已发送 3=已完成
    rule_ver    INT DEFAULT 0,
    created_at  DATETIME DEFAULT GETDATE()
);

-- 分拣规则表（替代 Wcs_ngoods + Wcs_iparcel，从云端同步，**已按 1:N 展开**）
-- ⚠️ 对应原 Wcs_ngoods：一个 SKU quantity=N 时，此表有 N 行（slot_seq 1..N），同一批次同一条码允许多行
CREATE TABLE sorting_rules (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    rule_ver    INT NOT NULL,
    batchno     NVARCHAR(100) NOT NULL,
    barcode     NVARCHAR(100) NOT NULL,
    slot_seq    INT DEFAULT 1,           -- 第几个物理件（1..quantity），对应 Wcs_ngoods 的一行
    portno      INT NOT NULL,            -- 分配格口（>102 = 溢出）
    innerport   INT DEFAULT 0,           -- 实际写PLC格口（溢出时=0，等重分配后更新）
    customer    NVARCHAR(200),
    goodsno     NVARCHAR(100),
    goodsmodel  NVARCHAR(100),
    floor       INT DEFAULT 0,           -- 由 goodsmodel 第二段首字母推算（见下方 floor_from_goodsmodel）
    serialnum   INT DEFAULT 0,           -- 写PLC byte 8-9（来自导入时@synid）
    label_data  NVARCHAR(200),           -- 面单数据（column4，格式：账号-CTN序号），打印用
    box_type    INT DEFAULT 1,
    status      INT DEFAULT 0,           -- 0=待扫 1=等待拣货 2=可上机 3=已落包 4=格口已清 5=批次已完成（truncate）
    created_at  DATETIME DEFAULT GETDATE(),
    synced_at   DATETIME,
    -- ⚠️ 唯一约束为 (batchno, barcode, slot_seq)，允许同一条码多件、跨批次复用
    CONSTRAINT UQ_rules_batch_barcode_slot UNIQUE (batchno, barcode, slot_seq)
);
CREATE INDEX IX_sorting_rules_barcode ON sorting_rules(barcode);
CREATE INDEX IX_sorting_rules_status  ON sorting_rules(status);

-- PDA 拣货进度表（替代 Wcs_pick）
-- ⚠️ 一行对应一个 SKU（barcode）：num=总需拣数量，anum=已拣数量
-- PDA 扫码时：检查 anum < num → anum++ → 对应 sorting_rules 里最小 slot_seq 且 status=1 的那件改为 status=2
-- ⚠️ 对应 Wcs_pick 完整字段（含 PDA 展示用字段和 picktype 过滤字段）
CREATE TABLE pick_progress (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    batchno     NVARCHAR(100) NOT NULL,
    barcode     NVARCHAR(100) NOT NULL,
    floor       INT DEFAULT 0,
    goodsnum    NVARCHAR(100),           -- 货号（PDA 展示用，对应 Wcs_pick.goodsnum）
    model       NVARCHAR(200),           -- 规格/型号（PDA 展示用，对应 Wcs_pick.model）
    unit        NVARCHAR(50),            -- 单位（PDA 展示用）
    quantity    INT DEFAULT 0,           -- 本 SKU 总数量（原 Wcs_goods.quantity）
    num         INT DEFAULT 0,           -- 本批次该条码需拣总数（= sorting_rules 中该条码行数）
    anum        INT DEFAULT 0,           -- 已拣数量
    picktype    INT DEFAULT 0,           -- 0=自动分拣 1=手工分拣（来自 Wcs_goods.column3）
    port        INT DEFAULT 0,           -- 分配格口（PDA 展示用，对应 Wcs_pick.port）
    updated_at  DATETIME DEFAULT GETDATE(),
    CONSTRAINT UQ_pick_batch_barcode UNIQUE (batchno, barcode)
);
CREATE INDEX IX_pick_barcode   ON pick_progress(barcode);
CREATE INDEX IX_pick_picktype  ON pick_progress(picktype);

-- 格口状态表（替代 Wcs_port）
CREATE TABLE sort_ports (
    portno      INT PRIMARY KEY,
    init_num    INT DEFAULT 0,           -- 本批次分配包裹总数
    fj_num      INT DEFAULT 0,           -- 已落包数量（每次扫码+1）
    remark      INT DEFAULT 0,           -- 手工锁格（1=锁，0=正常）
    is_enable   INT DEFAULT 1,
    modified_at DATETIME DEFAULT GETDATE()
    -- 格口已满可清：init_num != 0 AND init_num = fj_num AND remark = 0
    -- 格口超时告警：DATEADD(MINUTE,40,modified_at) < GETDATE() AND init_num!=0
    --               AND fj_num!=0 AND init_num!=fj_num AND remark=0
);

-- 扫码事件表（替代 Wcs_iparcel + Wcs_tparcelb）
CREATE TABLE scan_events (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    batchno     NVARCHAR(100),           -- ⚠️ 写入时携带，用于 print_port_label 跨批次过滤
    barcode     NVARCHAR(100) NOT NULL,
    innerport   INT NOT NULL,
    carno       INT NOT NULL,
    syno        INT NOT NULL,            -- 时间戳序号，写PLC byte 0-3
    serialnum   INT NOT NULL,            -- 写PLC byte 8-9
    length      INT DEFAULT 0,
    width       INT DEFAULT 0,
    height      INT DEFAULT 0,
    weight      INT DEFAULT 0,
    is_manual   INT DEFAULT 0,           -- 0=自动 1=手工分拣
    workstatus  INT DEFAULT 0,           -- 0=待确认 1=已落包 2=已打印（对应 Wcs_tparcelb.workstatus）
    packagetime DATETIME NULL,           -- 手工分拣时=scanned_at+1min；自动分拣=NULL（对应 Wcs_tparcelb.packagetime）
    dwsno       INT DEFAULT 0,           -- DWS台号（对应 Wcs_tparcelb.dws）
    scanned_at  DATETIME DEFAULT GETDATE(),
    pushed      INT DEFAULT 0,           -- 0=未推云端 1=已推
    event_key   NVARCHAR(64)             -- 幂等键（barcode+syno，推送去重用）
);
CREATE INDEX IX_scan_events_pushed ON scan_events(pushed);
CREATE INDEX IX_scan_events_batchno ON scan_events(batchno);

-- PLC 小车状态表（DB201 落包信号 Array[1..150]）
CREATE TABLE car_status (
    carno        INT PRIMARY KEY,        -- 小车号 1-150
    syno         INT DEFAULT 0,
    portno       INT DEFAULT 0,
    last_updated DATETIME DEFAULT GETDATE()
);

-- 格口灯状态表（DB200 格口状态 Array[1..200]）
CREATE TABLE port_lights (
    portno       INT PRIMARY KEY,        -- 格口号 1-200
    light_val    INT DEFAULT 0,          -- 0=全灭 1=绿常亮 2=黄常亮 3=红常亮 4=红闪 5=黄闪
    last_updated DATETIME DEFAULT GETDATE()
);

-- ============================================================
-- 初始化数据：sort_ports（1-200格口）+ port_lights（1-200格口）
-- ⚠️ 必须在建表后执行，否则后续 UPDATE WHERE portno=? 会 0 行静默失败
-- ============================================================
DECLARE @i INT = 1;
WHILE @i <= 200 BEGIN
    INSERT INTO sort_ports  (portno, init_num, fj_num, remark, is_enable)
    VALUES (@i, 0, 0, 0, 1);
    INSERT INTO port_lights (portno, light_val)
    VALUES (@i, 0);
    SET @i = @i + 1;
END;

-- 系统配置表（替代 Sys_config）
CREATE TABLE sys_config (
    [key]        NVARCHAR(100) PRIMARY KEY,
    value        NVARCHAR(500) NOT NULL
);
-- 初始数据：
-- INSERT INTO sys_config([key],value) VALUES
--   ('box_vol_1',''),('box_vol_2',''),('box_vol_3',''),
--   ('box_vol_offset','200'),('port_max','102'),('port_error','51'),
--   ('port_timeout_min','40'),('cloud_url','http://gongdashuai.top:5008'),
--   ('rule_version','0'),
--   ('active_batchno',''),  -- ⚠️ 当前活跃批次号，扫码查询时用此过滤
--   ('stat_total_num','0'), -- 总扫码件数（原 C# total_num）
--   ('stat_day_num','0'),   -- 今日正常件数（原 day_num）
--   ('stat_day_ng_num','0'),-- 今日异常件数（原 day_ng_num）
--   ('stat_day_date',''),    -- 统计日期，变化则自动清零 day_num/day_ng_num
--   ('auto_running','0')    -- 0=手工模式 1=自动上机中（前端 manual.html 用于禁用手工扫码按钮）
```

---

## 二、核心业务逻辑（替代 15 条存储过程）

### 2.1 规则同步（替代 Proc_Importordergoodsinfo + Proc_Producttoport + Proc_Sendsorting）

**原关键行为（已修正）：**
- `Proc_Importordergoodsinfo` 每次调用**先 TRUNCATE** Wcs_iparcel 和 Wcs_pick，全量替换
- `Proc_Sendsorting` 调用时**先清零所有格口** `InitNum/FJNum/IsEnable`，再重新统计

**新方案：** 云端生成「barcode → innerport」映射，本地按批次全量替换（对齐原系统行为）

```python
# local_agent/rule_sync.py
def sync_rules_from_cloud(db_conn, cloud_url, current_ver):
    resp = requests.get(f"{cloud_url}/agent/rules", params={"since_ver": current_ver}, timeout=5)
    data = resp.json()
    rules = data["rules"]
    if not rules:
        return

    # ⚠️ 对齐原系统：新批次到来时全量替换（TRUNCATE 语义）
    new_batchno = rules[0].get("batchno")
    try:
        if new_batchno:
            execute(db_conn, "DELETE FROM sorting_rules WHERE batchno=?", (new_batchno,))
            # 重置所有格口计数（对齐 Proc_Sendsorting 开头的 UPDATE Wcs_port）
            execute(db_conn, "UPDATE sort_ports SET init_num=0, fj_num=0, remark=0")

        for r in rules:
            innerport = r["portno"] if r["portno"] <= 102 else 0  # 溢出格口 innerport=0
            execute(db_conn, """
                INSERT INTO sorting_rules
                    (rule_ver, batchno, barcode, slot_seq, portno, innerport, customer, goodsno,
                     goodsmodel, floor, serialnum, label_data, box_type, status, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,GETDATE())
            """, (r["ver"], r["batchno"], r["barcode"], r.get("slot_seq", 1),
                  r["portno"], innerport,
                  r.get("customer"), r.get("goodsno"), r.get("goodsmodel"),
                  r.get("floor", 0), r["serialnum"],
                  r.get("label_data"),   # ⚠️ 云端 allocate_ports 返回 label_data（column4 面单数据）
                  r.get("box_type", 1)))
            if innerport != 0:
                execute(db_conn,
                    "UPDATE sort_ports SET init_num=init_num+1 WHERE portno=?", (innerport,))

        execute(db_conn, "UPDATE sys_config SET value=? WHERE [key]='rule_version'",
                (str(rules[-1]["ver"]),))
        # 更新活跃批次
        execute(db_conn, "UPDATE sys_config SET value=? WHERE [key]='active_batchno'",
                (new_batchno,))
        db_conn.commit()
    except Exception:
        db_conn.rollback()
        raise
```

### 2.2 扫码处理（替代 Proc_Getportinfo）

**落包机制确认（2026-06-12）：**
- 格口上**没有**红外传感器，不做物理落包检测
- **扫码成功 = 落包完成**，写完 PLC 即视为该包裹已入格口
- 无需读取 DB201 落包信号，不需要任何异步等待

**已修正关键点：**
- PLC 写入用 `innerport`，不是 `portno`
- `serialnum` 直接从 `sorting_rules.serialnum` 读取（导入时的 @synid）
- 未知条码返回 `innerport=51`（原系统行为）
- `syno` 并发碰撞修复：使用毫秒精度（不再只精确到秒）
- `floor` 由 `goodsmodel` **第二段首字母**推算（不是 product_number）

```python
# local_agent/scan_handler.py
import struct, threading
from datetime import datetime

_plc_lock = threading.Lock()
# ⚠️ PLC 所有操作（读+写）统一用此锁，snap7 底层 C 库不保证多线程安全

# ── 楼层推算（对应原 Proc_Sendsorting 中的 IF/ELSE 逻辑，已从 script.sql 逐行确认）──
# goodsmodel 示例："RED E12" → split → 第二段 "E12" → 首字母 'E' → floor=2
# 完整映射（4层，来自 SQL 原文）：
#   A/B/C/D → 1楼
#   E/F/G   → 2楼
#   H/I/J   → 3楼
#   K/L/M/N → 4楼
#   goodsmodel 段数 < 2（无空格或只一段）→ 0（原系统行为：floor=0，人工确认）
_FLOOR_MAP: dict = {}
for _c in 'ABCDabcd': _FLOOR_MAP[_c] = 1
for _c in 'EFGefg':   _FLOOR_MAP[_c] = 2
for _c in 'HIJhij':   _FLOOR_MAP[_c] = 3
for _c in 'KLMNklmn': _FLOOR_MAP[_c] = 4

def floor_from_goodsmodel(goodsmodel: str) -> int:
    segs = (goodsmodel or '').split()
    if len(segs) < 2:
        return 0   # 无第二段 → floor=0（对应原系统 ELSE 分支 set @floor=0）
    first_char = segs[1][0]
    return _FLOOR_MAP.get(first_char, 0)   # 字母不在映射表中也返回0（待确认）

# ── syno 生成（并发安全，毫秒精度）─────────────────────────────────────────────
# 原系统：'1'+HH+MM+SS+'1'（10位），但同秒两次扫码 syno 相同
# 新方案：'1'+HH+MM+SS+ms3位（12位数字，放入 UDInt 4字节 = 最大 4294967295 ≈ 42亿，足够）
# ⚠️ 新方案生成的 syno 最长 13 位（>2³²），实际约束：
#   最大值 = 1 + 23 + 59 + 59 + 999 = 12350999 → 8位，远小于 UDInt 上限，安全
def gen_syno() -> int:
    now = datetime.now()
    return int(f"1{now.hour:02d}{now.minute:02d}{now.second:02d}{now.microsecond // 1000:03d}")

def handle_scan(db_conn, plc, carno: int, barcode: str) -> bool:
    # 1. 查本地缓存（SQL Server 本机）— 必须 status=2（可上机）且属于活跃批次
    # ⚠️ 状态前提：规则同步写入时 status=1（等待拣货），需经 PDA 扫码或一键上机后
    #    变为 status=2（可上机），扫码才能命中。Phase 1 必须确认这个操作流程。
    active_batch = qval(db_conn,
        "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''

    cursor = db_conn.cursor()
    # ⚠️ ORDER BY slot_seq ASC OFFSET 0 ROWS FETCH NEXT 1 ROWS ONLY：
    #    同一条码有多件（slot_seq 1..N）时，按顺序取最小未扫的那件
    cursor.execute(
        "SELECT TOP 1 id, innerport, serialnum FROM sorting_rules "
        "WHERE barcode=? AND batchno=? AND status=2 ORDER BY slot_seq",
        (barcode, active_batch)
    )
    row = cursor.fetchone()

    if not row:
        # 未知条码 → 写错误格口 51，更新异常计数
        err_syno = gen_syno()
        _write_plc(plc, carno, port=51, serialnum=0, syno=err_syno)
        _update_stats(db_conn, ok=False)
        return False

    rule_id, innerport, serialnum = row

    # ⚠️ overflow 拦截：溢出包 innerport=0（未完成重分配），写 port=0 给 PLC 行为未定义
    # 此时条码已 status=2（PDA 拣过）但格口未分配，当作异常件处理 → errorport=51
    if innerport == 0:
        err_syno = gen_syno()
        _write_plc(plc, carno, port=51, serialnum=0, syno=err_syno)
        _update_stats(db_conn, ok=False)
        return False

    # 2. 生成 syno（毫秒精度，并发安全）
    syno = gen_syno()

    # 3. 写 PLC DB200（10字节大端序，innerport 写格口字段）
    # ⚠️ 先写 PLC 再写 DB：保证硬件优先（对齐原 Proc_Getportinfo 行为）
    # 风险：PLC 写成功但 DB 失败时格口计数不一致，需人工核查
    _write_plc(plc, carno, port=innerport, serialnum=serialnum, syno=syno)

    # 4. 更新状态 + 格口计数
    try:
        execute(db_conn, "UPDATE sorting_rules SET status=3 WHERE id=?", (rule_id,))
        execute(db_conn,
            "UPDATE sort_ports SET fj_num=fj_num+1, modified_at=GETDATE() WHERE portno=?",
            (innerport,))
        execute(db_conn,
            "INSERT INTO scan_events (batchno,barcode,innerport,carno,syno,serialnum,event_key) "
            "VALUES (?,?,?,?,?,?,?)",
            (active_batch, barcode, innerport, carno, syno, serialnum, f"{barcode}_{syno}"))
        db_conn.commit()
    except Exception:
        db_conn.rollback()
        # ⚠️ DB 写失败也计入异常统计（PLC 已写，DB 未记录）
        _update_stats(db_conn, ok=False)
        raise

    # 5. 统计计数器（对应原 C# total_num / day_num）
    _update_stats(db_conn, ok=True)

    # 6. 触发溢出重分配检查
    try_reassign_overflow(db_conn, innerport)
    return True


def _update_stats(db_conn, ok: bool):
    """更新扫码统计：total_num +1，今日正常/异常计数 +1（对应原 C# 内存计数器）"""
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        # 如果日期变了，重置今日计数
        db_conn.cursor().execute("""
            UPDATE sys_config SET value='0'
            WHERE [key] IN ('stat_day_num','stat_day_ng_num')
              AND (SELECT value FROM sys_config WHERE [key]='stat_day_date') <> ?
        """, (today,))
        db_conn.cursor().execute(
            "UPDATE sys_config SET value=? WHERE [key]='stat_day_date'", (today,))
        # total_num 始终 +1
        db_conn.cursor().execute(
            "UPDATE sys_config SET value=CAST(CAST(value AS INT)+1 AS NVARCHAR) "
            "WHERE [key]='stat_total_num'")
        # 今日计数（⚠️ 参数化查询，不用 f-string 拼接）
        key = 'stat_day_num' if ok else 'stat_day_ng_num'
        db_conn.cursor().execute(
            "UPDATE sys_config SET value=CAST(CAST(value AS INT)+1 AS NVARCHAR) "
            "WHERE [key]=?", (key,))
        db_conn.commit()
    except Exception:
        db_conn.rollback()


def _write_plc(plc, carno, port, serialnum, syno=0):
    """写 PLC DB200 相机信号（10字节大端序，详见 ADR-0001）
    PLC字段：序列号(UDInt) + 格口号(UInt) + 小车号(UInt) + 喷码号(UInt)
    """
    buf = bytearray(10)
    struct.pack_into('>I', buf, 0, syno)       # UDInt 4B — 序列号
    struct.pack_into('>H', buf, 4, port)       # UInt  2B — 格口号（innerport）
    struct.pack_into('>H', buf, 6, carno)      # UInt  2B — 小车号
    struct.pack_into('>H', buf, 8, serialnum)  # UInt  2B — 喷码号
    with _plc_lock:
        plc.db_write(200, 0, bytes(buf))

## 格口灯状态常量（DB200.格口状态 USInt，A08程序确认）
LIGHT_OFF     = 0  # 全灭（空闲）
LIGHT_GREEN   = 1  # 绿灯常亮（完成落格）
LIGHT_YELLOW  = 2  # 黄灯常亮（超时等待 >40min）
LIGHT_RED     = 3  # 红灯常亮（格口关闭）
LIGHT_RED_FLASH    = 4  # 红灯闪烁（强制完成/缺货）
LIGHT_YELLOW_FLASH = 5  # 黄灯闪烁（等待手工配货）

def write_port_light(db_conn, plc, portno: int, light_val: int):
    """写单个格口灯，DB200 offset = 9 + portno，⚠️需 HMI.主机模式=TRUE
    同时回写 port_lights 表（本地镜像，供看板/API 读取，无需反查 PLC）
    """
    with _plc_lock:
        plc.db_write(200, 9 + portno, bytes([light_val]))
    execute(db_conn,
        "UPDATE port_lights SET light_val=?, last_updated=GETDATE() WHERE portno=?",
        (light_val, portno))
    db_conn.commit()

def write_all_port_lights(db_conn, plc, light_values: list):
    """批量写200个格口灯，DB200 offset 10，共200字节；同步回写 port_lights"""
    vals = light_values[:200]
    with _plc_lock:
        plc.db_write(200, 10, bytes(vals))
    for i, v in enumerate(vals, start=1):
        execute(db_conn,
            "UPDATE port_lights SET light_val=?, last_updated=GETDATE() WHERE portno=?",
            (v, i))
    db_conn.commit()
```

### 2.3 手工分拣（替代 Proc_GetManualportinfo，新增功能）

**原逻辑：** DWS 台称扫描时带尺寸/重量参数，只处理 `column3=1`（手工类型）的商品

```python
# local_agent/scan_handler.py（新增）
def handle_manual_scan(db_conn, carno: int, barcode: str,
                        length: int, width: int, height: int, weight: int,
                        dwsno: int = 0) -> int:
    """手工分拣：不走PLC，直接写入scan_events，返回格口号"""
    active_batch = qval(db_conn, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    row = qone(db_conn,
        "SELECT id, innerport, serialnum FROM sorting_rules "
        "WHERE barcode=? AND batchno=? AND status=2",
        (barcode, active_batch))
    if not row:
        return 0

    rule_id, innerport, serialnum = row["id"], row["innerport"], row["serialnum"]
    # ⚠️ 使用毫秒精度 gen_syno()，与 handle_scan 一致，避免同秒两次手工扫同条码时 event_key 碰撞
    syno = gen_syno()

    try:
        execute(db_conn, "UPDATE sorting_rules SET status=3 WHERE id=?", (rule_id,))
        execute(db_conn,
            "UPDATE sort_ports SET fj_num=fj_num+1, modified_at=GETDATE() WHERE portno=?",
            (innerport,))
        execute(db_conn,
            "INSERT INTO scan_events "
            "(batchno,barcode,innerport,carno,syno,serialnum,length,width,height,weight,"
            " is_manual,packagetime,dwsno,event_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,1,DATEADD(MINUTE,1,GETDATE()),?,?)",
            # ⚠️ packagetime = scanned_at+1min（原 Proc_GetManualportinfo 行为）
            # workstatus 默认0（待确认），由后续打印流程更新
            (active_batch, barcode, innerport, carno, syno, serialnum,
             length, width, height, weight, dwsno, f"{barcode}_{syno}"))
        db_conn.commit()
    except Exception:
        db_conn.rollback()
        raise
    return innerport
```

### 2.4 PDA 扫码拣货（替代 Proc_Scanbarcode）

**原逻辑（已还原）：**
1. 查 `Wcs_pick WHERE Barcode=? AND Floor=?`，检查 `anum < num`（还有未拣件）
2. `anum++`
3. 把 `Wcs_iparcel` 中最小未拣 slot 的 `status 1→2`（等待拣货→可上机）
4. 若 `anum >= num`，不再允许扫码

**新系统对应：**
- `pick_progress` 替代 `Wcs_pick`（num/anum 计数）
- `sorting_rules.slot_seq` 替代 `Wcs_ngoods` 行序号
- 规则同步时须同步初始化 `pick_progress`（`num = sorting_rules 该条码总行数`）

```python
# local_agent/scan_handler.py
def handle_pda_scan(db_conn, barcode: str, floor: int) -> dict:
    """
    PDA 扫码确认拣货（替代 Proc_Scanbarcode）：
    - 检查 pick_progress.anum < num（还有未拣件）
    - anum++
    - 将 sorting_rules 中最小 slot_seq 且 status=1 的那一件改为 status=2（可上机）
    """
    active_batch = qval(db_conn,
        "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''

    # 1. 查拣货进度（⚠️ UPDLOCK+HOLDLOCK：持锁到事务提交，防两台PDA读到同一 anum<num）
    # ROWLOCK → HOLDLOCK：SQL Server UPDLOCK 若不加 HOLDLOCK，语句结束即释放锁
    progress = qone(db_conn,
        "SELECT id, num, anum FROM pick_progress WITH (UPDLOCK, HOLDLOCK) "
        "WHERE batchno=? AND barcode=? AND floor=?",
        (active_batch, barcode, floor))

    if not progress:
        # 判断是否属于其他楼层
        other = qval(db_conn,
            "SELECT TOP 1 floor FROM pick_progress WHERE batchno=? AND barcode=?",
            (active_batch, barcode))
        if other is not None:
            return {"ok": False, "msg": f"该商品在 {other} 楼，当前扫码楼层 {floor} 不匹配"}
        return {"ok": False, "msg": "条码不存在或不属于当前批次"}

    if progress["anum"] >= progress["num"]:
        return {"ok": False, "msg": f"该商品已全部出库（{progress['num']} 件）"}

    # 2. 找 status=1 且格口号最小的那一件
    # ⚠️ 原系统 Proc_Scanbarcode: ORDER BY port（格口号升序，最小格口先拣）
    # 不用 ORDER BY slot_seq，改为 ORDER BY portno 与原系统对齐
    # ⚠️ UPDLOCK+HOLDLOCK：两把锁联动，事务提交前阻塞其他 PDA 获取同一条码的 rule 行
    rule_row = qone(db_conn,
        "SELECT TOP 1 id FROM sorting_rules WITH (UPDLOCK, HOLDLOCK) "
        "WHERE batchno=? AND barcode=? AND status=1 ORDER BY portno",
        (active_batch, barcode))

    if not rule_row:
        return {"ok": False, "msg": "该商品无待拣库存（可能已一键上机或状态异常）"}

    try:
        execute(db_conn, "UPDATE sorting_rules SET status=2 WHERE id=?", (rule_row["id"],))
        execute(db_conn,
            "UPDATE pick_progress SET anum=anum+1, updated_at=GETDATE() WHERE id=?",
            (progress["id"],))
        db_conn.commit()
    except Exception:
        db_conn.rollback()
        raise

    remaining = progress["num"] - progress["anum"] - 1
    return {"ok": True, "msg": f"扫码成功，剩余 {remaining} 件待拣"}
```

**规则同步时初始化 pick_progress（rule_sync.py 补充）：**

```python
# 在 sync_rules_from_cloud() 写入 sorting_rules 后，统计每条码总件数并初始化进度表
# ⚠️ 每个 (batchno, barcode) 组合在 pick_progress 只有一行，num = slot 总数（即 quantity）
# ⚠️ 1:N 展开已在云端 allocate_ports() 完成（qty=N → sorting_rules 有 N 行 slot_seq 1..N）
#    rule_sync 收到的规则数据里，同一 barcode 可能有多条（slot_seq 不同），需聚合
# ⚠️ goodsnum/model/picktype/port 等展示字段从规则数据中任取一行（同 barcode 值相同）
cursor.execute("""
    MERGE pick_progress AS target
    USING (
        SELECT
            batchno, barcode,
            MAX(floor)    AS floor,
            MAX(goodsno)  AS goodsnum,
            MAX(goodsmodel) AS model,
            COUNT(*)      AS num,          -- slot_seq 行数 = quantity
            MAX(picktype) AS picktype,     -- 0=自动 1=手工（同 barcode 值一致）
            MAX(portno)   AS port          -- 任取一个格口号（PDA 展示用）
        FROM sorting_rules
        WHERE batchno=?
        GROUP BY batchno, barcode
    ) AS src ON target.batchno=src.batchno AND target.barcode=src.barcode
    WHEN MATCHED THEN
        UPDATE SET num=src.num, anum=0, floor=src.floor,
                   goodsnum=src.goodsnum, model=src.model,
                   picktype=src.picktype, port=src.port,
                   updated_at=GETDATE()
    WHEN NOT MATCHED THEN
        INSERT (batchno, barcode, floor, goodsnum, model, num, anum, picktype, port)
        VALUES (src.batchno, src.barcode, src.floor, src.goodsnum, src.model,
                src.num, 0, src.picktype, src.port);
""", (new_batchno,))
```

> **1:N 展开说明**：云端 `allocate_ports()` 在返回规则时，qty=N 的 SKU 已展开为 N 行（slot_seq 1..N），
> 所以 `rule_sync` 写入 `sorting_rules` 时同一 barcode 有 N 行；`pick_progress` 聚合后 `num=N`，
> PDA 扫码 N 次后 anum=N，完成该 SKU 的全部拣货。

### 2.5 格口溢出重分配（替代 Proc_Updateportinfo，已修正）

**原逻辑（已修正）：** 找 `innerport!=0` 且所有包裹 `status=3` 的格口 → 把该格口的溢出包（innerport=0）重新分配进来 → 更新 Wcs_pick

```python
# local_agent/port_manager.py

def try_reassign_overflow(db_conn, scanned_innerport: int):
    """
    扫码完成后检查：如果该格口所有包裹都已落包(status=3)，
    找最早的溢出条码(innerport=0)，重新分配进来。
    对应原 Proc_Updateportinfo。
    """
    # 检查该格口是否全部落包（⚠️ HOLDLOCK：持锁到事务提交，防两个扫码线程同时通过检查后双重触发重分配）
    pending = qval(db_conn,
        "SELECT COUNT(*) FROM sorting_rules WITH (UPDLOCK, HOLDLOCK) WHERE innerport=? AND status<3",
        (scanned_innerport,))
    if pending and pending > 0:
        return  # 还有未落包，不处理

    # 找最早一批溢出条码（portno>102 且 innerport=0），在标记 status=4 前先查
    overflow_rows = qall(db_conn,
        "SELECT id, barcode, portno FROM sorting_rules "
        "WHERE innerport=0 AND portno>102 AND status=1 ORDER BY id")
    if not overflow_rows:
        # 无溢出，直接标格口已清
        try:
            execute(db_conn,
                "UPDATE sorting_rules SET status=4 WHERE innerport=?", (scanned_innerport,))
            db_conn.commit()
        except Exception:
            db_conn.rollback()
            raise
        return

    # 确定第一批溢出包对应的 portno
    first_portno = overflow_rows[0]["portno"]
    same_group_count = sum(1 for r in overflow_rows if r["portno"] == first_portno)

    try:
        execute(db_conn,
            "UPDATE sorting_rules SET status=4 WHERE innerport=?", (scanned_innerport,))
        execute(db_conn,
            "UPDATE sorting_rules SET innerport=? WHERE portno=? AND innerport=0 AND status=1",
            (scanned_innerport, first_portno))
        execute(db_conn,
            "UPDATE sort_ports SET init_num=?, fj_num=0, modified_at=GETDATE() WHERE portno=?",
            (same_group_count, scanned_innerport))
        db_conn.commit()
    except Exception:
        db_conn.rollback()
        raise
```

### 2.6 格口状态监控（替代 Proc_PortStatus，新增）

**原逻辑：**
- Status=1：`InitNum != 0 AND InitNum = FJNum AND Remark = 0`（已满可清）
- Status=2：`ModifiedDate < now-40min AND InitNum != 0 AND FJNum != 0 AND InitNum != FJNum AND Remark = 0`（超时告警）

```python
# local_agent/port_manager.py
def get_port_status(db_conn) -> list[dict]:
    """
    返回需要处理的格口列表。
    对应原 Proc_PortStatus。
    """
    cursor = db_conn.cursor()
    cursor.execute("""
        SELECT portno, init_num, fj_num, remark,
               CASE
                 WHEN init_num != 0 AND init_num = fj_num AND remark = 0
                   THEN 1
                 WHEN DATEADD(MINUTE, 40, modified_at) < GETDATE()
                      AND init_num != 0 AND fj_num != 0
                      AND init_num != fj_num AND remark = 0
                   THEN 2
                 ELSE 0
               END AS port_status
        FROM sort_ports
        WHERE init_num != 0
    """)
    cols = [c[0] for c in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    return [r for r in rows if r["port_status"] > 0]
```

### 2.7 自动更新溢出格口（替代 Proc_AutoUpdatePort，新增）

**原逻辑：** 按钮触发，传入一个已清空的格口号，把溢出包分配进来（`AutoUpdatePort` 是有参数版，`Updateportinfo` 是自动遍历版）

```python
# local_agent/port_manager.py
def auto_update_port(db_conn, freed_port: int) -> bool:
    """
    按钮触发：把指定已清空格口分配给最早的溢出包。
    对应原 Proc_AutoUpdatePort(@Port)。
    """
    has_overflow = qval(db_conn,
        "SELECT COUNT(*) FROM sorting_rules WHERE innerport=0 AND portno>102 AND status=1")
    port_is_free = qval(db_conn,
        "SELECT COUNT(*) FROM sort_ports WHERE portno=? AND init_num=fj_num AND init_num!=0",
        (freed_port,))

    try:
        if has_overflow and port_is_free:
            overflow_portno = qval(db_conn,
                "SELECT TOP 1 portno FROM sorting_rules "
                "WHERE innerport=0 AND portno>102 AND status=1 ORDER BY portno")
            total = qval(db_conn,
                "SELECT COUNT(*) FROM sorting_rules WHERE portno=? AND innerport=0",
                (overflow_portno,))
            execute(db_conn,
                "UPDATE sorting_rules SET innerport=? WHERE portno=? AND innerport=0",
                (freed_port, overflow_portno))
            execute(db_conn,
                "UPDATE sort_ports SET remark=0, fj_num=0, init_num=? WHERE portno=?",
                (total, freed_port))
            db_conn.commit()
            return True
        else:
            execute(db_conn,
                "UPDATE sort_ports SET remark=0, fj_num=0, init_num=0 WHERE portno=?",
                (freed_port,))
            db_conn.commit()
            return False
    except Exception:
        db_conn.rollback()
        raise
```

### 2.8 TCP 扫码枪监听（替代 ReceiveClientMsg）

**原协议：** `carno#barcode\r\n`（TCP port 8888）

```python
# local_agent/tcp_server.py
import asyncio

async def handle_scanner(reader, writer, db_conn, plc):
    while True:
        data = await reader.readline()
        if not data:
            break
        msg = data.decode('utf-8', errors='ignore').strip()
        if '#' not in msg:
            continue
        carno_str, barcode = msg.split('#', 1)
        handle_scan(db_conn, plc, int(carno_str), barcode.strip())

async def start_tcp_server(db_conn, plc, port=8888):
    server = await asyncio.start_server(
        lambda r, w: handle_scanner(r, w, db_conn, plc),
        '0.0.0.0', port
    )
    async with server:
        await server.serve_forever()
```

### 2.9 PLC 状态读取（替代 ReadCarPLC + ReadBtnPLC）

```python
# local_agent/plc_reader.py
import struct, time

def read_car_status_loop(db_conn, plc):
    """每100ms读DB201[0-899]，150辆小车，每辆6字节（⚠️非816/136，TIA Portal确认为150辆）"""
    while True:
        try:
            with _plc_lock:  # ⚠️ 读PLC也加锁，与写操作串行
                buf = plc.db_read(201, 0, 900)  # 150 × 6 = 900字节
            cursor = db_conn.cursor()
            for i in range(150):  # 150辆，非136
                syno   = struct.unpack_from('>i', buf, 6 * i)[0]
                portno = struct.unpack_from('>h', buf, 6 * i + 4)[0]
                carno  = i + 1
                # SQL Server upsert（无 INSERT OR REPLACE）
                cursor.execute("""
                    IF EXISTS (SELECT 1 FROM car_status WHERE carno=?)
                        UPDATE car_status SET syno=?,portno=?,last_updated=GETDATE() WHERE carno=?
                    ELSE
                        INSERT INTO car_status(carno,syno,portno) VALUES(?,?,?)
                """, (carno, syno, portno, carno, carno, syno, portno))
            db_conn.commit()
        except Exception as e:
            pass  # 记日志，不中断
        time.sleep(0.1)

def read_button_status(plc) -> list[bool]:
    """读DB201[900-924]，25字节=200bit（TIA Portal确认：格口按钮[1..200] Bool）"""
    buf = plc.db_read(201, 900, 25)
    return [bool((buf[b] >> bit) & 1) for b in range(25) for bit in range(8)]
```

### 2.10 批次规划与分箱算法（替代 Proc_Importordergoodsinfo + Proc_Producttoport + Proc_Sendsorting，**在云端 5008 实现**）

**数据来源（2026-06-12 确认）：**

| 数据 | 来源 | 备注 |
|------|------|------|
| 订单列表 | `sales_orders`（JDY 同步） | 用户手工选单，状态 `unassigned→in_batch→done` |
| 订单明细 | `sales_details.data_json → entries[].code/barcode/qty` | |
| 商品尺寸 | `jdy_products.length / width / height` | JDY 原生字段（string），已同步入库 |
| 箱型上限 | `Sys_config code=1/2/3` | 小/中/大箱体积上限，单位 cm³ |

**扫码对象（2026-06-12 确认）：**
- 传送带扫描的是 **JDY 产品条码**（`jdy_products.barcode`），贴在货物上
- `sorting_rules.barcode` = JDY barcode，不是生成的箱号
- 箱号 `orderno-N` 仅用于打印标签，不参与扫码匹配

**已修正关键点：**
- `@offset=200`：体积超出箱型上限+200才换格口（容差）
- 箱型可升级：当前箱装不下但总体积 ≤ 当前箱型×1.5 时，升级到下一箱型
- **格口按箱递增（非按件）**：每个新箱分配一个格口，同箱所有商品共用同一格口
- **订单按总数量降序排列**（大单优先，格口号靠前）
- **格口 51 跳过**（PLC 程序中格口 51 已注释）
- 格口 ≤ 102：普通格口（`innerport = portno`）；格口 > 102：分拨格口（`innerport = 0`）
- **楼层由 `goodsmodel` 第二段首字母决定**（如 `"RED E12"` → `"E12"` → `'E'` → 楼层2；由 `floor_from_goodsmodel()` 计算，写入 sorting_rules.floor）
- **⚠️ 1:N 展开**：每个 SKU 按 qty 展开为 qty 行（slot_seq 1..qty），每行独立可扫；allocate_ports 仍按 SKU 整体体积分配格口，展开在写入 sorting_rules 时进行
- **⚠️ DWS 尺寸变化**：原系统 Proc_GetManualportinfo 从 DWS 台称实时读取 l/w/h，新系统改用静态值 `jdy_products.length/width/height`；若产品未录尺寸，体积按 0 处理（需在前端提示补录）

```python
# 云端 customs_server/sorting/batch_planner.py
def _next_port(p: int) -> int:
    p += 1
    return p + 1 if p == 51 else p   # 跳过格口51

def allocate_ports(orders: list, box_configs: dict, offset: int = 200) -> list[dict]:
    """
    orders: [{'orderno': str, 'goods': [{'barcode': str, 'goodsmodel': str,
                                          'l': float, 'w': float, 'h': float,
                                          'qty': int, 'serialnum': int}]}]
    box_configs: {1: small_max, 2: medium_max, 3: large_max}  ← 从 Sys_config 读
    返回: [{'barcode', 'slot_seq', 'portno', 'innerport', 'floor',
             'orderno', 'box_no', 'box_type', 'serialnum'}, ...]
    ⚠️ 同一 barcode 若 qty=N，返回 N 行（slot_seq 1..N），对应 Wcs_ngoods 1:N 展开
    """
    curr_port = 0
    rules = []

    # 按订单总数量降序（大单先分，对齐原系统 order by TotalQuantity desc）
    for order in sorted(orders, key=lambda o: sum(g['qty'] for g in o['goods']), reverse=True):
        curr_port = _next_port(curr_port)   # 新订单开始 → 新格口
        box_num, box_type, curr_vol = 1, 1, 0
        total_vol = sum(float(g['l'] or 0) * float(g['w'] or 0) * float(g['h'] or 0) * g['qty']
                        for g in order['goods'])

        for g in order['goods']:
            item_vol = float(g['l'] or 0) * float(g['w'] or 0) * float(g['h'] or 0) * g['qty']
            max_vol = box_configs[box_type]

            if curr_vol + item_vol > max_vol + offset:
                curr_port = _next_port(curr_port)   # 体积超限 → 新箱 → 新格口
                box_num += 1
                curr_vol = item_vol
                if max_vol < total_vol <= max_vol * 1.5:
                    box_type = min(box_type + 1, 3)  # 箱型升级
            else:
                curr_vol += item_vol

            innerport = curr_port if curr_port <= 102 else 0
            floor = floor_from_goodsmodel(g.get('goodsmodel', ''))

            # ⚠️ 1:N 展开：qty=N 时生成 N 行，slot_seq 1..N
            box_no = f"{order['orderno']}-{box_num}"
            for slot in range(1, g['qty'] + 1):
                rules.append({
                    'barcode':    g['barcode'],
                    'slot_seq':   slot,
                    'portno':     curr_port,
                    'innerport':  innerport,
                    'floor':      floor,
                    'orderno':    order['orderno'],
                    'box_no':     box_no,
                    'box_type':   box_type,
                    'serialnum':  g.get('serialnum', 0),
                    # ⚠️ label_data：对应 Wcs_goods.column4（格式：账号-CTN序号）
                    # 账号=orderno，CTN序号=box_num（当前箱编号），由云端 allocate_ports 拼接后下发
                    'label_data': box_no,
                })

    return rules
```

### 2.11 格口面单打印（新增功能）

**触发条件：** 格口按钮按下（DB201 offset 900+）且该格口状态为绿灯（`init_num = fj_num` 且 `init_num != 0`）

**技术方案：Jinja2 + pdfkit（替代原 C# Grid++Report / PrintHelper.cs）**

| | 原 C# | 新 Python |
|--|--|--|
| 模板格式 | `.grf`（锐浪报表专用格式） | HTML/CSS（浏览器直接预览调整）|
| 生成引擎 | gregn6Lib（商业库）| pdfkit + wkhtmltopdf（免费）|
| 依赖 | COM 组件注册 | `wkhtmltopdf.exe` 放同目录 |

**依赖安装：**
```bash
pip install pdfkit jinja2 pywin32
# 另需 wkhtmltopdf.exe：https://wkhtmltopdf.org/downloads.html
# PyInstaller 打包时带上：--add-binary "wkhtmltopdf.exe;."
```

**config.json 新增字段：**
```json
{
  "printer_name": "HP LaserJet",
  "label_template": "templates/label.html",
  "wkhtmltopdf_path": "wkhtmltopdf.exe"
}
```

```python
# local_agent/print_manager.py
import os, tempfile
import pdfkit
import win32api
from jinja2 import Template

def print_port_label(db_conn, portno: int, printer_name: str,
                     template_path: str, wkhtmltopdf_path: str):
    """
    格口面单打印：查出该格口所有已落包记录，渲染 HTML 模板，发送到指定打印机。
    触发时机：格口按钮按下 + init_num = fj_num（绿灯状态）
    """
    active_batch = qval(db_conn,
        "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    items = qall(db_conn,
        "SELECT se.barcode, sr.goodsno, sr.customer, sr.label_data, se.serialnum, se.scanned_at "
        "FROM scan_events se "
        # ⚠️ JOIN 必须加 batchno，防止历史批次同条码同格口的数据混入
        "JOIN sorting_rules sr ON se.barcode = sr.barcode AND se.batchno = sr.batchno "
        "WHERE se.innerport = ? AND se.batchno = ? "
        "ORDER BY se.scanned_at",
        (portno, active_batch))

    if not items:
        return

    port_info = qone(db_conn,
        "SELECT init_num, fj_num FROM sort_ports WHERE portno=?", (portno,))

    # 渲染 HTML 模板
    html = Template(open(template_path, encoding="utf-8").read()).render(
        portno=portno,
        total=port_info["init_num"] if port_info else len(items),
        items=[dict(r) for r in items],
    )

    # HTML → PDF（临时文件）
    config = pdfkit.configuration(wkhtmltopdf=wkhtmltopdf_path)
    tmp_pdf = os.path.join(tempfile.gettempdir(), f"label_port{portno}.pdf")
    pdfkit.from_string(html, tmp_pdf, configuration=config,
                       options={"page-size": "A4", "margin-top": "5mm",
                                "margin-bottom": "5mm", "encoding": "UTF-8"})

    # 发送到打印机
    win32api.ShellExecute(0, "print", tmp_pdf, f'/d:"{printer_name}"', ".", 0)
```

**按钮触发集成（plc_reader.py 中调用）：**

```python
# local_agent/plc_reader.py（新增按钮处理逻辑）
def read_button_loop(db_conn, plc, config):
    """每500ms轮询DB201[900-924]，检测按钮按下事件"""
    prev_states = [False] * 200
    while True:
        try:
            states = read_button_status(plc)   # 200个Bool
            for i, (prev, curr) in enumerate(zip(prev_states, states)):
                portno = i + 1
                if not prev and curr:           # 上升沿：按钮被按下
                    port = qone(db_conn,
                        "SELECT init_num, fj_num FROM sort_ports WHERE portno=?", (portno,))
                    # 仅绿灯状态（init_num=fj_num 且不为0）才打印
                    if port and port["fj_num"] == port["init_num"] != 0:
                        # ⚠️ 异步打印：print_port_label 含 PDF 生成（3-10s），同步调用会阻塞 500ms 轮询
                        # 使用独立 daemon thread，轮询主循环不受影响
                        threading.Thread(
                            target=print_port_label,
                            args=(get_db_conn(), portno,
                                  config["printer_name"],
                                  config["label_template"],
                                  config["wkhtmltopdf_path"]),
                            daemon=True
                        ).start()
            prev_states = states
        except Exception:
            pass
        time.sleep(0.5)
```

**HTML 模板示例（`templates/label.html`，可自由修改）：**

```html
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { font-family: Arial, sans-serif; font-size: 12px; }
  .header { font-size: 18px; font-weight: bold; margin-bottom: 8px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { border: 1px solid #ccc; padding: 4px 8px; }
  th { background: #f0f0f0; }
</style>
</head>
<body>
  <div class="header">格口 {{ portno }} — 共 {{ total }} 件</div>
  <table>
    <tr><th>条码</th><th>货号</th><th>客户</th><th>扫码时间</th></tr>
    {% for item in items %}
    <tr>
      <td>{{ item.barcode }}</td>
      <td>{{ item.goodsno }}</td>
      <td>{{ item.customer }}</td>
      <td>{{ item.scanned_at }}</td>
    </tr>
    {% endfor %}
  </table>
</body>
</html>
```

### 2.12 后台循环（rule_sync_loop / port_monitor_loop）

**说明：** main.py 启动这两个循环线程，正文只有内部函数实现，缺外壳。

```python
# sync/rule_sync.py（新增循环壳）
import time

def rule_sync_loop():
    """每 30s 从云端拉取规则差量（替代原 C# 手工导入按钮）"""
    while True:
        try:
            db = get_db_conn()
            cloud_url = qval(db, "SELECT value FROM sys_config WHERE [key]='cloud_url'") or ''
            current_ver = int(qval(db,
                "SELECT value FROM sys_config WHERE [key]='rule_version'") or 0)
            if cloud_url:
                sync_rules_from_cloud(db, cloud_url, current_ver)
        except Exception:
            pass   # 不中断，记日志
        time.sleep(30)

# local_agent/port_manager.py（新增循环壳）
def port_monitor_loop():
    """每 5s 检查格口超时告警，触发灯控制（替代原 C# 定时轮询）"""
    while True:
        try:
            db = get_db_conn()
            alerts = get_port_status(db)
            for a in alerts:
                if a["port_status"] == 2:  # 超时告警 → 黄灯
                    # ⚠️ write_port_light 需要 db_conn 和 plc，plc 需作为全局或参数传入
                    pass   # TODO：write_port_light(db, plc, a["portno"], LIGHT_YELLOW)
        except Exception:
            pass
        time.sleep(5)
```

### 2.13 事件推送（替代 Redis YJLG 队列 → 云端看板）

**原系统：** C# 写 Redis `YJLG` List，云端 Flask 消费队列推看板。
**新系统：** `scan_events.pushed=0` 的记录由后台线程批量推送 `POST /agent/events`，网络恢复后自动补发。

```python
# sync/event_push.py
import time, requests

def event_push_loop():
    """每 3s 推送一批未发事件到云端（替代原 Redis YJLG 队列）"""
    while True:
        try:
            db = get_db_conn()
            cloud_url = qval(db, "SELECT value FROM sys_config WHERE [key]='cloud_url'") or ''
            if not cloud_url:
                time.sleep(3)
                continue

            rows = qall(db,
                "SELECT TOP 50 id, batchno, barcode, innerport, carno, syno, serialnum, "
                "       length, width, height, weight, is_manual, scanned_at, event_key "
                "FROM scan_events WHERE pushed=0 ORDER BY id")
            if rows:
                resp = requests.post(f"{cloud_url}/agent/events",
                                     json={"events": rows}, timeout=5)
                if resp.status_code == 200:
                    ids = [r["id"] for r in rows]
                    # 批量标记已推（SQL Server IN 子句，ids 长度≤50）
                    placeholders = ",".join(["?"] * len(ids))
                    execute(db,
                        f"UPDATE scan_events SET pushed=1 WHERE id IN ({placeholders})", ids)
                    db.commit()
        except Exception:
            pass   # 网络断开不崩溃，3s 后重试
        time.sleep(3)
```

---

## 三、Flask Web API（替代 EMSAPI，运行在 :5009）

```python
# local_agent/web_api.py
# ⚠️ 每个请求独立获取 DB 连接（Flask 多线程模式下不共享 connection）

@app.get('/api/pick')
def get_pick_list():
    """
    PDA 拣货列表（替代 GET /GetPickInfo?floor=N&type=N）
    ⚠️ type 参数对应 Wcs_goods.column3：0=自动分拣（默认），1=手工分拣
    ⚠️ 从 pick_progress 查（对应原 Wcs_pick WHERE num>anum AND floor=? AND picktype=?）
    """
    floor    = request.args.get('floor',    type=int, default=0)
    picktype = request.args.get('type',     type=int, default=0)
    active   = qval(get_db_conn(),
                    "SELECT value FROM sys_config WHERE [key]='active_batchno'") or ''
    db   = get_db_conn()
    rows = qall(db,
        "SELECT barcode, port AS portno, goodsnum, model, unit, quantity, num, anum "
        "FROM pick_progress "
        "WHERE batchno=? AND num>anum AND floor=? AND picktype=? "
        "ORDER BY model ASC",
        (active, floor, picktype))
    return jsonify(rows)

@app.get('/api/pda/scan')
def pda_scan():
    """PDA 扫码确认拣货（替代 GET /GetScancodeInfo）"""
    barcode = request.args['barcode']
    floor   = request.args.get('floor', type=int, default=0)
    result  = handle_pda_scan(get_db_conn(), barcode, floor)
    return jsonify(result)

@app.post('/api/manual-sort')
def manual_sort():
    """
    手工分拣扫码（替代 ManualSortController / Proc_GetManualportinfo）
    body: {"barcode": str, "carno": int, "length": int, "width": int,
           "height": int, "weight": int, "dwsno": int}
    ⚠️ 只处理 picktype=1（column3=1）的商品；普通商品走传送带扫码
    返回: {"ok": bool, "portno": int, "autoRunning": bool, "msg": str}
    ⚠️ autoRunning：前端 manual.html 用此字段判断是否禁用手工扫码按钮（自动上机期间禁用）
    """
    body    = request.get_json() or {}
    barcode = body.get('barcode', '')
    carno   = body.get('carno', 0)
    length  = body.get('length', 0)
    width   = body.get('width', 0)
    height  = body.get('height', 0)
    weight  = body.get('weight', 0)
    dwsno   = body.get('dwsno', 0)
    if not barcode:
        return jsonify({"success": False, "port": 0, "autoRunning": False,
                        "msg": "barcode 不能为空"}), 400
    db = get_db_conn()
    auto_running = bool(int(qval(db,
        "SELECT value FROM sys_config WHERE [key]='auto_running'") or 0))
    portno = handle_manual_scan(db, carno, barcode, length, width, height, weight, dwsno)
    if portno == 0:
        return jsonify({"success": False, "port": 0, "autoRunning": auto_running,
                        "msg": "条码不存在或非手工分拣商品"})
    # ⚠️ 字段名对齐前端 manual.html：success/port（非 ok/portno）
    return jsonify({"success": True, "port": portno, "autoRunning": auto_running,
                    "msg": f"请放入格口 {portno}"})

@app.get('/api/status')
def get_status():
    """看板数据：格口占用 + 小车状态 + 告警格口 + 统计计数"""
    db = get_db_conn()
    ports  = qall(db, "SELECT * FROM sort_ports WHERE init_num!=0")
    cars   = qall(db, "SELECT * FROM car_status ORDER BY carno")
    alerts = get_port_status(db)
    stats  = {r['key']: r['value'] for r in qall(db,
        "SELECT [key], value FROM sys_config WHERE [key] LIKE 'stat_%'")}
    return jsonify({"ports": ports, "cars": cars, "alerts": alerts, "stats": stats})

@app.post('/api/port/<int:portno>/remark')
def set_port_remark(portno):
    """锁格/解锁（替代 Proc_UpdatePortRemark）"""
    db = get_db_conn()
    remark = request.json.get('remark', 0)
    try:
        execute(db, "UPDATE sort_ports SET remark=? WHERE portno=?", (remark, portno))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"ok": True})

@app.post('/api/port/<int:portno>/auto-update')
def auto_update(portno):
    """按钮触发格口重分配（替代 Proc_AutoUpdatePort）"""
    result = auto_update_port(get_db_conn(), portno)
    return jsonify({"ok": True, "reassigned": result})

@app.post('/api/batch/onekey')
def onekey():
    """
    一键上机（替代 Proc_OneKey）
    ⚠️ 原系统两步操作（script.sql 确认）：
      1. update Wcs_iparcel set status=2        → sorting_rules status 1→2
      2. update Wcs_pick set anum=num           → pick_progress anum=num（标记全部已拣）
    不加第2步会导致 PDA 界面仍显示待拣货，状态不一致。
    """
    active = qval(get_db_conn(),
                  "SELECT value FROM sys_config WHERE [key]='active_batchno'")
    if not active:
        # ⚠️ active_batchno 未设置时不能用 '' 匹配，否则静默成功但实际什么都没改
        return jsonify({"ok": False, "msg": "未设置当前活跃批次（active_batchno）"}), 400
    db = get_db_conn()
    try:
        execute(db, "UPDATE sorting_rules SET status=2 WHERE batchno=? AND status=1", (active,))
        # ⚠️ 同步 pick_progress.anum=num（对应原系统 update Wcs_pick set anum=num）
        execute(db, "UPDATE pick_progress SET anum=num, updated_at=GETDATE() WHERE batchno=?",
                (active,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"ok": True})

@app.post('/api/batch/<batchno>/truncate')
def truncate_batch(batchno):
    """
    完成当前批次并清理扫码数据（替代 Proc_Truncatesorting）
    ⚠️ 原系统行为（script.sql 确认，非"软删"，是状态转移）：
      1. Wcs_ngoods/Wcs_goods/Wcs_order isenable=2→3（标记为"已完成"，不是删除）
      2. TRUNCATE Wcs_iparcel（清空扫码记录）
      3. TRUNCATE Wcs_pick（清空拣货进度）
    新系统对应：
      1. sorting_rules status=1/2/3 → status=5（已完成批次，与 rollback 区分）
      2. pick_progress 该批次全清（DELETE，不保留）
      3. scan_events 按 batchno 关联删除（可选，保留做审计则跳过此步）
    ⚠️ 与 rollback 的区别：truncate=正常完成归档，rollback=异常回退重做
    """
    db = get_db_conn()
    try:
        # 1. 把该批次所有规则行标记为"已完成"（status=5，区别于 rollback 的删除）
        execute(db, "UPDATE sorting_rules SET status=5 WHERE batchno=? AND status IN (1,2,3)",
                (batchno,))
        # 2. 清空该批次拣货进度（对应 TRUNCATE Wcs_pick）
        execute(db, "DELETE FROM pick_progress WHERE batchno=?", (batchno,))
        # 3. 格口全部清零（对应原系统 UPDATE Wcs_port 全量重置，为下一批次做准备）
        execute(db, "UPDATE sort_ports SET init_num=0, fj_num=0, remark=0, "
                    "modified_at=GETDATE()")
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"ok": True, "msg": f"批次 {batchno} 已完成归档，格口已清零"})

@app.post('/api/batch/<batchno>/cancel')
def cancel_batch(batchno):
    """
    取消格口分配（替代 Proc_Canceltoport）
    ⚠️ TODO：Proc_Canceltoport 在 script.sql 中未找到实现，具体行为待对照原系统确认后补全
    """
    return jsonify({"ok": False, "msg": "cancel 接口待实现（Proc_Canceltoport 逻辑待确认）"}), 501

@app.delete('/api/batch/<batchno>/hard')
def hard_delete_batch(batchno):
    """
    硬删除批次（替代 Proc_Deletesorting）
    ⚠️ 不可逆，彻底清除该批次所有相关数据
    """
    db = get_db_conn()
    try:
        execute(db, "DELETE FROM sorting_rules WHERE batchno=?", (batchno,))
        execute(db, "DELETE FROM pick_progress WHERE batchno=?", (batchno,))
        execute(db, "DELETE FROM scan_events WHERE batchno=?", (batchno,))
        execute(db, "DELETE FROM sort_batches WHERE batchno=?", (batchno,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"ok": True, "msg": f"批次 {batchno} 已彻底删除"})

@app.post('/api/batch/<batchno>/rollback')
def rollback_batch(batchno):
    """
    按批次回滚（替代 Proc_Rollbacksorting）
    ⚠️ 对应原系统行为（script.sql 确认）：
      - sorting_rules：删除该批次 status>=2（已上机/已落包）的行 → 保留 status=1（待拣）
      - pick_progress：该批次 anum 全部清零（对应 TRUNCATE Wcs_pick）
      - scan_events：保留（审计用，原系统 Wcs_iparcel TRUNCATE，新系统不清）
    ⚠️ 此操作不可逆，会丢失已完成的扫码记录对应的 sorting_rules 行
    """
    db = get_db_conn()
    try:
        # 1. 删除该批次已上机/已落包/已清格的规则行（status>=2）
        execute(db,
            "DELETE FROM sorting_rules WHERE batchno=? AND status>=2", (batchno,))
        # 2. 保留 status=1 的行（待拣，对应原系统 isenable=1 不动）
        # 3. pick_progress 该批次 anum 清零（对应 TRUNCATE Wcs_pick）
        execute(db,
            "UPDATE pick_progress SET anum=0, updated_at=GETDATE() WHERE batchno=?", (batchno,))
        # 4. sort_ports 格口计数重置（⚠️ 加 WHERE 只清本批次相关格口，避免误清其他批次数据）
        execute(db, """
            UPDATE sort_ports SET fj_num=0, modified_at=GETDATE()
            WHERE portno IN (
                SELECT DISTINCT innerport FROM sorting_rules
                WHERE batchno=? AND innerport!=0
            )
        """, (batchno,))
        # 5. 重新统计各格口 init_num（按回滚后剩余 status=1 的行数）
        execute(db, """
            UPDATE sp SET sp.init_num = cnt.c
            FROM sort_ports sp
            JOIN (
                SELECT innerport, COUNT(*) AS c
                FROM sorting_rules WHERE batchno=? AND status=1 AND innerport!=0
                GROUP BY innerport
            ) cnt ON sp.portno = cnt.innerport
        """, (batchno,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"ok": True, "msg": f"批次 {batchno} 回滚完成（已上机记录已删，待拣记录保留）"})
```

---

## 四、云端新增接口（gongdashuai.top:5008）

```
GET  /agent/rules?since_ver=N    → 返回版本N之后的规则差量（含 innerport 字段）
POST /agent/events               → 接收本地推送的扫码事件（看板用）
  ⚠️ 实现要求：
  - 本地离线积压：scan_events.pushed=0 的记录定时批量重推，网络恢复后补发
  - 幂等键：event_key = barcode+syno，云端按此去重，防止重推写入重复记录
  - 推送失败告警：连续失败 N 次后写本地日志/管理界面红点，现场可感知
GET  /sorting/dashboard          → 看板页面（Phase 2）
POST /sorting/rules              → 手工录入/编辑规则（Phase 1）
POST /sorting/batch/assign       → 触发分箱算法，生成规则批次
```

---

## 五、主程序入口

```python
# local_agent/main.py
import asyncio, threading

def main():
    # ⚠️ pyodbc Connection 不是线程安全的，每个线程启动时独立调用 get_db_conn()
    # 不要把同一个 conn 对象传给多个线程
    plc = connect_plc(config['plc_ip'])   # 172.168.10.100

    # ⚠️ read_car_status_loop 和 start_tcp_server 需要独立 db_conn，各自在启动时创建
    threading.Thread(target=read_car_status_loop, args=(get_db_conn(), plc), daemon=True).start()
    threading.Thread(target=read_button_loop,     args=(get_db_conn(), plc, config), daemon=True).start()
    threading.Thread(target=port_monitor_loop,    args=(),      daemon=True).start()
    threading.Thread(target=rule_sync_loop,       args=(),      daemon=True).start()
    threading.Thread(target=event_push_loop,      args=(),      daemon=True).start()
    threading.Thread(target=run_flask_app,        args=(),      daemon=True).start()

    asyncio.run(start_tcp_server(get_db_conn(), plc, port=8888))
    # 各函数内部第一行调用 db = get_db_conn() 获取自己的连接

if __name__ == '__main__':
    main()
```

---

## 六、文件结构规划

```
sorting_agent/
├── main.py
├── config.json              ← plc_ip, cloud_url, printer_name 等
├── plc/
│   ├── plc_client.py        ← snap7 封装，_plc_lock（读写全加锁）
│   ├── plc_reader.py        ← ReadCarPLC / ReadBtnPLC
│   └── plc_writer.py        ← _write_plc（严格大端序，'>I'/'>H'）
├── core/
│   ├── scan_handler.py      ← handle_scan / handle_manual_scan / handle_pda_scan
│   ├── port_manager.py      ← try_reassign_overflow / auto_update_port / get_port_status
│   └── db.py                ← pyodbc 连接工厂（每线程独立连接，SQL Server）
├── sync/
│   ├── rule_sync.py         ← 从云端拉规则（30s轮询）
│   └── event_push.py        ← 推扫码事件到云端
├── api/
│   ├── web_api.py           ← Flask :5009
│   └── templates/
│       ├── dashboard.html   ← 管理界面（格口状态+告警）
│       └── pick.html        ← PDA 拣货界面
├── print/
│   ├── print_manager.py     ← print_port_label()
│   └── templates/
│       └── label.html       ← 面单 HTML 模板（可直接用浏览器预览修改）
├── wkhtmltopdf.exe          ← 打包时随 exe 一起发布（免费）
└── tcp/
    └── tcp_server.py        ← asyncio TCP :8888
```

---

## 七、原有功能完整对照表（15条存储过程）

| 原存储过程 | 作用 | 新实现 | 备注 |
|-----------|------|--------|------|
| Proc_Importordergoodsinfo | 导入订单（全量替换） | 云端生成规则 + rule_sync.py | 同步时重置格口计数 |
| Proc_Producttoport | 分箱分格口 | 云端 assign_bins() | offset=200容差已还原 |
| Proc_Sendsorting | 发送分拣，创建iparcel | rule_sync.py 写入时自动生效 | 重置格口计数 |
| Proc_Getportinfo | 扫码查格口（主流程） | scan_handler.handle_scan | 用 innerport 写PLC |
| Proc_GetManualportinfo | 手工分拣（DWS） | scan_handler.handle_manual_scan + **POST /api/manual-sort** | 不写PLC，仅写scan_events；只处理 picktype=1（column3=1）商品 |
| Proc_Scanbarcode | PDA扫码确认拣货 | scan_handler.handle_pda_scan | status 1→2 |
| Proc_Updateportinfo | 溢出自动重分配 | port_manager.try_reassign_overflow | 扫码后自动触发 |
| Proc_AutoUpdatePort | 按钮触发格口重分 | port_manager.auto_update_port | POST /api/port/:no/auto-update |
| Proc_PortStatus | 格口状态查询 | port_manager.get_port_status | 看板告警用 |
| Proc_UpdatePortRemark | 锁格/解锁 | POST /api/port/:no/remark | 管理界面 |
| Proc_OneKey | 一键上机 | POST /api/batch/onekey | ⚠️ 两步：sorting_rules status 1→2 + pick_progress anum=num（缺第2步PDA界面状态不一致） |
| Proc_Rollbacksorting | 按批次回滚 | POST /api/batch/:batchno/rollback | ⚠️ 对应原系统两段操作：① sorting_rules 删除 status>=2 的行（保留 status=1 待拣）；② pick_progress 该批次 anum 全清零；③ 重置 sort_ports 格口计数；scan_events 保留（原系统 TRUNCATE Wcs_iparcel，新系统保留做审计） |
| Proc_Canceltoport | 取消格口分配 | POST /api/batch/:batchno/cancel | ⚠️ 存根，Proc_Canceltoport 在 script.sql 中未找到，逻辑待确认后实现 |
| Proc_Deletesorting | 按批次硬删除 | DELETE /api/batch/:batchno/hard | ⚠️ 不可逆：清 sorting_rules/pick_progress/scan_events/sort_batches |
| Proc_Truncatesorting | 批次完成归档 | POST /api/batch/:batchno/truncate | ⚠️ 非软删：isenable=2→3（已完成态）+ TRUNCATE Wcs_iparcel/Wcs_pick；新系统：sorting_rules status→5 + 删 pick_progress + 清 sort_ports；与 rollback 区分（正常完成 vs 异常回退） |
| PrintHelper（C#）| 格口面单打印 | print_manager.print_port_label | Jinja2+pdfkit替代Grid++Report，模板改为HTML |

---

## 影响

- **本地**：C# WinForms + EMSAPI + SQL Server → Python Agent（单进程多线程）
- **云端**：新增 5 个 API 端点，新增 sorting 相关表（不影响现有功能）
- **网络**：本地每 30s 轮询云端（流量极小），仅 :5008 端口

## 回滚方式

- Python Agent 不可用时，直接启动原 C# `yc_line_wcs.exe` 恢复
- SQL Server `EmsSort` 原始数据完全保留，不做任何修改
- C# 程序与 Python Agent 可并行运行（ADR-0001 Step 3 验证阶段）

## 状态

`proposed`

*创建时间：2026-06-11 | 最后更新：2026-06-13（第八次修订：新角度全面比对，修复13项 → ①DDL补sort_ports/port_lights初始1-200行；②sorting_rules INSERT加synced_at=GETDATE()；③handle_scan加overflow innerport=0拦截(返回errorport=51)；④_update_stats失败路径补调用；⑤allocate_ports返回dict加label_data字段(格式:orderno-box_num)；⑥新增rule_sync_loop/port_monitor_loop循环壳(2.12节)；⑦Flask补POST /api/batch/:batchno/cancel和DELETE /api/batch/:batchno/hard两路由；⑧⚠️重要修正表补8条新发现）*
