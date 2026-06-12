import contextlib
import io
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import migrate_sqlite_to_sqlserver as migrate


def make_sqlite_db() -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "cache.sqlite3"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE sales_orders (
                account TEXT NOT NULL,
                number TEXT NOT NULL,
                date TEXT,
                customer_name TEXT,
                total_qty REAL DEFAULT 0,
                total_amount REAL DEFAULT 0,
                check_status TEXT,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (account, number)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sales_orders
            (account, number, date, customer_name, total_qty, total_amount, check_status, data_json, updated_at)
            VALUES ('a1', 'SO-1', '2026-06-12', 'Customer', 1, 2, 'ok', '{}', '2026-06-12 00:00:00')
            """
        )
        conn.commit()
    finally:
        conn.close()
    return tmp


def run_capture(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            code = migrate.main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0) if isinstance(exc.code, int) else 1
    return code, buf.getvalue()


def test_dry_run_does_not_open_sqlserver():
    tmp = make_sqlite_db()
    try:
        path = str(Path(tmp.name) / "cache.sqlite3")
        code, out = run_capture(["--sqlite", path, "--tables", "sales_orders"])
        assert code == 0
        assert "dry-run only" in out
        assert "source_rows=1" in out
    finally:
        tmp.cleanup()


def test_write_requires_confirm_token():
    tmp = make_sqlite_db()
    try:
        path = str(Path(tmp.name) / "cache.sqlite3")
        code, out = run_capture(["--sqlite", path, "--tables", "sales_orders", "--write"])
        assert code == 1
        assert out == ""
    finally:
        tmp.cleanup()


def test_truncate_requires_separate_confirm_token():
    tmp = make_sqlite_db()
    try:
        path = str(Path(tmp.name) / "cache.sqlite3")
        code, out = run_capture([
            "--sqlite",
            path,
            "--tables",
            "sales_orders",
            "--write",
            "--truncate-target",
            "--confirm",
            migrate.WRITE_CONFIRM,
        ])
        assert code == 1
        assert out == ""
    finally:
        tmp.cleanup()


def test_confirm_tokens_accept_repeated_values():
    tokens = migrate._confirm_tokens([migrate.WRITE_CONFIRM, migrate.TRUNCATE_CONFIRM])
    assert migrate.WRITE_CONFIRM in tokens
    assert migrate.TRUNCATE_CONFIRM in tokens


def test_table_allowlist_rejects_unknown_table():
    tmp = make_sqlite_db()
    try:
        path = str(Path(tmp.name) / "cache.sqlite3")
        code, out = run_capture(["--sqlite", path, "--tables", "sales_orders,bad_table"])
        assert code == 1
        assert out == ""
    finally:
        tmp.cleanup()


def run_all():
    tests = [
        test_dry_run_does_not_open_sqlserver,
        test_write_requires_confirm_token,
        test_truncate_requires_separate_confirm_token,
        test_confirm_tokens_accept_repeated_values,
        test_table_allowlist_rejects_unknown_table,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} migration tool tests passed")


if __name__ == "__main__":
    run_all()
