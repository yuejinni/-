# SQL Server cache backend

SQLite remains the default local cache backend. SQL Server is opt-in.

## Switch backend

```powershell
$env:QIHANG_DB_BACKEND = "mssql"
$env:QIHANG_SQLSERVER_CONNECTION_STRING = "Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=qihang_workbench;Trusted_Connection=yes;TrustServerCertificate=yes"
```

Alternatively set `QIHANG_SQLSERVER_DSN` to an ODBC DSN name.

When `QIHANG_DB_BACKEND` is unset or set to `sqlite`, the service continues to
use `_sales_cache/sales_cache.sqlite3`.

## Covered tables

The SQL Server schema generator covers the current local cache tables used by
the Flask service and quick-create workflow:

- `jdy_products`
- `jdy_suppliers`
- `sales_orders`
- `sales_details`
- `sales_product_quantities`
- `transfer_orders`
- `transfer_details`
- `inventory_snapshots`
- `product_create_draft`
- `product_create_draft_item`
- `product_create_sequences`
- `product_create_submit_log`
- `customs_product_master`
- `customs_product_master_history`
- `webhook_events`
- `webhook_runtime_settings`
- `accessory_purchase_orders`
- `accessory_products`
- `purchase_inbounds`
- `purchase_inbound_attachments`
- `bill_attachments`
- `purchase_attachment_items`
- `purchase_order_prices`
- `reorder_items`

## Migration

Dry-run only:

```powershell
G:\QihangJDY\venv\Scripts\python.exe customs_server\tools\migrate_sqlite_to_sqlserver.py --sqlite G:\path\to\sales_cache.sqlite3
```

Create schema only:

```powershell
G:\QihangJDY\venv\Scripts\python.exe customs_server\tools\migrate_sqlite_to_sqlserver.py --sqlite G:\path\to\sales_cache.sqlite3 --sqlserver-conn "<connection string>" --schema-only --write --confirm MIGRATE_SQLITE_TO_SQLSERVER
```

Copy selected data:

```powershell
G:\QihangJDY\venv\Scripts\python.exe customs_server\tools\migrate_sqlite_to_sqlserver.py --sqlite G:\path\to\sales_cache.sqlite3 --sqlserver-conn "<connection string>" --data --tables jdy_products,jdy_suppliers --write --confirm MIGRATE_SQLITE_TO_SQLSERVER
```

Truncate target tables before copying requires a second confirmation:

```powershell
G:\QihangJDY\venv\Scripts\python.exe customs_server\tools\migrate_sqlite_to_sqlserver.py --sqlite G:\path\to\sales_cache.sqlite3 --sqlserver-conn "<connection string>" --data --truncate-target --write --confirm MIGRATE_SQLITE_TO_SQLSERVER --confirm TRUNCATE_SQLSERVER_TARGET
```
