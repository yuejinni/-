"""Dry-run-first SQLite to SQL Server cache migration tool."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db_backend


WRITE_CONFIRM = "MIGRATE_SQLITE_TO_SQLSERVER"
TRUNCATE_CONFIRM = "TRUNCATE_SQLSERVER_TARGET"


def _parse_tables(value: str) -> list[str]:
    if not value:
        return db_backend.table_names()
    requested = [x.strip() for x in value.split(",") if x.strip()]
    unknown = [x for x in requested if x not in db_backend.TABLES]
    if unknown:
        raise SystemExit(f"unknown table(s): {', '.join(unknown)}")
    return requested


def _confirm_tokens(confirm: list[str] | str) -> set[str]:
    values = confirm if isinstance(confirm, list) else [confirm]
    tokens: set[str] = set()
    for value in values or []:
        tokens.update(x.strip() for x in str(value or "").split(",") if x.strip())
    return tokens


def _sqlite_conn(path: str):
    uri = "file:" + str(Path(path).resolve()).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _sqlite_columns(conn, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _count(conn, table: str, backend: str) -> int:
    if backend == db_backend.BACKEND_MSSQL:
        sql = f"SELECT COUNT(*) AS c FROM {db_backend.quote_table(table)}"
    else:
        sql = f"SELECT COUNT(*) AS c FROM {table}"
    row = conn.execute(sql).fetchone()
    return int(row["c"] if hasattr(row, "keys") else row[0])


def _chunks(rows: Iterable[sqlite3.Row], size: int):
    batch = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _target_columns_for_table(sqlite_conn, table: str) -> list[str]:
    sqlite_cols = set(_sqlite_columns(sqlite_conn, table))
    definition_cols = db_backend.table_columns_from_definition(table)
    return [
        col
        for col in definition_cols
        if col in sqlite_cols and not (table in db_backend.IDENTITY_TABLES and col == "id")
    ]


def _insert_batch(mssql_conn, table: str, columns: list[str], rows: list[sqlite3.Row]) -> int:
    if not rows:
        return 0
    quoted = db_backend.quote_table(table)
    col_sql = ", ".join(db_backend.quote_ident(col) for col in columns)
    marks = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {quoted} ({col_sql}) VALUES ({marks})"
    params = [[row[col] for col in columns] for row in rows]
    mssql_conn.executemany(sql, params)
    return len(params)


def _truncate_table(mssql_conn, table: str) -> None:
    quoted = db_backend.quote_table(table)
    try:
        mssql_conn.execute(f"TRUNCATE TABLE {quoted}")
    except Exception:
        mssql_conn.execute(f"DELETE FROM {quoted}")


def _table_plan(sqlite_conn, table: str) -> dict[str, Any]:
    exists = _sqlite_table_exists(sqlite_conn, table)
    count = _count(sqlite_conn, table, db_backend.BACKEND_SQLITE) if exists else 0
    columns = _target_columns_for_table(sqlite_conn, table) if exists else []
    return {
        "table": table,
        "source_exists": exists,
        "source_rows": count,
        "columns": columns,
        "estimated_write_rows": count if exists and columns else 0,
        "skipped_reason": "" if exists and columns else ("missing source table" if not exists else "no common columns"),
    }


def _print_plan(plan: list[dict[str, Any]]) -> None:
    print("migration plan:")
    for item in plan:
        columns = ",".join(item["columns"][:8])
        if len(item["columns"]) > 8:
            columns += ",..."
        print(
            f"- {item['table']}: exists={item['source_exists']} "
            f"source_rows={item['source_rows']} estimated_write_rows={item['estimated_write_rows']} "
            f"columns=[{columns}]"
            + (f" skipped={item['skipped_reason']}" if item["skipped_reason"] else "")
        )


def migrate(args) -> int:
    tables = _parse_tables(args.tables)
    sqlite_path = args.sqlite or os.environ.get("QIHANG_SQLITE_CACHE_PATH") or ""
    if not sqlite_path:
        raise SystemExit("--sqlite is required unless QIHANG_SQLITE_CACHE_PATH is set")
    if not Path(sqlite_path).exists():
        raise SystemExit(f"SQLite source does not exist: {sqlite_path}")

    write = bool(args.write)
    dry_run = bool(args.dry_run or not write)
    confirms = _confirm_tokens(args.confirm)
    if write and WRITE_CONFIRM not in confirms:
        raise SystemExit(f"refusing to write SQL Server without --confirm {WRITE_CONFIRM}")
    if args.truncate_target and TRUNCATE_CONFIRM not in confirms:
        raise SystemExit(f"refusing to truncate SQL Server without --confirm {TRUNCATE_CONFIRM}")

    if args.truncate_target and not write:
        print("--truncate-target ignored because this is a dry-run")

    sqlite_conn = _sqlite_conn(sqlite_path)
    try:
        plan = [_table_plan(sqlite_conn, table) for table in tables]
        _print_plan(plan)
        if dry_run:
            print("dry-run only: SQL Server was not opened and no data was written")
            return 0

        mssql_conn = db_backend.connect_mssql(args.sqlserver_conn or "")
        try:
            db_backend.ensure_mssql_cache_schema(mssql_conn, tables)
            copy_data = bool(args.data and not args.schema_only)
            if not copy_data:
                print("schema-only complete")
                return 0
            if args.truncate_target:
                for item in plan:
                    if item["source_exists"]:
                        _truncate_table(mssql_conn, item["table"])
                mssql_conn.commit()

            summary = []
            for item in plan:
                table = item["table"]
                if not item["source_exists"] or not item["columns"] or args.schema_only:
                    summary.append({**item, "target_rows": None, "written_rows": 0, "failed_rows": 0, "skipped_rows": item["source_rows"]})
                    continue
                written = 0
                failed = 0
                query = f"SELECT {', '.join(item['columns'])} FROM {table}"
                cursor = sqlite_conn.execute(query)
                for batch in _chunks(cursor, max(1, int(args.batch_size or 1000))):
                    try:
                        written += _insert_batch(mssql_conn, table, item["columns"], batch)
                        mssql_conn.commit()
                    except Exception:
                        mssql_conn.rollback()
                        failed += len(batch)
                target_rows = _count(mssql_conn, table, db_backend.BACKEND_MSSQL)
                summary.append({
                    **item,
                    "target_rows": target_rows,
                    "written_rows": written,
                    "failed_rows": failed,
                    "skipped_rows": max(0, item["source_rows"] - written - failed),
                })

            print("migration summary:")
            for item in summary:
                print(
                    f"- {item['table']}: source_rows={item['source_rows']} "
                    f"target_rows={item['target_rows']} written_rows={item['written_rows']} "
                    f"failed_rows={item['failed_rows']} skipped_rows={item['skipped_rows']}"
                )
            return 1 if any(item["failed_rows"] for item in summary) else 0
        finally:
            try:
                mssql_conn.close()
            except Exception:
                pass
    finally:
        sqlite_conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate local SQLite cache tables to SQL Server.")
    parser.add_argument("--sqlite", default="", help="Source SQLite path; opened read-only.")
    parser.add_argument("--sqlserver-conn", default="", help="SQL Server ODBC connection string.")
    parser.add_argument("--schema-only", action="store_true", help="Create SQL Server schema only.")
    parser.add_argument("--data", action="store_true", help="Copy source data after schema creation.")
    parser.add_argument("--tables", default="", help="Comma-separated table allowlist.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; this is the default.")
    parser.add_argument("--write", action="store_true", help="Actually write to SQL Server.")
    parser.add_argument("--batch-size", type=int, default=1000, help="Insert batch size.")
    parser.add_argument("--truncate-target", action="store_true", help="Delete target table data before copying.")
    parser.add_argument(
        "--confirm",
        action="append",
        default=[],
        help="Required confirmation token. Repeat for write plus truncate confirmations.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return migrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
