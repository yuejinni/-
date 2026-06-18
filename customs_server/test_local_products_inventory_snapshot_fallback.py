import json
import sqlite3

import server


ACCOUNT = "祺航饰品"


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
            unit_name TEXT,
            category_name TEXT,
            default_supplier_number TEXT,
            default_supplier_name TEXT,
            image_url TEXT,
            status TEXT,
            data_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE sales_product_quantities (
            account TEXT NOT NULL,
            code TEXT NOT NULL,
            stock_new REAL DEFAULT 0,
            stock_transit REAL DEFAULT 0,
            stock_local REAL DEFAULT 0,
            stock_factory REAL DEFAULT 0,
            factory_qty REAL DEFAULT 0,
            updated_at TEXT NOT NULL,
            error TEXT DEFAULT '',
            PRIMARY KEY (account, code)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE transfer_details (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            date TEXT,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE inventory_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            product_number TEXT,
            normalized_product_number TEXT,
            barcode TEXT,
            product_name TEXT,
            warehouse_name TEXT NOT NULL,
            quantity REAL NOT NULL DEFAULT 0,
            unit TEXT,
            snapshot_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'jdy_inventory',
            data_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return conn


def insert_product(conn, code="H.78669H-9", barcode="260114H036001"):
    conn.execute(
        """
        INSERT INTO jdy_products (
            account, product_id, product_number, product_name, spec, barcode,
            unit_name, category_name, default_supplier_number, default_supplier_name,
            image_url, status, data_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ACCOUNT,
            "pid",
            code,
            "10片盒装甲(处)",
            "54",
            barcode,
            "DZ",
            "Artificial Nails",
            "",
            "78669 天津穿戴甲",
            "",
            "",
            "{}",
        ),
    )


def insert_snapshot(conn, warehouse, qty, code="H.78669H-9", normalized="78669H-9", barcode="260114H036001", at="2026-06-12 10:00:00"):
    conn.execute(
        """
        INSERT INTO inventory_snapshots (
            account, product_number, normalized_product_number, barcode, product_name,
            warehouse_name, quantity, unit, snapshot_at, source, data_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'jdy_inventory', '{}')
        """,
        (ACCOUNT, code, normalized, barcode, "10片盒装甲(处)", warehouse, qty, "DZ", at),
    )


def insert_quantity(
    conn,
    code="H.78669H-9",
    stock_new=0,
    stock_transit=0,
    stock_local=0,
    stock_factory=0,
    factory_qty=0,
):
    conn.execute(
        """
        INSERT INTO sales_product_quantities (
            account, code, stock_new, stock_transit, stock_local, stock_factory,
            factory_qty, updated_at, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, '2026-06-12 10:00:00', '')
        """,
        (ACCOUNT, code, stock_new, stock_transit, stock_local, stock_factory, factory_qty),
    )


def insert_transfer(conn):
    order = {
        "number": "DB20260512001",
        "date": "2026-05-12",
        "checkStatus": "true",
        "entries": [
            {
                "productNumber": "H.78669H-9",
                "barCode": "260114H036001",
                "qty": 112,
                "unitName": "DZ",
                "outLocationName": "厂家订单",
                "inLocationName": "在途仓库",
            }
        ],
    }
    conn.execute(
        """
        INSERT INTO transfer_details (account, number, date, data_json, updated_at)
        VALUES (?, ?, ?, ?, '2026-06-12 10:00:00')
        """,
        (ACCOUNT, "DB20260512001", "2026-05-12", json.dumps(order, ensure_ascii=False)),
    )


def local_products_payload(conn, q="78669H-9"):
    original = server._sales_readonly_conn
    server._sales_readonly_conn = lambda: conn
    try:
        with server.app.test_request_context("/local-products", query_string={"q": q, "account": ACCOUNT}):
            payload, status = server._local_products_response()
    finally:
        server._sales_readonly_conn = original
    assert status == 200
    assert payload["called_jdy"] is False
    assert payload["local_only"] is True
    return payload


def test_local_products_does_not_fallback_without_quantity_row():
    conn = make_conn()
    insert_product(conn)
    insert_transfer(conn)
    insert_snapshot(conn, "厂家订单", 128)

    payload = local_products_payload(conn)
    item = payload["items"][0]

    assert item["stock_new"] == 0
    assert item["stock_transit"] == 0
    assert item["stock_local"] == 0
    assert item["stock_factory"] == 0
    assert item["factory_qty"] == 0
    assert item["warehouses"][1]["qty"] == 0
    assert item["warehouses"][3]["qty"] == 0
    assert "quantity_fallback" not in item
    conn.close()


def test_positive_quantity_cache_is_not_overwritten_by_snapshot_or_transfer():
    conn = make_conn()
    insert_product(conn)
    insert_quantity(conn, stock_new=11, stock_transit=22, stock_local=33, factory_qty=66, stock_factory=77)
    insert_transfer(conn)
    insert_snapshot(conn, "厂家订单", 128)

    payload = local_products_payload(conn)
    item = payload["items"][0]

    assert item["stock_new"] == 11
    assert item["stock_transit"] == 22
    assert item["stock_local"] == 33
    assert item["factory_qty"] == 66
    assert item["stock_factory"] == 77
    assert item["warehouses"][1]["qty"] == 22
    assert item["warehouses"][3]["qty"] == 66
    assert "quantity_fallback" not in item
    conn.close()


def test_snapshot_warehouse_name_variants_do_not_change_local_products():
    conn = make_conn()
    insert_product(conn)
    insert_snapshot(conn, "厂家订单", 10)
    insert_snapshot(conn, "工厂订单", 20)
    insert_snapshot(conn, "工厂", 30)
    insert_snapshot(conn, "在途仓库", 999)

    payload = local_products_payload(conn)
    item = payload["items"][0]

    assert item["stock_factory"] == 0
    assert item["warehouses"][3]["qty"] == 0
    assert "quantity_fallback" not in item
    conn.close()


def test_snapshot_normalized_code_and_barcode_do_not_change_local_products():
    conn = make_conn()
    insert_product(conn, code="H.78669H-9", barcode="260114H036001")
    insert_snapshot(conn, "厂家订单", 40, code="", normalized="78669H-9", barcode="")
    insert_snapshot(conn, "厂家订单", 88, code="OTHER", normalized="OTHER", barcode="260114H036001")

    payload = local_products_payload(conn)
    item = payload["items"][0]

    assert item["stock_factory"] == 0
    assert item["warehouses"][3]["qty"] == 0
    assert "quantity_fallback" not in item
    conn.close()


if __name__ == "__main__":
    test_local_products_does_not_fallback_without_quantity_row()
    test_positive_quantity_cache_is_not_overwritten_by_snapshot_or_transfer()
    test_snapshot_warehouse_name_variants_do_not_change_local_products()
    test_snapshot_normalized_code_and_barcode_do_not_change_local_products()
    print("local products inventory snapshot non-fallback tests passed")
