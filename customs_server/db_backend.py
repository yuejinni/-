"""Database backend helpers for the local cache layer.

SQLite remains the default backend. SQL Server is opt-in with:

    QIHANG_DB_BACKEND=mssql
    QIHANG_SQLSERVER_CONNECTION_STRING=...

The helpers in this module deliberately avoid any production-specific
connection defaults. A SQL Server connection is only opened when the operator
provides a DSN or full connection string.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


BACKEND_SQLITE = "sqlite"
BACKEND_MSSQL = "mssql"
ENV_BACKEND = "QIHANG_DB_BACKEND"
ENV_SQLSERVER_DSN = "QIHANG_SQLSERVER_DSN"
ENV_SQLSERVER_CONNECTION_STRING = "QIHANG_SQLSERVER_CONNECTION_STRING"


def current_backend() -> str:
    value = (os.environ.get(ENV_BACKEND) or BACKEND_SQLITE).strip().lower()
    if value in ("sqlserver", "sql_server", "mssql"):
        return BACKEND_MSSQL
    return BACKEND_SQLITE


def is_mssql_enabled() -> bool:
    return current_backend() == BACKEND_MSSQL


def _require_pyodbc():
    try:
        import pyodbc  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "SQL Server backend requires pyodbc. Install it in the runtime "
            "environment, then set QIHANG_DB_BACKEND=mssql and provide "
            "QIHANG_SQLSERVER_CONNECTION_STRING or QIHANG_SQLSERVER_DSN."
        ) from exc
    return pyodbc


def _sqlserver_connection_string(explicit: str = "") -> str:
    conn = (explicit or os.environ.get(ENV_SQLSERVER_CONNECTION_STRING) or "").strip()
    if conn:
        return conn
    dsn = (os.environ.get(ENV_SQLSERVER_DSN) or "").strip()
    if not dsn:
        raise RuntimeError(
            "SQL Server backend is enabled but no connection info was provided. "
            f"Set {ENV_SQLSERVER_CONNECTION_STRING} or {ENV_SQLSERVER_DSN}."
        )
    if ";" in dsn or "=" in dsn:
        return dsn
    return f"DSN={dsn}"


class SqlRow:
    def __init__(self, names: Sequence[str], values: Sequence[Any]):
        self._names = [str(x) for x in names]
        self._values = tuple(values)
        self._index = {name.lower(): idx for idx, name in enumerate(self._names)}

    def keys(self) -> list[str]:
        return list(self._names)

    def items(self):
        return [(name, self[name]) for name in self._names]

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except Exception:
            return default

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return self._values[key]
        idx = self._index[str(key).lower()]
        return self._values[idx]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"SqlRow({dict(self.items())!r})"


class StaticCursor:
    def __init__(self, rows: Iterable[SqlRow] | None = None, rowcount: int = -1):
        self._rows = list(rows or [])
        self._pos = 0
        self.rowcount = rowcount

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self):
        rows = self._rows[self._pos :]
        self._pos = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class MssqlCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return int(getattr(self._cursor, "rowcount", -1))

    def _names(self) -> list[str]:
        desc = getattr(self._cursor, "description", None) or []
        return [str(item[0]) for item in desc]

    def _wrap(self, raw):
        if raw is None:
            return None
        return SqlRow(self._names(), tuple(raw))

    def fetchone(self):
        return self._wrap(self._cursor.fetchone())

    def fetchall(self):
        return [self._wrap(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                break
            yield row


class MssqlConnection:
    backend = BACKEND_MSSQL

    def __init__(self, raw):
        self.raw = raw
        self.row_factory = None

    def cursor(self):
        return MssqlCursor(self.raw.cursor())

    def execute(self, sql: str, params: Sequence[Any] | None = None):
        sql = str(sql or "")
        params_list = list(params or [])
        special = self._execute_special(sql, params_list)
        if special is not None:
            return special
        sql, params_list = translate_sqlite_to_mssql(sql, params_list)
        cur = self.raw.cursor()
        cur.execute(sql, params_list)
        return MssqlCursor(cur)

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]):
        sql, _ = translate_sqlite_to_mssql(str(sql or ""), [])
        cur = self.raw.cursor()
        cur.fast_executemany = True
        cur.executemany(sql, list(seq_of_params))
        return MssqlCursor(cur)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False

    def _execute_special(self, sql: str, params: list[Any]):
        stripped = sql.strip()
        lower = re.sub(r"\s+", " ", stripped.lower())
        pragma = re.match(r"pragma\s+table_info\(([^)]+)\)", stripped, re.I)
        if pragma:
            table = pragma.group(1).strip().strip("'\"[]")
            return StaticCursor(self._pragma_table_info(table))
        if " from sqlite_master " in f" {lower} ":
            return self._sqlite_master_query(sql, params)
        return None

    def _query_table_columns(self, table: str) -> list[SqlRow]:
        schema, table_name = split_table_name(table)
        cur = self.raw.cursor()
        cur.execute(
            """
            SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, ORDINAL_POSITION
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
            ORDER BY ORDINAL_POSITION
            """,
            [schema, table_name],
        )
        rows = []
        for row in cur.fetchall():
            rows.append(
                SqlRow(
                    ["COLUMN_NAME", "DATA_TYPE", "IS_NULLABLE", "COLUMN_DEFAULT", "ORDINAL_POSITION"],
                    tuple(row),
                )
            )
        return rows

    def _pragma_table_info(self, table: str) -> list[SqlRow]:
        rows = []
        for idx, row in enumerate(self._query_table_columns(table)):
            rows.append(
                SqlRow(
                    ["cid", "name", "type", "notnull", "dflt_value", "pk"],
                    [
                        idx,
                        row["COLUMN_NAME"],
                        row["DATA_TYPE"],
                        1 if str(row["IS_NULLABLE"]).upper() == "NO" else 0,
                        row["COLUMN_DEFAULT"],
                        0,
                    ],
                )
            )
        return rows

    def _sqlite_master_query(self, sql: str, params: list[Any]):
        table = params[0] if params else ""
        if not table:
            match = re.search(r"name\s*=\s*'([^']+)'", sql, re.I)
            table = match.group(1) if match else ""
        exists = table_exists(self, str(table))
        if not exists:
            return StaticCursor([])
        select_name = re.search(r"select\s+name\b", sql, re.I)
        names = ["name"] if select_name else ["1"]
        values = [str(table)] if select_name else [1]
        return StaticCursor([SqlRow(names, values)])


def connect_mssql(connection_string: str = "") -> MssqlConnection:
    pyodbc = _require_pyodbc()
    raw = pyodbc.connect(_sqlserver_connection_string(connection_string), autocommit=False)
    return MssqlConnection(raw)


def is_mssql_connection(conn: Any) -> bool:
    return isinstance(conn, MssqlConnection) or getattr(conn, "backend", "") == BACKEND_MSSQL


def split_table_name(table: str) -> tuple[str, str]:
    parts = [x.strip().strip("[]") for x in str(table or "").split(".") if x.strip()]
    if len(parts) == 2:
        return parts[0], parts[1]
    return "dbo", parts[0] if parts else ""


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str) -> str:
    name = str(name or "").strip().strip("[]")
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return f"[{name}]"


def quote_table(table: str) -> str:
    schema, name = split_table_name(table)
    return f"{quote_ident(schema)}.{quote_ident(name)}"


def _limit_int(value: Any) -> int:
    try:
        num = int(value)
    except Exception as exc:
        raise ValueError(f"invalid SQL limit value: {value!r}") from exc
    if num < 0:
        raise ValueError(f"invalid negative SQL limit value: {value!r}")
    return num


def _select_with_top(sql: str, top_value: int) -> str:
    return re.sub(
        r"^(\s*SELECT\s+)(DISTINCT\s+)?",
        lambda m: f"{m.group(1)}{m.group(2) or ''}TOP ({top_value}) ",
        sql,
        count=1,
        flags=re.I,
    )


def translate_sqlite_to_mssql(sql: str, params: Sequence[Any] | None = None) -> tuple[str, list[Any]]:
    params_list = list(params or [])
    text = str(sql or "")
    stripped = text.strip()
    text = re.sub(
        r"GROUP_CONCAT\s*\(\s*DISTINCT\s+([A-Za-z_][A-Za-z0-9_\.]*)\s*\)",
        r"STRING_AGG(CAST(\1 AS nvarchar(max)), ',')",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"GROUP_CONCAT\s*\(\s*([A-Za-z_][A-Za-z0-9_\.]*)\s*\)",
        r"STRING_AGG(CAST(\1 AS nvarchar(max)), ',')",
        text,
        flags=re.I,
    )
    if re.match(r"^BEGIN\s+IMMEDIATE\b", stripped, re.I):
        return "BEGIN TRANSACTION", params_list
    if re.match(r"^SELECT\s+last_insert_rowid\(\)\s+AS\s+id\s*;?\s*$", stripped, re.I):
        raise ValueError("last_insert_rowid() is not supported in SQL Server mode; use OUTPUT INSERTED.id")
    text = re.sub(r"\bHAVING\s+c\s*>\s*([0-9]+)", r"HAVING COUNT(*) > \1", text, flags=re.I)

    match = re.search(r"\s+LIMIT\s+\?\s+OFFSET\s+\?\s*;?\s*$", text, re.I)
    if match:
        if len(params_list) < 2:
            raise ValueError("LIMIT/OFFSET query missing parameters")
        limit = _limit_int(params_list[-2])
        offset = _limit_int(params_list[-1])
        params_list = params_list[:-2]
        base = text[: match.start()]
        if not re.search(r"\bORDER\s+BY\b", base, re.I):
            base += " ORDER BY (SELECT NULL)"
        return f"{base} OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY", params_list

    match = re.search(r"\s+LIMIT\s+\?\s*;?\s*$", text, re.I)
    if match:
        if not params_list:
            raise ValueError("LIMIT query missing parameter")
        limit = _limit_int(params_list[-1])
        params_list = params_list[:-1]
        return _select_with_top(text[: match.start()], limit), params_list

    match = re.search(r"\s+LIMIT\s+([0-9]+)\s*;?\s*$", text, re.I)
    if match:
        limit = _limit_int(match.group(1))
        return _select_with_top(text[: match.start()], limit), params_list

    return text, params_list


@dataclass(frozen=True)
class TableDef:
    name: str
    columns: list[tuple[str, str]]
    primary_key: tuple[str, ...] = ()
    indexes: list[tuple[str, tuple[str, ...], bool]] = field(default_factory=list)


def _idx(name: str, columns: Sequence[str], unique: bool = False) -> tuple[str, tuple[str, ...], bool]:
    return name, tuple(columns), unique


TABLES: dict[str, TableDef] = {
    "sales_orders": TableDef(
        "sales_orders",
        [
            ("account", "NVARCHAR(100) NOT NULL"),
            ("number", "NVARCHAR(100) NOT NULL"),
            ("date", "NVARCHAR(32) NULL"),
            ("customer_name", "NVARCHAR(255) NULL"),
            ("total_qty", "FLOAT NOT NULL DEFAULT 0"),
            ("total_amount", "FLOAT NOT NULL DEFAULT 0"),
            ("check_status", "NVARCHAR(100) NULL"),
            ("data_json", "NVARCHAR(MAX) NOT NULL"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("account", "number"),
        [_idx("idx_sales_orders_date", ["date"]), _idx("idx_sales_orders_customer", ["customer_name"])],
    ),
    "sales_details": TableDef(
        "sales_details",
        [
            ("account", "NVARCHAR(100) NOT NULL"),
            ("number", "NVARCHAR(100) NOT NULL"),
            ("date", "NVARCHAR(32) NULL"),
            ("data_json", "NVARCHAR(MAX) NOT NULL"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("account", "number"),
    ),
    "transfer_orders": TableDef(
        "transfer_orders",
        [
            ("account", "NVARCHAR(100) NOT NULL"),
            ("number", "NVARCHAR(100) NOT NULL"),
            ("date", "NVARCHAR(32) NULL"),
            ("out_location", "NVARCHAR(255) NULL"),
            ("check_status", "NVARCHAR(100) NULL"),
            ("remark", "NVARCHAR(MAX) NULL"),
            ("data_json", "NVARCHAR(MAX) NOT NULL"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("account", "number"),
        [_idx("idx_transfer_orders_date", ["date"]), _idx("idx_transfer_orders_out_location", ["out_location"])],
    ),
    "transfer_details": TableDef(
        "transfer_details",
        [
            ("account", "NVARCHAR(100) NOT NULL"),
            ("number", "NVARCHAR(100) NOT NULL"),
            ("date", "NVARCHAR(32) NULL"),
            ("data_json", "NVARCHAR(MAX) NOT NULL"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("account", "number"),
    ),
    "sales_product_quantities": TableDef(
        "sales_product_quantities",
        [
            ("account", "NVARCHAR(100) NOT NULL"),
            ("code", "NVARCHAR(120) NOT NULL"),
            ("stock_new", "FLOAT NOT NULL DEFAULT 0"),
            ("stock_transit", "FLOAT NOT NULL DEFAULT 0"),
            ("stock_local", "FLOAT NOT NULL DEFAULT 0"),
            ("stock_factory", "FLOAT NOT NULL DEFAULT 0"),
            ("factory_qty", "FLOAT NOT NULL DEFAULT 0"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
            ("error", "NVARCHAR(MAX) NOT NULL DEFAULT ''"),
        ],
        ("account", "code"),
    ),
    "inventory_snapshots": TableDef(
        "inventory_snapshots",
        [
            ("id", "BIGINT IDENTITY(1,1) NOT NULL"),
            ("account", "NVARCHAR(100) NOT NULL"),
            ("product_number", "NVARCHAR(120) NULL"),
            ("normalized_product_number", "NVARCHAR(120) NULL"),
            ("barcode", "NVARCHAR(120) NULL"),
            ("product_name", "NVARCHAR(255) NULL"),
            ("warehouse_name", "NVARCHAR(255) NOT NULL"),
            ("quantity", "FLOAT NOT NULL DEFAULT 0"),
            ("unit", "NVARCHAR(50) NULL"),
            ("snapshot_at", "NVARCHAR(32) NOT NULL"),
            ("source", "NVARCHAR(100) NOT NULL DEFAULT 'jdy_inventory'"),
            ("data_json", "NVARCHAR(MAX) NULL"),
            ("created_at", "NVARCHAR(32) NOT NULL DEFAULT CONVERT(nvarchar(32), SYSDATETIME(), 120)"),
            ("updated_at", "NVARCHAR(32) NOT NULL DEFAULT CONVERT(nvarchar(32), SYSDATETIME(), 120)"),
        ],
        ("id",),
        [
            _idx("idx_inventory_snapshots_account_product", ["account", "product_number"]),
            _idx("idx_inventory_snapshots_account_normalized_product", ["account", "normalized_product_number"]),
            _idx("idx_inventory_snapshots_account_barcode", ["account", "barcode"]),
            _idx("idx_inventory_snapshots_account_warehouse", ["account", "warehouse_name"]),
            _idx("idx_inventory_snapshots_account_snapshot", ["account", "snapshot_at"]),
        ],
    ),
    "accessory_purchase_orders": TableDef(
        "accessory_purchase_orders",
        [
            ("account", "NVARCHAR(100) NOT NULL"),
            ("number", "NVARCHAR(100) NOT NULL"),
            ("date", "NVARCHAR(32) NULL"),
            ("supplier_name", "NVARCHAR(255) NULL"),
            ("supplier_number", "NVARCHAR(120) NULL"),
            ("total_qty", "FLOAT NOT NULL DEFAULT 0"),
            ("total_amount", "FLOAT NOT NULL DEFAULT 0"),
            ("bill_status", "NVARCHAR(100) NULL"),
            ("bill_status_name", "NVARCHAR(100) NULL"),
            ("entries_count", "INT NOT NULL DEFAULT 0"),
            ("data_json", "NVARCHAR(MAX) NOT NULL"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("account", "number"),
        [_idx("idx_accessory_po_supplier", ["supplier_name"]), _idx("idx_accessory_po_date", ["date"])],
    ),
    "accessory_products": TableDef(
        "accessory_products",
        [
            ("account", "NVARCHAR(100) NOT NULL"),
            ("code", "NVARCHAR(120) NOT NULL"),
            ("name", "NVARCHAR(255) NULL"),
            ("category", "NVARCHAR(255) NULL"),
            ("data_json", "NVARCHAR(MAX) NOT NULL"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("account", "code"),
        [_idx("idx_accessory_products_category", ["category"])],
    ),
    "jdy_suppliers": TableDef(
        "jdy_suppliers",
        [
            ("account", "NVARCHAR(100) NOT NULL"),
            ("number", "NVARCHAR(120) NOT NULL"),
            ("name", "NVARCHAR(255) NULL"),
            ("category_text", "NVARCHAR(MAX) NULL"),
            ("data_json", "NVARCHAR(MAX) NOT NULL"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
            ("status_code", "NVARCHAR(32) NULL"),
            ("status", "NVARCHAR(32) NULL"),
            ("status_name", "NVARCHAR(100) NULL"),
            ("contact", "NVARCHAR(255) NULL"),
            ("phone", "NVARCHAR(100) NULL"),
            ("last_seen_at", "NVARCHAR(32) NULL"),
            ("last_seen_enabled_at", "NVARCHAR(32) NULL"),
            ("disabled_at", "NVARCHAR(32) NULL"),
            ("manual_category_text", "NVARCHAR(MAX) NULL"),
            ("manual_tags", "NVARCHAR(MAX) NULL"),
            ("manual_note", "NVARCHAR(MAX) NULL"),
            ("accessory_override", "NVARCHAR(100) NULL"),
        ],
        ("account", "number"),
        [_idx("idx_jdy_suppliers_account_name", ["account", "name"])],
    ),
    "jdy_products": TableDef(
        "jdy_products",
        [
            ("id", "BIGINT IDENTITY(1,1) NOT NULL"),
            ("account", "NVARCHAR(100) NULL"),
            ("product_id", "NVARCHAR(120) NULL"),
            ("product_number", "NVARCHAR(120) NULL"),
            ("product_name", "NVARCHAR(255) NULL"),
            ("spec", "NVARCHAR(MAX) NULL"),
            ("barcode", "NVARCHAR(120) NULL"),
            ("category_id", "NVARCHAR(120) NULL"),
            ("category_name", "NVARCHAR(255) NULL"),
            ("unit_id", "NVARCHAR(120) NULL"),
            ("unit_name", "NVARCHAR(100) NULL"),
            ("default_supplier_id", "NVARCHAR(120) NULL"),
            ("default_supplier_number", "NVARCHAR(120) NULL"),
            ("default_supplier_name", "NVARCHAR(255) NULL"),
            ("image_url", "NVARCHAR(MAX) NULL"),
            ("status", "NVARCHAR(100) NULL"),
            ("data_json", "NVARCHAR(MAX) NULL"),
            ("created_at", "NVARCHAR(32) NULL"),
            ("updated_at", "NVARCHAR(32) NULL"),
            ("last_seen_at", "NVARCHAR(32) NULL"),
        ],
        ("id",),
        [
            _idx("idx_jdy_products_unique", ["account", "product_number"], True),
            _idx("idx_jdy_products_name", ["account", "product_name"]),
            _idx("idx_jdy_products_category", ["account", "category_name"]),
            _idx("idx_jdy_products_updated", ["account", "updated_at"]),
            _idx("idx_jdy_products_seen", ["account", "last_seen_at"]),
        ],
    ),
    "webhook_events": TableDef(
        "webhook_events",
        [
            ("id", "BIGINT IDENTITY(1,1) NOT NULL"),
            ("account", "NVARCHAR(100) NULL"),
            ("biz_type", "NVARCHAR(100) NULL"),
            ("resource", "NVARCHAR(100) NULL"),
            ("bill_no", "NVARCHAR(120) NULL"),
            ("action", "NVARCHAR(100) NULL"),
            ("status", "NVARCHAR(32) NOT NULL DEFAULT 'pending'"),
            ("attempts", "INT NOT NULL DEFAULT 0"),
            ("payload_json", "NVARCHAR(MAX) NOT NULL"),
            ("error", "NVARCHAR(MAX) NULL"),
            ("created_at", "NVARCHAR(32) NOT NULL"),
            ("processed_at", "NVARCHAR(32) NULL DEFAULT ''"),
            ("event_id", "NVARCHAR(120) NULL"),
            ("payload_hash", "NVARCHAR(128) NULL"),
            ("updated_at", "NVARCHAR(32) NULL"),
        ],
        ("id",),
        [
            _idx("idx_webhook_events_status", ["status", "id"]),
            _idx("idx_webhook_events_bill", ["account", "bill_no", "resource"]),
            _idx("idx_webhook_events_event_id", ["event_id", "account", "resource", "bill_no"]),
            _idx("idx_webhook_events_payload_hash", ["payload_hash"]),
        ],
    ),
    "webhook_runtime_settings": TableDef(
        "webhook_runtime_settings",
        [
            ("id", "INT NOT NULL"),
            ("processing_mode", "NVARCHAR(32) NOT NULL DEFAULT 'manual'"),
            ("auto_enabled", "INT NOT NULL DEFAULT 0"),
            ("paused", "INT NOT NULL DEFAULT 1"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("id",),
    ),
    "reorder_items": TableDef(
        "reorder_items",
        [
            ("id", "BIGINT IDENTITY(1,1) NOT NULL"),
            ("account", "NVARCHAR(100) NULL"),
            ("supplier_number", "NVARCHAR(120) NULL"),
            ("supplier_name", "NVARCHAR(255) NULL"),
            ("code", "NVARCHAR(120) NOT NULL"),
            ("name", "NVARCHAR(255) NULL"),
            ("spec", "NVARCHAR(MAX) NULL"),
            ("barcode", "NVARCHAR(120) NULL"),
            ("unit", "NVARCHAR(50) NULL"),
            ("image_url", "NVARCHAR(MAX) NULL"),
            ("source_type", "NVARCHAR(100) NULL"),
            ("source_number", "NVARCHAR(120) NULL"),
            ("source_date", "NVARCHAR(32) NULL"),
            ("source_customer", "NVARCHAR(255) NULL"),
            ("source_user", "NVARCHAR(120) NULL"),
            ("sales_qty_60", "FLOAT NOT NULL DEFAULT 0"),
            ("stock_new", "FLOAT NOT NULL DEFAULT 0"),
            ("stock_transit", "FLOAT NOT NULL DEFAULT 0"),
            ("stock_local", "FLOAT NOT NULL DEFAULT 0"),
            ("stock_factory", "FLOAT NOT NULL DEFAULT 0"),
            ("factory_qty", "FLOAT NOT NULL DEFAULT 0"),
            ("suggested_qty", "FLOAT NOT NULL DEFAULT 0"),
            ("confirmed_qty", "FLOAT NOT NULL DEFAULT 0"),
            ("status", "NVARCHAR(32) NOT NULL DEFAULT 'pending'"),
            ("note", "NVARCHAR(MAX) NULL DEFAULT ''"),
            ("created_by", "NVARCHAR(120) NULL"),
            ("created_by_name", "NVARCHAR(120) NULL"),
            ("reorder_price", "FLOAT NOT NULL DEFAULT 0"),
            ("generated_batch_no", "NVARCHAR(120) NULL"),
            ("generated_at", "NVARCHAR(32) NULL"),
            ("sync_status", "NVARCHAR(32) NULL DEFAULT 'not_synced'"),
            ("jdy_order_no", "NVARCHAR(120) NULL"),
            ("synced_at", "NVARCHAR(32) NULL"),
            ("sync_error", "NVARCHAR(MAX) NULL"),
            ("data_json", "NVARCHAR(MAX) NOT NULL"),
            ("created_at", "NVARCHAR(32) NOT NULL"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("id",),
        [
            _idx("idx_reorder_items_supplier", ["status", "supplier_name", "supplier_number"]),
            _idx("idx_reorder_items_code", ["account", "code", "status"]),
            _idx("idx_reorder_items_picker", ["status", "created_by_name", "created_by"]),
            _idx("idx_reorder_items_source", ["account", "source_number", "code"]),
            _idx("idx_reorder_items_batch", ["generated_batch_no", "sync_status", "generated_at"]),
        ],
    ),
    "purchase_inbounds": TableDef(
        "purchase_inbounds",
        [
            ("account", "NVARCHAR(100) NOT NULL"),
            ("number", "NVARCHAR(120) NOT NULL"),
            ("date", "NVARCHAR(32) NULL"),
            ("supplier_number", "NVARCHAR(120) NULL"),
            ("supplier_name", "NVARCHAR(255) NULL"),
            ("total_qty", "FLOAT NOT NULL DEFAULT 0"),
            ("total_amount", "FLOAT NOT NULL DEFAULT 0"),
            ("data_json", "NVARCHAR(MAX) NOT NULL"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("account", "number"),
        [_idx("idx_purchase_inbounds_code_json", ["account", "number"])],
    ),
    "purchase_inbound_attachments": TableDef(
        "purchase_inbound_attachments",
        [
            ("account", "NVARCHAR(100) NOT NULL"),
            ("number", "NVARCHAR(120) NOT NULL"),
            ("attachment_key", "NVARCHAR(255) NOT NULL"),
            ("name", "NVARCHAR(255) NULL"),
            ("url", "NVARCHAR(MAX) NULL"),
            ("local_path", "NVARCHAR(MAX) NULL"),
            ("data_json", "NVARCHAR(MAX) NOT NULL"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("account", "number", "attachment_key"),
    ),
    "bill_attachments": TableDef(
        "bill_attachments",
        [
            ("id", "BIGINT IDENTITY(1,1) NOT NULL"),
            ("account", "NVARCHAR(100) NULL"),
            ("source_type", "NVARCHAR(100) NULL"),
            ("source_number", "NVARCHAR(120) NULL"),
            ("source_date", "NVARCHAR(32) NULL"),
            ("bill_type", "NVARCHAR(100) NULL"),
            ("attachment_key", "NVARCHAR(255) NULL"),
            ("name", "NVARCHAR(255) NULL"),
            ("url", "NVARCHAR(MAX) NULL"),
            ("local_path", "NVARCHAR(MAX) NULL"),
            ("file_mime", "NVARCHAR(120) NULL"),
            ("file_size", "BIGINT NOT NULL DEFAULT 0"),
            ("download_status", "NVARCHAR(100) NULL DEFAULT ''"),
            ("download_error", "NVARCHAR(MAX) NULL DEFAULT ''"),
            ("data_json", "NVARCHAR(MAX) NULL"),
            ("created_at", "NVARCHAR(32) NULL"),
            ("updated_at", "NVARCHAR(32) NULL"),
        ],
        ("id",),
        [
            _idx("idx_bill_attachments_unique", ["account", "source_type", "source_number", "attachment_key"], True),
            _idx("idx_bill_attachments_source", ["account", "source_type", "source_number"]),
            _idx("idx_bill_attachments_date", ["source_date"]),
            _idx("idx_bill_attachments_status", ["download_status"]),
        ],
    ),
    "purchase_order_prices": TableDef(
        "purchase_order_prices",
        [
            ("id", "BIGINT IDENTITY(1,1) NOT NULL"),
            ("account", "NVARCHAR(100) NOT NULL"),
            ("order_number", "NVARCHAR(120) NOT NULL"),
            ("order_date", "NVARCHAR(32) NOT NULL"),
            ("supplier_name", "NVARCHAR(255) NULL"),
            ("supplier_number", "NVARCHAR(120) NULL"),
            ("product_number", "NVARCHAR(120) NOT NULL"),
            ("price", "FLOAT NOT NULL"),
            ("qty", "FLOAT NOT NULL DEFAULT 0"),
            ("unit", "NVARCHAR(50) NULL DEFAULT ''"),
            ("created_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("id",),
        [_idx("idx_pop_product", ["account", "product_number"])],
    ),
    "purchase_attachment_items": TableDef(
        "purchase_attachment_items",
        [
            ("id", "BIGINT IDENTITY(1,1) NOT NULL"),
            ("account", "NVARCHAR(100) NULL"),
            ("source_type", "NVARCHAR(100) NULL"),
            ("source_number", "NVARCHAR(120) NULL"),
            ("source_date", "NVARCHAR(32) NULL"),
            ("supplier_number", "NVARCHAR(120) NULL"),
            ("supplier_name", "NVARCHAR(255) NULL"),
            ("product_code", "NVARCHAR(120) NULL"),
            ("product_name", "NVARCHAR(255) NULL"),
            ("spec", "NVARCHAR(MAX) NULL"),
            ("qty", "FLOAT NULL"),
            ("unit", "NVARCHAR(50) NULL"),
            ("price", "FLOAT NULL"),
            ("amount", "FLOAT NULL"),
            ("attachment_key", "NVARCHAR(255) NULL"),
            ("attachment_id", "BIGINT NULL"),
            ("data_json", "NVARCHAR(MAX) NULL"),
            ("created_at", "NVARCHAR(32) NULL"),
            ("updated_at", "NVARCHAR(32) NULL"),
        ],
        ("id",),
        [
            _idx("idx_purchase_attachment_items_product", ["account", "product_code"]),
            _idx("idx_purchase_attachment_items_supplier", ["account", "supplier_number"]),
            _idx("idx_purchase_attachment_items_source", ["account", "source_type", "source_number"]),
            _idx("idx_purchase_attachment_items_date", ["source_date"]),
            _idx(
                "idx_purchase_attachment_items_unique",
                ["account", "source_type", "source_number", "product_code", "attachment_key"],
                True,
            ),
        ],
    ),
    "customs_product_master": TableDef(
        "customs_product_master",
        [
            ("id", "BIGINT IDENTITY(1,1) NOT NULL"),
            ("account", "NVARCHAR(100) NULL"),
            ("product_code", "NVARCHAR(120) NOT NULL"),
            ("product_name", "NVARCHAR(255) NULL"),
            ("category_name", "NVARCHAR(255) NULL"),
            ("spec", "NVARCHAR(MAX) NULL"),
            ("unit", "NVARCHAR(50) NULL"),
            ("barcode", "NVARCHAR(120) NULL"),
            ("jdy_product_id", "NVARCHAR(120) NULL"),
            ("customs_name_cn", "NVARCHAR(255) NULL"),
            ("customs_name_en", "NVARCHAR(255) NULL"),
            ("hs_code", "NVARCHAR(120) NULL"),
            ("customs_unit", "NVARCHAR(100) NULL"),
            ("origin", "NVARCHAR(100) NULL"),
            ("material", "NVARCHAR(MAX) NULL"),
            ("usage", "NVARCHAR(MAX) NULL"),
            ("tax_refund_rate", "FLOAT NULL"),
            ("is_tax_refund", "NVARCHAR(50) NULL"),
            ("gross_weight_per_pkg", "FLOAT NULL"),
            ("net_weight_per_pkg", "FLOAT NULL"),
            ("carton_length", "FLOAT NULL"),
            ("carton_width", "FLOAT NULL"),
            ("carton_height", "FLOAT NULL"),
            ("carton_volume", "FLOAT NULL"),
            ("pcs_per_pkg", "FLOAT NULL"),
            ("supplier_number", "NVARCHAR(120) NULL"),
            ("tax_code", "NVARCHAR(120) NULL"),
            ("tax_category_short_name", "NVARCHAR(255) NULL"),
            ("source", "NVARCHAR(120) NULL"),
            ("source_excel_path", "NVARCHAR(MAX) NULL"),
            ("source_row_no", "BIGINT NULL"),
            ("created_at", "NVARCHAR(32) NULL"),
            ("updated_at", "NVARCHAR(32) NULL"),
            ("updated_by", "NVARCHAR(120) NULL"),
        ],
        ("id",),
        [
            _idx("idx_customs_product_code", ["product_code"], True),
            _idx("idx_customs_product_account", ["account"]),
            _idx("idx_customs_product_source", ["source"]),
            _idx("idx_customs_product_updated", ["updated_at"]),
        ],
    ),
    "customs_product_master_history": TableDef(
        "customs_product_master_history",
        [
            ("id", "BIGINT IDENTITY(1,1) NOT NULL"),
            ("product_code", "NVARCHAR(120) NOT NULL"),
            ("changed_at", "NVARCHAR(32) NOT NULL"),
            ("changed_by", "NVARCHAR(120) NULL"),
            ("change_source", "NVARCHAR(120) NULL"),
            ("old_json", "NVARCHAR(MAX) NULL"),
            ("new_json", "NVARCHAR(MAX) NULL"),
            ("created_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("id",),
        [_idx("idx_customs_product_history_code", ["product_code", "changed_at"])],
    ),
    "product_create_sequences": TableDef(
        "product_create_sequences",
        [
            ("account_key", "NVARCHAR(50) NOT NULL"),
            ("scope", "NVARCHAR(50) NOT NULL DEFAULT 'global'"),
            ("current_seq", "BIGINT NOT NULL DEFAULT 0"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("account_key", "scope"),
    ),
    "product_create_draft": TableDef(
        "product_create_draft",
        [
            ("id", "BIGINT IDENTITY(1,1) NOT NULL"),
            ("draft_no", "NVARCHAR(120) NOT NULL"),
            ("account_key", "NVARCHAR(50) NOT NULL"),
            ("account", "NVARCHAR(100) NOT NULL DEFAULT ''"),
            ("title", "NVARCHAR(255) NULL DEFAULT ''"),
            ("status", "NVARCHAR(32) NOT NULL DEFAULT 'draft'"),
            ("idempotency_key", "NVARCHAR(128) NOT NULL"),
            ("payload_hash", "NVARCHAR(128) NULL DEFAULT ''"),
            ("source", "NVARCHAR(100) NULL DEFAULT 'quick_create'"),
            ("created_by", "NVARCHAR(120) NULL DEFAULT ''"),
            ("created_by_name", "NVARCHAR(120) NULL DEFAULT ''"),
            ("created_at", "NVARCHAR(32) NOT NULL"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
            ("validated_at", "NVARCHAR(32) NULL DEFAULT ''"),
            ("dry_run_at", "NVARCHAR(32) NULL DEFAULT ''"),
            ("approved_at", "NVARCHAR(32) NULL DEFAULT ''"),
            ("approved_by", "NVARCHAR(120) NULL DEFAULT ''"),
            ("submitted_at", "NVARCHAR(32) NULL DEFAULT ''"),
            ("submitted_by", "NVARCHAR(120) NULL DEFAULT ''"),
            ("submit_confirm_code", "NVARCHAR(120) NULL DEFAULT ''"),
            ("locked_at", "NVARCHAR(32) NULL DEFAULT ''"),
            ("locked_by", "NVARCHAR(120) NULL DEFAULT ''"),
            ("lock_token", "NVARCHAR(120) NULL DEFAULT ''"),
            ("last_error", "NVARCHAR(MAX) NULL DEFAULT ''"),
            ("jdy_result_json", "NVARCHAR(MAX) NULL DEFAULT '{}'"),
            ("sync_status", "NVARCHAR(100) NULL DEFAULT 'not_needed'"),
            ("data_json", "NVARCHAR(MAX) NULL DEFAULT '{}'"),
        ],
        ("id",),
        [
            _idx("idx_product_create_draft_no", ["draft_no"], True),
            _idx("idx_product_create_draft_idempotency", ["idempotency_key"], True),
            _idx("idx_product_create_draft_status", ["status", "updated_at"]),
            _idx("idx_product_create_draft_account", ["account_key", "updated_at"]),
        ],
    ),
    "product_create_draft_item": TableDef(
        "product_create_draft_item",
        [
            ("id", "BIGINT IDENTITY(1,1) NOT NULL"),
            ("draft_id", "BIGINT NOT NULL"),
            ("line_no", "INT NOT NULL DEFAULT 1"),
            ("status", "NVARCHAR(32) NOT NULL DEFAULT 'draft'"),
            ("account_key", "NVARCHAR(50) NOT NULL"),
            ("account", "NVARCHAR(100) NOT NULL DEFAULT ''"),
            ("product_number", "NVARCHAR(120) NULL DEFAULT ''"),
            ("product_name", "NVARCHAR(255) NOT NULL"),
            ("spec", "NVARCHAR(MAX) NULL DEFAULT ''"),
            ("category_id", "NVARCHAR(120) NULL DEFAULT ''"),
            ("category_name", "NVARCHAR(255) NULL DEFAULT ''"),
            ("category_prefix", "NVARCHAR(50) NULL DEFAULT ''"),
            ("category_small_code", "NVARCHAR(50) NULL DEFAULT ''"),
            ("category_code_2d", "NVARCHAR(50) NULL DEFAULT ''"),
            ("unit_id", "NVARCHAR(120) NULL DEFAULT ''"),
            ("unit_name", "NVARCHAR(100) NULL DEFAULT ''"),
            ("supplier_id", "NVARCHAR(120) NULL DEFAULT ''"),
            ("supplier_number", "NVARCHAR(120) NULL DEFAULT ''"),
            ("supplier_name", "NVARCHAR(255) NULL DEFAULT ''"),
            ("tax_no", "NVARCHAR(120) NULL DEFAULT ''"),
            ("transformed_code", "NVARCHAR(120) NULL DEFAULT ''"),
            ("purchase_price", "FLOAT NULL DEFAULT 0"),
            ("purchase_qty", "FLOAT NULL DEFAULT 1"),
            ("sale_price", "FLOAT NULL DEFAULT 0"),
            ("image_local_path", "NVARCHAR(MAX) NULL DEFAULT ''"),
            ("image_b64", "NVARCHAR(MAX) NULL DEFAULT ''"),
            ("image_sha256", "NVARCHAR(128) NULL DEFAULT ''"),
            ("ka_barcode", "NVARCHAR(120) NULL DEFAULT ''"),
            ("remark", "NVARCHAR(MAX) NULL DEFAULT ''"),
            ("color", "NVARCHAR(120) NULL DEFAULT ''"),
            ("seq_preview", "BIGINT NULL DEFAULT 0"),
            ("seq_reserved", "BIGINT NULL DEFAULT 0"),
            ("ean13", "NVARCHAR(32) NULL DEFAULT ''"),
            ("jdy_product_id", "NVARCHAR(120) NULL DEFAULT ''"),
            ("jdy_error", "NVARCHAR(MAX) NULL DEFAULT ''"),
            ("payload_json", "NVARCHAR(MAX) NULL DEFAULT '{}'"),
            ("validation_json", "NVARCHAR(MAX) NULL DEFAULT '{}'"),
            ("customs_json", "NVARCHAR(MAX) NULL DEFAULT '{}'"),
            ("created_at", "NVARCHAR(32) NOT NULL"),
            ("updated_at", "NVARCHAR(32) NOT NULL"),
        ],
        ("id",),
        [
            _idx("idx_product_create_item_unique_line", ["draft_id", "line_no"], True),
            _idx("idx_product_create_item_draft", ["draft_id", "line_no"]),
            _idx("idx_product_create_item_product", ["account_key", "product_number"]),
        ],
    ),
    "product_create_submit_log": TableDef(
        "product_create_submit_log",
        [
            ("id", "BIGINT IDENTITY(1,1) NOT NULL"),
            ("draft_id", "BIGINT NOT NULL"),
            ("item_id", "BIGINT NULL DEFAULT 0"),
            ("action", "NVARCHAR(100) NOT NULL"),
            ("endpoint", "NVARCHAR(255) NOT NULL DEFAULT ''"),
            ("status", "NVARCHAR(50) NOT NULL"),
            ("idempotency_key", "NVARCHAR(255) NOT NULL"),
            ("payload_hash", "NVARCHAR(128) NOT NULL"),
            ("payload_json", "NVARCHAR(MAX) NOT NULL"),
            ("response_json", "NVARCHAR(MAX) NULL DEFAULT '{}'"),
            ("error", "NVARCHAR(MAX) NULL DEFAULT ''"),
            ("attempts", "INT NOT NULL DEFAULT 0"),
            ("called_jdy", "INT NOT NULL DEFAULT 0"),
            ("dry_run", "INT NOT NULL DEFAULT 1"),
            ("actor", "NVARCHAR(120) NULL DEFAULT ''"),
            ("actor_role", "NVARCHAR(50) NULL DEFAULT ''"),
            ("created_at", "NVARCHAR(32) NOT NULL"),
            ("finished_at", "NVARCHAR(32) NULL DEFAULT ''"),
        ],
        ("id",),
        [
            _idx("idx_product_create_log_unique", ["idempotency_key", "action", "payload_hash"], True),
            _idx("idx_product_create_log_draft", ["draft_id", "created_at"]),
        ],
    ),
}


SCHEMA_TABLE_ORDER = tuple(TABLES.keys())
IDENTITY_TABLES = {
    name
    for name, definition in TABLES.items()
    if definition.columns and "IDENTITY" in definition.columns[0][1].upper()
}


def table_names() -> list[str]:
    return list(SCHEMA_TABLE_ORDER)


def _column_lookup(definition: TableDef) -> dict[str, str]:
    return {name.lower(): name for name, _ in definition.columns}


def table_columns_from_definition(table: str) -> list[str]:
    definition = TABLES[str(table)]
    return [name for name, _ in definition.columns]


def generate_mssql_schema_sql(tables: Sequence[str] | None = None) -> list[str]:
    wanted = list(tables or SCHEMA_TABLE_ORDER)
    statements: list[str] = []
    for table in wanted:
        definition = TABLES[str(table)]
        quoted = quote_table(definition.name)
        schema, table_name = split_table_name(definition.name)
        object_name = f"{schema}.{table_name}"
        lines = [f"    {quote_ident(name)} {spec}" for name, spec in definition.columns]
        if definition.primary_key:
            pk_cols = ", ".join(quote_ident(col) for col in definition.primary_key)
            lines.append(f"    CONSTRAINT {quote_ident('PK_' + definition.name)} PRIMARY KEY ({pk_cols})")
        statements.append(
            f"IF OBJECT_ID(N'{object_name}', N'U') IS NULL\n"
            "BEGIN\n"
            f"CREATE TABLE {quoted} (\n" + ",\n".join(lines) + "\n);\n"
            "END"
        )
    for table in wanted:
        definition = TABLES[str(table)]
        quoted = quote_table(definition.name)
        schema, table_name = split_table_name(definition.name)
        object_name = f"{schema}.{table_name}"
        for name, columns, unique in definition.indexes:
            cols = ", ".join(quote_ident(col) for col in columns)
            prefix = "CREATE UNIQUE INDEX" if unique else "CREATE INDEX"
            statements.append(
                f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'{name}' "
                f"AND object_id = OBJECT_ID(N'{object_name}'))\n"
                f"{prefix} {quote_ident(name)} ON {quoted} ({cols})"
            )
    return statements


def ensure_mssql_cache_schema(conn: Any, tables: Sequence[str] | None = None) -> None:
    if not is_mssql_connection(conn):
        return
    for statement in generate_mssql_schema_sql(tables):
        conn.execute(statement)
    conn.commit()


def table_exists(conn: Any, table: str) -> bool:
    if not is_mssql_connection(conn):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
        return bool(row)
    schema, table_name = split_table_name(table)
    row = conn.execute(
        """
        SELECT TOP (1) 1
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND TABLE_TYPE = 'BASE TABLE'
        """,
        (schema, table_name),
    ).fetchone()
    return bool(row)


def table_columns(conn: Any, table: str) -> list[str]:
    if not is_mssql_connection(conn):
        return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def build_mssql_upsert_sql(
    table: str,
    columns: Sequence[str],
    key_columns: Sequence[str],
    preserve_update_columns: Sequence[str] = (),
) -> dict[str, str]:
    column_list = [str(col) for col in columns]
    key_list = [str(col) for col in key_columns]
    preserve = {str(col).lower() for col in preserve_update_columns}
    where = " AND ".join(f"{quote_ident(col)} = ?" for col in key_list)
    update_cols = [col for col in column_list if col not in key_list and col.lower() not in preserve]
    set_sql = ", ".join(f"{quote_ident(col)} = ?" for col in update_cols)
    insert_cols = ", ".join(quote_ident(col) for col in column_list)
    insert_marks = ", ".join("?" for _ in column_list)
    quoted = quote_table(table)
    return {
        "exists": f"SELECT TOP (1) 1 FROM {quoted} WHERE {where}",
        "update": f"UPDATE {quoted} SET {set_sql} WHERE {where}" if update_cols else "",
        "insert": f"INSERT INTO {quoted} ({insert_cols}) VALUES ({insert_marks})",
    }


def upsert_by_key(
    conn: Any,
    table: str,
    values: Mapping[str, Any],
    key_columns: Sequence[str],
    preserve_update_columns: Sequence[str] = (),
) -> str:
    if not is_mssql_connection(conn):
        raise TypeError("upsert_by_key is intended for SQL Server connections")
    columns = list(values.keys())
    sql = build_mssql_upsert_sql(table, columns, key_columns, preserve_update_columns)
    key_params = [values[col] for col in key_columns]
    exists = conn.execute(sql["exists"], key_params).fetchone()
    if exists:
        update_cols = [
            col
            for col in columns
            if col not in key_columns and col.lower() not in {x.lower() for x in preserve_update_columns}
        ]
        if sql["update"]:
            conn.execute(sql["update"], [values[col] for col in update_cols] + key_params)
        return "updated"
    conn.execute(sql["insert"], [values[col] for col in columns])
    return "inserted"


def insert_if_missing(
    conn: Any,
    table: str,
    values: Mapping[str, Any],
    key_columns: Sequence[str],
) -> bool:
    if not is_mssql_connection(conn):
        raise TypeError("insert_if_missing is intended for SQL Server connections")
    columns = list(values.keys())
    sql = build_mssql_upsert_sql(table, columns, key_columns)
    key_params = [values[col] for col in key_columns]
    if conn.execute(sql["exists"], key_params).fetchone():
        return False
    conn.execute(sql["insert"], [values[col] for col in columns])
    return True


def connect_cache(readonly: bool = False) -> MssqlConnection:
    _ = readonly
    return connect_mssql()
