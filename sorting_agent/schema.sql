-- ============================================================
-- 数据库：SortingAgent（与 EmsSort 同实例，独立隔离）
-- 执行前请先在 SQL Server 中创建数据库：
--   CREATE DATABASE SortingAgent;
--   USE SortingAgent;
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

-- 分拣规则表（替代 Wcs_ngoods + Wcs_iparcel，从云端同步，已按 1:N 展开）
-- ⚠️ 同一批次同一条码允许多行（slot_seq 区分），不可用 UNIQUE(batchno,barcode)
CREATE TABLE sorting_rules (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    rule_ver    INT NOT NULL,
    batchno     NVARCHAR(100) NOT NULL,
    barcode     NVARCHAR(100) NOT NULL,
    slot_seq    INT DEFAULT 1,           -- 第几个物理件（1..quantity）
    portno      INT NOT NULL,            -- 分配格口（>102=溢出）
    innerport   INT DEFAULT 0,           -- 实际写PLC格口（溢出时=0，重分配后更新）
    customer    NVARCHAR(200),
    goodsno     NVARCHAR(100),
    goodsmodel  NVARCHAR(100),
    floor       INT DEFAULT 0,           -- 由 goodsmodel 第二段首字母推算
    serialnum   INT DEFAULT 0,           -- 写PLC byte 8-9
    label_data  NVARCHAR(200),           -- 面单数据（账号-CTN序号），打印用
    box_type    INT DEFAULT 1,
    queue_seq   INT DEFAULT 0,           -- 排队序号（1=最先上机；>100 时 innerport=0 等候）
    status      INT DEFAULT 0,           -- 0=待扫 1=等待拣货 2=可上机 3=已落包 4=格口已清 5=批次已完成
    created_at  DATETIME DEFAULT GETDATE(),
    synced_at   DATETIME,
    CONSTRAINT UQ_rules_batch_barcode_slot_label UNIQUE
        (batchno, barcode, slot_seq, label_data)
);
CREATE INDEX IX_sorting_rules_barcode ON sorting_rules(barcode);
CREATE INDEX IX_sorting_rules_status  ON sorting_rules(status);

-- PDA 拣货进度表（替代 Wcs_pick）
-- 一行对应一个 SKU（barcode）：num=总需拣数量，anum=已拣数量
CREATE TABLE pick_progress (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    batchno     NVARCHAR(100) NOT NULL,
    barcode     NVARCHAR(100) NOT NULL,
    floor       INT DEFAULT 0,
    goodsnum    NVARCHAR(100),           -- 货号（PDA 展示用）
    model       NVARCHAR(200),           -- 规格/型号（PDA 展示用）
    unit        NVARCHAR(50),            -- 单位
    quantity    INT DEFAULT 0,           -- 本 SKU 总数量
    num         INT DEFAULT 0,           -- 本批次该条码需拣总数
    anum        INT DEFAULT 0,           -- 已拣数量
    picktype    INT DEFAULT 0,           -- 0=自动分拣 1=手工分拣
    port        INT DEFAULT 0,           -- 分配格口（PDA 展示用）
    updated_at  DATETIME DEFAULT GETDATE(),
    CONSTRAINT UQ_pick_batch_barcode UNIQUE (batchno, barcode)
);
CREATE INDEX IX_pick_barcode   ON pick_progress(barcode);
CREATE INDEX IX_pick_picktype  ON pick_progress(picktype);

-- 格口状态表（替代 Wcs_port）
CREATE TABLE sort_ports (
    portno      INT PRIMARY KEY,
    init_num    INT DEFAULT 0,           -- 本批次分配包裹总数
    fj_num      INT DEFAULT 0,           -- 已落包数量
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
    workstatus  INT DEFAULT 0,           -- 0=待确认 1=已落包 2=已打印
    packagetime DATETIME NULL,           -- 手工分拣时=scanned_at+1min；自动分拣=NULL
    dwsno       INT DEFAULT 0,           -- DWS台号
    scanned_at  DATETIME DEFAULT GETDATE(),
    pushed      INT DEFAULT 0,           -- 0=未推云端 1=已推
    event_key   NVARCHAR(64)             -- 幂等键（barcode+syno，推送去重用）
);
CREATE INDEX IX_scan_events_pushed  ON scan_events(pushed);
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

-- 系统配置表（替代 Sys_config）
CREATE TABLE sys_config (
    [key]        NVARCHAR(100) PRIMARY KEY,
    value        NVARCHAR(500) NOT NULL
);

-- ============================================================
-- 初始化数据
-- ⚠️ 必须在建表后执行，否则 UPDATE WHERE portno=? 会 0 行静默失败
-- ============================================================

-- sort_ports 和 port_lights 各 1-200 行
DECLARE @i INT = 1;
WHILE @i <= 200 BEGIN
    INSERT INTO sort_ports  (portno, init_num, fj_num, remark, is_enable)
    VALUES (@i, 0, 0, 0, 1);
    INSERT INTO port_lights (portno, light_val)
    VALUES (@i, 0);
    SET @i = @i + 1;
END;

-- sys_config 初始 9 条数据
INSERT INTO sys_config([key], value) VALUES
    ('box_vol_1',        ''),
    ('box_vol_2',        ''),
    ('box_vol_3',        ''),
    ('box_vol_offset',   '200'),
    ('port_max',         '102'),
    ('port_error',       '51'),
    ('port_timeout_min', '40'),
    ('cloud_url',        'http://gongdashuai.top:5008'),
    ('rule_version',     '0'),
    ('active_batchno',   ''),
    ('stat_total_num',   '0'),
    ('stat_day_num',     '0'),
    ('stat_day_ng_num',  '0'),
    ('stat_day_date',    ''),
    ('auto_running',     '0');

-- 验证：SELECT COUNT(*) FROM sort_ports  → 200
--        SELECT COUNT(*) FROM port_lights → 200
--        SELECT COUNT(*) FROM sys_config  → 15
