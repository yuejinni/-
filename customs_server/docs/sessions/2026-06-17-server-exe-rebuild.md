# 2026-06-17 server.exe 旧版本排查与重打包记录

## 背景

用户反馈访问 `http://gongdashuai.top:5008/jdy` 后看到的页面像旧版本。

当时 GitHub `main` 最新代码已经包含：

- `ea04fa0 Add sales order request tab and webhook refresh`
- `97e79b5 Exempt /agent/* routes from login auth` 已包含在分拣机集成分支历史中，并已进入当前代码线
- `customs_server/templates/jdy.html` 已包含 `销货单 / 销货订单` 标签
- `customs_server/server.py` 已包含 `/sales-order-requests`、`/sales-order-request-refresh*` 和 `/agent/*` 免登录逻辑

## 根因

生产 5008 实际运行的是旧打包文件：

```text
G:\祺航本地项目运行\服务端\server.exe
LastWriteTime: 2026/6/4 10:09:16
Old SHA256: 24FA6E91223AC39BAF91A8364E614D6A5D220387B4FFCB4BAB256F5460389F3E
```

虽然此前已经复制了新的 `server.py` 和 `templates\jdy.html` 到生产目录，但旧 `server.exe` 未重新打包替换，因此运行时后端逻辑仍然是旧版本。

证据：

```text
http://127.0.0.1:5008/health -> 200
http://gongdashuai.top:5008/health -> 200
当前 5008 进程：G:\祺航本地项目运行\服务端\server.exe
/agent/rules 在旧 exe 下返回 401
生产 server.py 中已存在 /agent/ 免登录代码，但旧 exe 未生效
```

结论：公网地址和端口没有指错，问题是生产运行的 `server.exe` 不是最新打包版本。

## 处理方案

用户选择方案 1：重新打包最新 `server.exe`，继续使用原生产 exe/watchdog 运行方式。

未采用方案 2：直接用 Python 运行 `server.py`。原因是生产全局 Python 缺少 Flask 等依赖，且现有 watchdog 默认启动 `server.exe`，临时改运行方式风险更高。

## 构建步骤摘要

在开发仓库：

```powershell
cd G:\QihangJDY\repo\customs_server
python -m venv .venv_build_server
.\.venv_build_server\Scripts\python.exe -m pip install --upgrade pip
.\.venv_build_server\Scripts\python.exe -m pip install pyinstaller flask flask-cors requests openpyxl xlrd pdfplumber certifi lxml waitress Pillow
```

构建前验证：

```powershell
.\.venv_build_server\Scripts\python.exe -m py_compile server.py jdy_api.py excel_gen.py product_create_workflow.py sorting\agent_api.py sorting\batch_planner.py db_backend.py
.\.venv_build_server\Scripts\python.exe -c "import flask, waitress, openpyxl, pdfplumber, requests, PIL; import jdy_api, excel_gen, product_create_workflow, db_backend; import sorting.agent_api; print('import ok')"
```

PyInstaller 打包命令：

```powershell
.\.venv_build_server\Scripts\python.exe -m PyInstaller `
  --onefile --noconsole --name server `
  --hidden-import flask `
  --hidden-import flask_cors `
  --hidden-import openpyxl `
  --hidden-import openpyxl.styles `
  --hidden-import openpyxl.utils `
  --hidden-import xlrd `
  --hidden-import pdfplumber `
  --hidden-import pdfminer `
  --hidden-import pdfminer.high_level `
  --hidden-import pdfminer.layout `
  --hidden-import certifi `
  --hidden-import lxml `
  --hidden-import lxml.etree `
  --hidden-import waitress `
  --hidden-import waitress.server `
  --hidden-import requests `
  --hidden-import PIL `
  --hidden-import sorting `
  --hidden-import sorting.agent_api `
  --hidden-import sorting.batch_planner `
  --add-data "G:\QihangJDY\repo\customs_server\templates;templates" `
  --distpath "_tmp_pyinstaller_dist" `
  --workpath "_tmp_pyinstaller_build" `
  --specpath "_tmp_pyinstaller_spec" `
  server.py
```

新 exe 构建结果：

```text
G:\QihangJDY\repo\customs_server\_tmp_pyinstaller_dist\server.exe
Length: 42903564
SHA256: F9886A0A04394F6A893E5D4D7B79946B8D3F8B4341BC3D069D26F1AB6442E947
```

## 临时端口预演

用 `QIHANG_PORT=5019` 启动新 exe，不影响生产 5008：

```powershell
$env:QIHANG_PORT='5019'
Start-Process -FilePath "G:\QihangJDY\repo\customs_server\_tmp_pyinstaller_dist\server.exe" `
  -WorkingDirectory "G:\QihangJDY\repo\customs_server" `
  -WindowStyle Hidden
```

预演结果：

```text
http://127.0.0.1:5019/health -> 200
响应：{"build":"20260603-reorder-mvp","port":5019,"status":"ok"}

http://127.0.0.1:5019/agent/rules -> 200
响应：{"count":0,"rules":[]}
```

临时 5019 进程随后已停止。

## 生产替换

备份旧生产 exe：

```text
G:\祺航本地项目运行\服务端\_deploy_backups\server_exe_20260617_161250\server.exe
```

替换生产 exe：

```text
New production server.exe SHA256:
F9886A0A04394F6A893E5D4D7B79946B8D3F8B4341BC3D069D26F1AB6442E947
```

生产重启后状态：

```text
ProcessId: 56904
Name: server.exe
ExecutablePath: G:\祺航本地项目运行\服务端\server.exe
CreationDate: 2026/6/17 16:12:56
```

验证：

```text
http://127.0.0.1:5008/health -> 200
http://127.0.0.1:5008/agent/rules -> 200, {"count":0,"rules":[]}
http://gongdashuai.top:5008/agent/rules -> 200, {"count":0,"rules":[]}
```

`/sales-order-requests?limit=1` 未登录返回 401，属于正常登录保护，不是旧版本。

## 相关销货订单数据状态

在本次重打包前，已完成一次受控销货订单占位回填：

```text
sales_order_requests:
祺航箱包 1 条，internal 占位 0
祺航饰品 8 条，internal 占位 0

sales_order_request_details:
9 条
```

回填备份：

```text
G:\祺航本地项目运行\服务端\_sales_cache\sales_cache.sqlite3.bak_20260617_154443_before_sales_order_request_backfill
```

回填只写：

- `sales_order_requests`
- `sales_order_request_details`

未写：

- `sales_orders`
- `sales_details`
- Excel 文件

## 注意事项

- 生产原 watchdog 脚本仍以 `server.exe` 为启动目标。
- 后续如果只复制 `server.py`，但不重打包或不切换到 Python 运行模式，后端逻辑不会自动进入生产。
- `templates\jdy.html` 是否生效取决于运行时读取路径；当前 `server.py` 已支持优先从 exe 同目录加载 `.py` 和模板，但旧 exe 仍可能无法覆盖所有后端逻辑。
- 如果后续要临时直接运行 `server.py`，需先准备完整 Python 运行环境，并同步调整 watchdog，否则 watchdog 会把服务拉回 `server.exe`。
- SQL Server 可选依赖 `pyodbc` 未安装；当前生产仍使用 SQLite，不影响本次运行。

## 本轮边界

- 已重打包并替换生产 `server.exe`
- 已重启 5008
- 未改业务代码
- 未提交 commit
- 未 push
- 未调用 JDY 写入
- 未修改 Excel
- 未生成报关文件
- 构建临时目录和 venv 已清理
- 仓库工作区最终保持干净
