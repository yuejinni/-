# ADR-0001：Python snap7 与 C# Sharp7 PLC 兼容性风险

## 背景

系统集成方案决定将本地 WCS Agent 从 C# Windows Forms（`yc_line_wcs`）重写为 Python 轻量服务，
以便与云端报关平台（gongdashuai.top:5008，Python Flask）统一技术栈。

重写过程中，最核心的硬件操作是通过 S7 协议操作西门子 S7-1200 PLC：
- **写入**：DB200（`SCADA-Write`），至少210字节，含分拣指令+格口灯状态（对应 `WriteStartPLC`）
- **读取**：DB201（`SCADA-Read`），900字节，监控 **150辆** 小车状态（对应 `ReadCarPLC`）

> ⚠️ **修正（2026-06-12 TIA Portal 确认）**：
> - 小车数量为 **150辆**（Array[1..150]），不是136辆；150×6字节=900字节
> - DB200 除分拣指令外，偏移10.0起还有 `格口状态 Array[1..200] of USInt`（200字节，控制灯光）
> - DB201 格口按钮为 `Array[1..200] of Bool`（200个），不是96bit
> - DB200 内部名称：`相机信号`（Struct，10字节）+ `格口状态`（Array，200字节）
> - DB201 内部名称：`落包信号`（Array[1..150] of Struct）+ `格口按钮`（Array[1..200] of Bool，偏移900.0）
> - `相机信号` Struct 内部字段和 `落包信号` Struct 内部字段**待下次 TIA Portal 展开确认**

C# 使用 Sharp7 库，Python 拟使用 `python-snap7` 库，两者底层协议相同（S7/ISO-on-TCP），
但字节级操作需要精确对齐，否则会导致**静默的分拣错误**（包裹进错格口且不报警）。

---

## DB200 精确结构（TIA Portal 2026-06-12 确认）

**SCADA-Write [DB200] 完整布局（210字节）：**

```
偏移     字段名      PLC类型   字节数   C#对应字段       说明
0.0      序列号      UDInt     4        syno             时间戳序号（'1'+HHMMSS+'1'）
4.0      格口号      UInt      2        plot/portno      目标格口（写 innerport）
6.0      小车号      UInt      2        carno            小车编号
8.0      喷码号      UInt      2        serialnum        订单序号（@synid来源）
10.0     格口状态[1] USInt     1        WriteRightSign   格口1灯状态
11.0     格口状态[2] USInt     1        ...              格口2灯状态
...
209.0    格口状态[200] USInt   1                         格口200灯状态
```

> 注意：C# 使用有符号类型（DInt/Int），PLC 定义为无符号（UDInt/UInt）。
> 字节格式完全相同，正数范围内无影响。Python 应使用无符号格式符（`'>I'`/`'>H'`）更准确。

## C# 原始实现（精确字节格式）

### WriteStartPLC（`wcs_main.cs` 第 409-439 行）

```csharp
byte[] writeBuffer = new byte[10];
S7.SetDIntAt(writeBuffer, 0, syno);            // offset 0, 4字节, UDInt（PLC）/ DInt（C#）
S7.SetIntAt(writeBuffer, 4, (short)plot);      // offset 4, 2字节, UInt（PLC）/ Int（C#）格口号
S7.SetIntAt(writeBuffer, 6, (short)carno);     // offset 6, 2字节, UInt（PLC）/ Int（C#）小车号
S7.SetIntAt(writeBuffer, 8, (short)serialnum); // offset 8, 2字节, UInt（PLC）/ Int（C#）喷码号
_plcMain.DBWrite(200, 0, 10, writeBuffer);     // 写 DB200，偏移0，共10字节
```

**10字节内存布局（大端序）：**

```
字节位置:   [0] [1] [2] [3]   [4] [5]   [6] [7]   [8] [9]
字段名:      ──序列号(4B)───  ─格口号─  ─小车号─  ─喷码号─
示例(syno=1000, port=3, car=1, serial=1):
十六进制:   00  00  03  E8    00  03    00  01    00  01
```

### WriteRightSignPLC（格口灯控制）

**格口状态 USInt 值含义（TIA Portal A08程序确认）：**

```python
# DB200.格口状态[n] USInt 值 → 灯效映射（PLC A08 格口信号程序确认）
PORT_LIGHT = {
    0: "全灭（空闲/无分配）",
    1: "绿灯常亮（完成落格，InitNum=FJNum）",
    2: "黄灯常亮（超时等待，>40分钟未完成）",
    3: "红灯常亮（格口关闭）",
    4: "红灯闪烁（强制完成/缺货，Clock_1Hz）",
    5: "黄灯闪烁（等待手工配货）",
}

# Python 写格口灯：DB200 offset = 9 + portno
def write_port_light(plc, portno: int, light_val: int):
    """light_val: 0-5，见 PORT_LIGHT 映射"""
    plc.db_write(200, 9 + portno, bytes([light_val]))

# 批量写（推荐，减少 S7 通讯次数）
def write_all_port_lights(plc, light_values: list):
    """light_values: 长度200的列表"""
    plc.db_write(200, 10, bytes(light_values[:200]))
```

**重要前提：`HMI.主机模式` 必须为 TRUE**，SCADA 控制才生效。主机模式 FALSE 时，PLC 使用本地按钮信号控制灯光，忽略 DB200 格口状态。

**DB201 格口按钮机制：**
PLC 在 SCADA 模式下将物理按钮状态复制到 `SCADA-Read.格口按钮[i]`（DB201 offset 900），WCS 通过读 DB201 获知按钮是否被按下。

### ReadCarPLC（`wcs_main.cs` 第 444-475 行）

> ⚠️ **修正（2026-06-12 TIA Portal 确认）**：C# 原代码读 816 字节（136辆），实际 PLC 有 **150辆**，应读 **900 字节**。

```csharp
// ⚠️ 原始 C# 代码（有误，仅供参考字节格式）
byte[] plcLineRBuffer = new byte[816];
_plcMain.DBRead(201, 0, 816, plcLineRBuffer); // ← 应为 900 字节！

// 解析：136辆小车（← 应为 150辆），每辆6字节
for (int i = 0; i < 136; i++) {
    int carSNY  = 6 * i;        // DInt(4B) = 序列号
    int carPort = 6 * i + 4;    // Int(2B)  = 格口号
    int carSNYValue  = S7.GetDIntAt(plcLineRBuffer, carSNY);
    int carPortValue = S7.GetIntAt(plcLineRBuffer, carPort);
}
```

## DB201 精确结构（TIA Portal 2026-06-12 确认）

**SCADA-Read [DB201] 完整布局：**

```
区域           字段名          PLC类型   偏移      字节数   说明
落包信号[1]    序列号          DInt      0.0       4        小车当前载荷序列号（signed）
落包信号[1]    格口号          Int       4.0       2        小车当前目标格口（signed）
落包信号[2]    序列号          DInt      6.0       4
落包信号[2]    格口号          Int       10.0      2
...（每辆小车6字节，步长6）
落包信号[150]  格口号          Int       894.0     2        第150辆，偏移 149×6+4=898
格口按钮[1..200] Bool         Bool      900.0+    25字节   每bit=1个格口按钮
```

> **关键不对称性**：DB200（写入）字段为 **无符号**（UDInt/UInt），DB201（读取）字段为 **有符号**（DInt/Int）。
> 字节格式完全相同，正数范围内无差异，但 Python 格式符必须分开使用：
> - 写 DB200：`'>I'`（UDInt）、`'>H'`（UInt）
> - 读 DB201：`'>i'`（DInt，小写）、`'>h'`（Int，小写）

**正确的 Python 读取代码：**

```python
# ✅ 正确：读 DB201 落包信号（150辆）
buf = plc.db_read(201, 0, 900)   # 读900字节（150 × 6字节）

for i in range(150):
    syn_no  = struct.unpack_from('>i', buf, 6 * i)[0]      # DInt 有符号 4字节
    port_no = struct.unpack_from('>h', buf, 6 * i + 4)[0]  # Int  有符号 2字节
```

---

## 风险清单

### 🔴 风险1：字节序写反（最致命）

**问题：** 西门子 S7 协议固定使用大端序（Big-Endian/Motorola），Python 默认是小端序。

```python
# ❌ 错误：小端序
struct.pack('<I', 1000)   # → E8 03 00 00  ← 顺序反了！

# ✅ 正确：大端序（UDInt 用大写 I，UInt 用大写 H）
struct.pack('>I', 1000)   # → 00 00 03 E8
```

**后果：** PLC 收到错误的格口号，包裹被分到错误格口，**不报任何错误**，静默出错。

---

### 🔴 风险2：数据类型宽度错误

**问题：** `SetDIntAt` = 32位（4字节），`SetIntAt` = 16位（2字节），混用导致字节错位。

```python
# ❌ 错误：把16位字段当32位写
buf[4:8] = struct.pack('>i', plot)   # 写了4字节，后续全部错位！

# ✅ 正确对应关系（按 PLC 实际类型，使用无符号格式符）
buf[0:4] = struct.pack('>I', syno)        # UDInt → '>I'  4字节（序列号）
buf[4:6] = struct.pack('>H', portno)      # UInt  → '>H'  2字节（格口号）
buf[6:8] = struct.pack('>H', carno)       # UInt  → '>H'  2字节（小车号）
buf[8:10] = struct.pack('>H', serialnum)  # UInt  → '>H'  2字节（喷码号）
```

---

### 🟡 风险3：snap7.dll 打包缺失

**问题：** `python-snap7` 底层依赖 `snap7.dll`（Windows），PyInstaller 打包时若未包含，
在无 Python 环境的 Windows 机器上运行会报"找不到 DLL"。

**解决：**
```bash
pyinstaller sorting_agent.py \
  --add-binary "snap7.dll;." \
  --onefile
```

---

### 🟡 风险4：DB201 读取解析偏移错误

**问题：** 每辆小车6字节，序列号在 `offset 6*i`，格口号在 `offset 6*i+4`，若偏移计算错误，
150辆小车状态全部错位。此外 C# 原代码读 816 字节（136辆）是错误的，**必须读 900 字节（150辆）**。

```python
# ✅ 正确的 Python 解析（TIA Portal 确认：150辆，DInt+Int，均有符号）
buf = plc.db_read(201, 0, 900)   # ✅ 900字节，不是816！

for i in range(150):             # ✅ 150辆，不是136！
    syn_no  = struct.unpack_from('>i', buf, 6 * i)[0]      # DInt 有符号 4字节（小写i）
    port_no = struct.unpack_from('>h', buf, 6 * i + 4)[0]  # Int  有符号 2字节（小写h）

# ⚠️ 注意：DB200写入用'>I'/'>H'（无符号），DB201读取用'>i'/'>h'（有符号），不要混用！
```

---

### 🟡 风险5：并发写 PLC 未加锁

**问题：** C# 用 `lock(_lockForPlcMain)` 保证 PLC 写操作串行，Python 必须同等处理。

```python
# ✅ Python 等价实现
import threading
_plc_lock = threading.Lock()

def write_start_plc(syno, plot, carno, serialnum):
    with _plc_lock:
        buf = bytearray(10)
        struct.pack_into('>I', buf, 0, syno)       # ✅ UDInt 无符号，大写 I
        struct.pack_into('>H', buf, 4, plot)       # ✅ UInt  无符号，大写 H
        struct.pack_into('>H', buf, 6, carno)      # ✅ UInt  无符号，大写 H
        struct.pack_into('>H', buf, 8, serialnum)  # ✅ UInt  无符号，大写 H
        return plc.db_write(200, 0, bytes(buf))

# ⚠️ 注意：DB200（写入）全部无符号 '>I'/'>H'；DB201（读取）全部有符号 '>i'/'>h'
# 读写方向不同，格式符不同，不可混用！
```

---

## 验证方案（Phase 1 必做，不可跳过）

### Step 1：字节对比（无需 PLC，开发机即可）

让 C# 和 Python 对同一组参数生成 writeBuffer，打印十六进制对比：

```
测试参数: syno=1000, plot=3, carno=1, serialnum=1
期望输出: 00 00 03 E8  00 03  00 01  00 01
C# 实际:  ________________（运行 C# 打印）
Python:   ________________（运行 Python 打印）
两者必须完全一致才能进入下一步
```

### Step 2：测试 PLC 写入验证

在测试用 DB（如 DB999）上，Python 写入，用 TIA Portal 监视实际值，确认数值正确。

### Step 3：并行运行对比（正式上线前）

C# 原程序和 Python Agent 同时运行，Python 只记录"如果我来写，格口号是多少"，
不实际写 PLC。连续运行一个工作日，比对两者对每个条码的格口号是否100%一致。
确认一致后再切换 Python 写 PLC，C# 退为备用。

---

## 结论

| 风险 | 级别 | 可消除 | 消除阶段 |
|------|------|--------|--------|
| 字节序写反 | 🔴 高 | ✅ | Step 1，开发阶段 |
| 数据类型宽度错误 | 🔴 高 | ✅ | Step 1，开发阶段 |
| snap7.dll 打包缺失 | 🟡 中 | ✅ | 打包阶段 |
| DB201 解析偏移错误 | 🟡 中 | ✅ | Step 2，测试阶段 |
| 并发写未加锁 | 🟡 中 | ✅ | 代码审查 |

**所有风险均可通过验证步骤消除，不存在不可控风险。**
关键前提：Phase 1 测试阶段必须完成 Step 1-3，不得跳过直接上线。

---

## 状态

`proposed`

*创建时间：2026-06-11*
