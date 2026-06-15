import os
import sys

ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db_backend


def _read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8", errors="ignore") as f:
        return f.read()


def test_default_backend_is_sqlite(monkeypatch=None):
    old = os.environ.pop(db_backend.ENV_BACKEND, None)
    try:
        assert db_backend.current_backend() == db_backend.BACKEND_SQLITE
        assert db_backend.is_mssql_enabled() is False
    finally:
        if old is not None:
            os.environ[db_backend.ENV_BACKEND] = old


def test_mssql_schema_generation_contains_required_tables():
    sql = "\n".join(db_backend.generate_mssql_schema_sql())
    for table in [
        "jdy_products",
        "jdy_suppliers",
        "sales_orders",
        "sales_details",
        "sales_product_quantities",
        "transfer_orders",
        "transfer_details",
        "inventory_snapshots",
        "product_create_draft",
        "product_create_draft_item",
        "product_create_sequences",
        "product_create_submit_log",
        "customs_product_master",
        "customs_product_master_history",
        "webhook_events",
    ]:
        assert f"[{table}]" in sql
    assert "IDENTITY(1,1)" in sql
    assert "NVARCHAR(MAX)" in sql


def test_sqlite_limit_translation_for_mssql():
    sql, params = db_backend.translate_sqlite_to_mssql(
        "SELECT * FROM jdy_products WHERE account = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        ["a1", 20, 40],
    )
    assert "OFFSET 40 ROWS FETCH NEXT 20 ROWS ONLY" in sql
    assert params == ["a1"]

    sql2, params2 = db_backend.translate_sqlite_to_mssql(
        "SELECT * FROM sales_orders WHERE account = ? LIMIT ?",
        ["a1", 10],
    )
    assert "TOP (10)" in sql2
    assert params2 == ["a1"]


def test_group_concat_translation_for_mssql():
    sql, params = db_backend.translate_sqlite_to_mssql(
        "SELECT resource, COUNT(*) AS c, GROUP_CONCAT(id) AS ids, GROUP_CONCAT(DISTINCT account) AS accounts FROM webhook_events GROUP BY resource HAVING c > 1",
        [],
    )
    assert "STRING_AGG(CAST(id AS nvarchar(max)), ',') AS ids" in sql
    assert "STRING_AGG(CAST(account AS nvarchar(max)), ',') AS accounts" in sql
    assert "HAVING COUNT(*) > 1" in sql
    assert params == []

    try:
        db_backend.translate_sqlite_to_mssql("SELECT last_insert_rowid() AS id", [])
    except ValueError as exc:
        assert "OUTPUT INSERTED.id" in str(exc)
    else:
        raise AssertionError("last_insert_rowid should be rejected in SQL Server mode")


def test_mssql_upsert_sql_is_parameterized():
    sql = db_backend.build_mssql_upsert_sql(
        "sales_orders",
        ["account", "number", "date", "data_json", "updated_at"],
        ["account", "number"],
    )
    assert "?" in sql["exists"]
    assert "?" in sql["update"]
    assert "?" in sql["insert"]
    assert "sales-001" not in "\n".join(sql.values())
    assert "[account] = ?" in sql["exists"]
    assert "[data_json] = ?" in sql["update"]


def test_static_mssql_readonly_and_identity_guards():
    server_text = _read("server.py")
    readonly_start = server_text.index("def _sales_readonly_conn():")
    readonly_end = server_text.index("CUSTOMS_PRODUCT_COLUMNS", readonly_start)
    readonly_block = server_text[readonly_start:readonly_end]
    mssql_start = readonly_block.index("if db_backend.is_mssql_enabled():")
    mssql_block = readonly_block[mssql_start:readonly_block.index("if not os.path.exists", mssql_start)]
    assert "ensure_mssql_cache_schema" not in mssql_block

    workflow_text = _read("product_create_workflow.py")
    assert "OUTPUT INSERTED.id AS id" in workflow_text
    assert "SCOPE_IDENTITY" not in workflow_text
    assert "OUTPUT INSERTED.id AS id" in server_text


def run_all():
    tests = [
        test_default_backend_is_sqlite,
        test_mssql_schema_generation_contains_required_tables,
        test_sqlite_limit_translation_for_mssql,
        test_group_concat_translation_for_mssql,
        test_mssql_upsert_sql_is_parameterized,
        test_static_mssql_readonly_and_identity_guards,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} SQL Server backend tests passed")


if __name__ == "__main__":
    run_all()
