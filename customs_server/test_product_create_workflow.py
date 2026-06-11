import json
import os
import sqlite3
import tempfile
from pathlib import Path

import product_create_workflow as workflow


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE jdy_suppliers (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            name TEXT,
            category_text TEXT,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE jdy_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT,
            product_id TEXT,
            product_number TEXT,
            product_name TEXT,
            spec TEXT,
            barcode TEXT,
            category_id TEXT,
            category_name TEXT,
            unit_id TEXT,
            unit_name TEXT,
            default_supplier_id TEXT,
            default_supplier_number TEXT,
            default_supplier_name TEXT,
            image_url TEXT,
            status TEXT,
            data_json TEXT,
            created_at TEXT,
            updated_at TEXT,
            last_seen_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE customs_product_master (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_code TEXT UNIQUE,
            account TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO jdy_suppliers (account, number, name, category_text, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "account1-name",
            "SUP-001",
            "Supplier One",
            "",
            json.dumps({"id": "sup-id-1", "taxPayerNo": "102629"}),
            "2026-06-11 00:00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO jdy_products
        (account, product_number, product_name, unit_id, unit_name, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("account1-name", "EXISTING", "Existing", "unit-pac", "PAC", "{}", "2026-06-11 00:00:00"),
    )
    workflow.ensure_schema(conn)
    return conn


def make_base_dir():
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    (base / "category_code_map.json").write_text(
        json.dumps(
            {
                "account1": {
                    "cat-1": {
                        "path": "Accessories/Test",
                        "prefix": "C",
                        "small_code": "02",
                        "code_2d": "11",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return tmp, str(base)


def sample_items():
    return [
        {
            "product_name": "Test Product",
            "spec": "gold",
            "category_id": "cat-1",
            "supplier_number": "SUP-001",
            "purchase_price": 3.25,
            "purchase_qty": 12,
            "unit_name": "PAC",
            "customs": {
                "customs_name_cn": "test cn",
                "hs_code": "7117190000",
                "material": "alloy",
                "usage": "decoration",
            },
        }
    ]


def test_preview_is_local_only_and_does_not_create_draft_rows():
    tmp, base = make_base_dir()
    try:
        conn = make_conn()
        result = workflow.preview_payload(conn, base, "account1", "account1-name", sample_items())
        assert result["success"] is True
        assert result["called_jdy"] is False
        assert result["would_call_jdy"] is False
        assert result["ready"] is True
        item = result["items"][0]
        assert item["product_number"] == "C.A1262902-0001"
        assert item["ean13"].isdigit() and len(item["ean13"]) == 13
        assert item["payloads"]["product_add"]["productNumber"] == item["product_number"]
        count = conn.execute("SELECT COUNT(*) AS c FROM product_create_draft").fetchone()["c"]
        assert count == 0
    finally:
        tmp.cleanup()


def test_draft_dry_run_and_mock_submit_never_call_real_jdy():
    tmp, base = make_base_dir()
    try:
        conn = make_conn()
        draft = workflow.create_draft(
            conn,
            base,
            "account1",
            "account1-name",
            "Unit Test Draft",
            sample_items(),
            {"username": "tester", "role": "admin"},
        )
        assert draft["status"] == "validated"
        dry = workflow.dry_run_draft(conn, draft["id"])
        assert dry["success"] is True
        assert dry["called_jdy"] is False
        assert dry["would_call_jdy"] is True
        assert dry["items"][0]["would_write_jdy"]["product_add"] is True

        result = workflow.submit_draft(
            conn,
            draft["id"],
            {"username": "admin", "role": "admin"},
            confirm_code=workflow.CONFIRM_SUBMIT_CODE,
            dry_run_confirmed=True,
            mock=True,
        )
        assert result["success"] is True
        assert result["called_jdy"] is False
        assert result["mock"] is True
        submitted = workflow.read_draft(conn, draft["id"])
        assert submitted["status"] == "submitted"
        assert submitted["sync_status"] == "needs_jdy_products_sync"
        log_rows = conn.execute("SELECT action, called_jdy FROM product_create_submit_log").fetchall()
        assert {row["action"] for row in log_rows} >= {"product_add", "product_price_update", "purchase_order_add"}
        assert all(row["called_jdy"] == 0 for row in log_rows)
    finally:
        tmp.cleanup()


def test_real_submit_requires_second_confirm_token():
    tmp, base = make_base_dir()
    try:
        conn = make_conn()
        draft = workflow.create_draft(
            conn,
            base,
            "account1",
            "account1-name",
            "Unit Test Draft",
            sample_items(),
            {"username": "tester", "role": "admin"},
        )
        result = workflow.submit_draft(
            conn,
            draft["id"],
            {"username": "admin", "role": "admin"},
            confirm_code=workflow.CONFIRM_SUBMIT_CODE,
            dry_run_confirmed=True,
            mock=False,
            submitter=workflow.MockJdySubmitter(),
            confirm_real_jdy="",
        )
        assert result["success"] is False
        assert "REAL_JDY_PRODUCT_CREATE" in result["error"]
    finally:
        tmp.cleanup()


def test_generate_local_first_guard_strings_present():
    server_path = Path(__file__).with_name("server.py")
    text = server_path.read_text(encoding="utf-8", errors="ignore")
    generate_start = text.index("@app.route('/generate'")
    generate_end = text.index("@app.route('/ai-config'", generate_start)
    block = text[generate_start:generate_end]
    assert "_customs_generation_local_context" in block
    assert "customs_base_map" in block
    assert "called_jdy': False" in block or '"called_jdy": False' in block
    assert "generate_all(data)" in block
    assert "write_confirmed_license" not in block


def run_all():
    tests = [
        test_preview_is_local_only_and_does_not_create_draft_rows,
        test_draft_dry_run_and_mock_submit_never_call_real_jdy,
        test_real_submit_requires_second_confirm_token,
        test_generate_local_first_guard_strings_present,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} product-create local-first tests passed")


if __name__ == "__main__":
    run_all()

