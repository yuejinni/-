import json
import os
import sqlite3
import tempfile

import server


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
    return conn


def insert_transfer(conn, account, number, date, check_status, entries):
    order = {
        "number": number,
        "date": date,
        "checkStatus": check_status,
        "entries": entries,
    }
    conn.execute(
        """
        INSERT INTO transfer_details (account, number, date, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (account, number, date, json.dumps(order, ensure_ascii=False), "2026-06-12 00:00:00"),
    )


def make_stock(factory=128, transit=112, local=5, new=0):
    stock = server._empty_sales_stock_buckets()
    keys = list(stock.keys())
    stock[keys[0]] = new
    stock[keys[1]] = transit
    stock[keys[2]] = local
    stock[keys[3]] = factory
    return stock


def test_checked_factory_to_transit_fallback_ignores_unchecked_deduction():
    conn = make_conn()
    insert_transfer(
        conn,
        "祺航饰品",
        "DB20260512001",
        "2026-05-12",
        "true",
        [
            {
                "productNumber": "H.78669H-9",
                "barCode": "260114H036001",
                "qty": 54,
                "unitName": "DZ",
                "outLocationName": "厂家订单",
                "inLocationName": "在途仓库",
            },
            {
                "productNumber": "H.78669H-9",
                "barCode": "260114H036001",
                "qty": 58,
                "unitName": "DZ",
                "outLocationName": "厂家订单",
                "inLocationName": "在途仓库",
            },
        ],
    )
    insert_transfer(
        conn,
        "祺航饰品",
        "DB20260512003",
        "2026-05-12",
        "false",
        [
            {
                "productNumber": "H.78669H-9",
                "barCode": "260114H036001",
                "qty": 112,
                "unitName": "DZ",
                "outLocationName": "在途仓库",
                "inLocationName": "新大仓库",
            }
        ],
    )

    fallback = server._local_products_transfer_transit_fallback(
        conn,
        [{"account": "祺航饰品", "product_number": "H.78669H-9", "barcode": "260114H036001"}],
    )

    assert fallback[("祺航饰品", "H.78669H-9")]["stock_transit"] == 112
    assert len(fallback[("祺航饰品", "H.78669H-9")]["sources"]) == 2
    conn.close()


def test_transfer_fallback_matches_without_prefix_and_by_barcode():
    conn = make_conn()
    insert_transfer(
        conn,
        "祺航饰品",
        "DB1",
        "2026-06-01",
        "已审核",
        [
            {
                "productNumber": "123",
                "qty": 20,
                "outLocationName": "工厂",
                "inLocationName": "在途仓库",
            },
            {
                "productNumber": "OTHER",
                "barCode": "BC-123",
                "qty": 5,
                "outLocationName": "在途仓库",
                "inLocationName": "新大仓库",
            },
        ],
    )

    fallback = server._local_products_transfer_transit_fallback(
        conn,
        [{"account": "祺航饰品", "product_number": "A.123", "barcode": "BC-123"}],
    )

    assert fallback[("祺航饰品", "A.123")]["stock_transit"] == 15
    conn.close()


def test_factory_logic_effective_date_starts_2026_05_29():
    original = server._sales_sync_config
    server._sales_sync_config = lambda: {
        "factory_purchase_begin_date": "2026-05-28",
        "factory_new_logic_begin_date": "2026-05-28",
        "factory_disabled_suppliers": "",
    }
    try:
        assert server._effective_sales_factory_new_logic_begin_date() == "2026-05-29"
        assert server._sales_order_uses_legacy_factory_logic("2025-10-31") is False
        assert server._sales_order_uses_legacy_factory_logic("2025-11-01") is True
        assert server._sales_order_uses_legacy_factory_logic("2026-05-28") is True
        assert server._sales_order_uses_legacy_factory_logic("2026-05-29") is False
        assert server._sales_order_uses_new_factory_logic("2026-05-28") is False
        assert server._sales_order_uses_new_factory_logic("2026-05-29") is True
        assert server._sales_factory_purchase_filter_config()["begin_date"] == "2026-05-29"
    finally:
        server._sales_sync_config = original


def test_legacy_factory_qty_on_2026_05_28_uses_factory_order_stock():
    assert server._factory_qty_from_purchase_strict(
        object(),
        "H.78669H-9",
        stock={"工厂订单": 128, "在途": 112, "金华/本地": 5},
        order_date="2026-05-28",
    ) == 128


def test_legacy_factory_qty_before_2025_11_01_is_zero():
    assert server._factory_qty_from_purchase_strict(
        object(),
        "H.78669H-9",
        stock=make_stock(factory=128, transit=112, local=5),
        order_date="2025-10-31",
    ) == 0


def test_legacy_factory_qty_on_2025_11_01_uses_factory_order_stock():
    assert server._factory_qty_from_purchase_strict(
        object(),
        "H.78669H-9",
        stock=make_stock(factory=128, transit=112, local=5),
        order_date="2025-11-01",
    ) == 128


def test_new_factory_qty_on_2026_05_29_uses_checked_purchase_orders():
    original_fetch = server._fetch_checked_factory_purchase_orders
    original_config = server._sales_sync_config
    server._fetch_checked_factory_purchase_orders = lambda cli, code, filter_conf: [
        {
            "number": "PO-1",
            "entries": [{"productNumber": "H.78669H-9", "qty": 100}],
        },
        {
            "number": "PO-2",
            "entries": [{"productNumber": "H.78669H-9", "mainQty": 28}],
        },
    ]
    server._sales_sync_config = lambda: {
        "factory_new_logic_begin_date": "2026-05-29",
        "factory_purchase_begin_date": "2026-05-29",
        "factory_disabled_suppliers": "",
    }
    try:
        assert server._factory_qty_from_purchase_strict(
            object(),
            "H.78669H-9",
            stock={"工厂订单": 999, "在途": 112, "金华/本地": 5},
            order_date="2026-05-29",
        ) == 128
    finally:
        server._fetch_checked_factory_purchase_orders = original_fetch
        server._sales_sync_config = original_config


def test_checked_purchase_fetch_requests_only_checked_orders():
    calls = []

    class Client:
        def get_purchase_order_requests(self, **kwargs):
            calls.append(kwargs)
            return {"list": [], "total": 0}

    server._fetch_checked_factory_purchase_orders(
        Client(),
        "H.78669H-9",
        {"begin_date": "2026-05-29", "disabled_suppliers": []},
    )

    assert calls
    assert calls[0]["check_status"] == 1
    assert calls[0]["begin_date"] == "2026-05-29"


def test_checked_purchase_factory_qty_counts_linked_orders_without_stock_deduction():
    original = server._fetch_checked_factory_purchase_orders
    server._fetch_checked_factory_purchase_orders = lambda cli, code, filter_conf: [
        {
            "number": "PO-1",
            "entries": [
                {"productNumber": "H.78669H-9", "qty": 100},
                {"productNumber": "OTHER", "qty": 999},
            ],
        },
        {
            "number": "PO-2",
            "sourceBillNo": "GH20260601001",
            "entries": [{"productNumber": "H.78669H-9", "qty": 200}],
        },
    ]
    try:
        assert server._factory_qty_from_checked_purchase_strict(
            object(),
            "H.78669H-9",
            stock={"在途": 90, "金华/本地": 10},
        ) == 300
    finally:
        server._fetch_checked_factory_purchase_orders = original


def make_quantity_cache_path():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    conn = sqlite3.connect(path)
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
        CREATE TABLE jdy_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT,
            product_number TEXT,
            product_name TEXT,
            barcode TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE purchase_inbounds (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            date TEXT,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
        """
    )
    conn.commit()
    conn.close()
    return path


def insert_purchase_inbound(path, account, number, date, code):
    conn = sqlite3.connect(path)
    payload = {
        "number": number,
        "date": date,
        "entries": [{"productNumber": code, "qty": 1}],
    }
    conn.execute(
        """
        INSERT INTO purchase_inbounds (account, number, date, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (account, number, date, json.dumps(payload, ensure_ascii=False), "2026-06-18 00:00:00"),
    )
    conn.commit()
    conn.close()


def quantity_cache_conn_factory(path):
    class ClosingConnection:
        def __enter__(self):
            self.conn = sqlite3.connect(path)
            self.conn.row_factory = sqlite3.Row
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            self.conn.close()
            return False

    def _open():
        return ClosingConnection()
    return _open


class QuantitySyncClient:
    def __init__(self, purchase_rows=None):
        self.purchase_rows = purchase_rows or []
        self.purchase_calls = []

    def get_inventory_by_product(self, code, page_size=80):
        return {
            "list": [
                {"productNumber": code, "locationName": "新大仓库", "qty": 11},
                {"productNumber": code, "locationName": "在途仓库", "qty": 22},
                {"productNumber": code, "locationName": "金华/本地", "qty": 33},
                {"productNumber": code, "locationName": "工厂订单", "qty": 128},
            ]
        }

    def get_purchase_order_requests(self, **kwargs):
        self.purchase_calls.append(kwargs)
        rows = list(self.purchase_rows)
        begin_date = str(kwargs.get("begin_date") or "")[:10]
        if begin_date:
            rows = [
                row for row in rows
                if str(row.get("date") or row.get("billDate") or row.get("createTime") or "")[:10] >= begin_date
            ]
        if kwargs.get("check_status") == 1:
            rows = [
                row for row in rows
                if str(row.get("checkStatus", True)).lower() not in ("false", "0", "unchecked", "未审核")
            ]
        return {"list": rows, "total": len(rows)}


def test_fetch_sales_quantity_map_writes_legacy_factory_qty_from_factory_order_stock():
    path = make_quantity_cache_path()
    original_conn = server._sales_cache_conn
    server._sales_cache_conn = quantity_cache_conn_factory(path)
    try:
        result, errors = server._fetch_sales_quantity_map(
            QuantitySyncClient(),
            "绁鸿埅楗板搧",
            ["H.78669H-9"],
            order_date="2026-05-28",
        )
        assert errors == []
        assert result["H.78669H-9"]["factory_qty"] == 128
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sales_product_quantities WHERE code=?", ("H.78669H-9",)).fetchone()
        conn.close()
        assert row["stock_new"] == 11
        assert row["stock_transit"] == 22
        assert row["stock_local"] == 33
        assert row["stock_factory"] == 128
        assert row["factory_qty"] == 128
    finally:
        server._sales_cache_conn = original_conn
        os.remove(path)


def test_fetch_sales_quantity_map_writes_new_factory_qty_from_checked_purchase_orders():
    path = make_quantity_cache_path()
    original_conn = server._sales_cache_conn
    original_config = server._sales_sync_config
    server._sales_cache_conn = quantity_cache_conn_factory(path)
    server._sales_sync_config = lambda: {
        "factory_new_logic_begin_date": "2026-05-29",
        "factory_purchase_begin_date": "2026-05-29",
        "factory_disabled_suppliers": "",
    }
    client = QuantitySyncClient([
        {
            "number": "PO-1",
            "date": "2026-05-29",
            "entries": [{"productNumber": "H.78669H-9", "qty": 100}],
        },
        {
            "number": "PO-2",
            "date": "2026-05-30",
            "sourceBillNo": "GH20260601001",
            "entries": [{"productNumber": "H.78669H-9", "qty": 200}],
        },
    ])
    try:
        result, errors = server._fetch_sales_quantity_map(
            client,
            "绁鸿埅楗板搧",
            ["H.78669H-9"],
            order_date="2026-05-29",
        )
        assert errors == []
        assert result["H.78669H-9"]["factory_qty"] == 300
        assert client.purchase_calls[0]["check_status"] == 1
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sales_product_quantities WHERE code=?", ("H.78669H-9",)).fetchone()
        conn.close()
        assert row["stock_factory"] == 128
        assert row["factory_qty"] == 300
    finally:
        server._sales_cache_conn = original_conn
        server._sales_sync_config = original_config
        os.remove(path)


def test_daily_catalog_quantity_refresh_covers_product_without_sales_order():
    path = make_quantity_cache_path()
    original_conn = server._sales_cache_conn
    original_sources = server._sales_sources_for_account
    original_client = server._client_for_sync_account
    client = QuantitySyncClient()
    server._sales_cache_conn = quantity_cache_conn_factory(path)
    server._sales_sources_for_account = lambda account='all': [(lambda: client, "A")]
    server._client_for_sync_account = lambda account_name, cli_fn: client
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO jdy_products (account, product_number, product_name, barcode) VALUES (?, ?, ?, ?)",
        ("A", "NO-SALES-1", "No sales product", "BC1"),
    )
    conn.commit()
    conn.close()
    insert_purchase_inbound(path, "A", "GH20251101001", "2025-11-01", "NO-SALES-1")
    try:
        summary = server._refresh_all_local_product_quantities(account="all", batch_size=10)
        assert summary["total_codes"] == 1
        assert summary["refreshed_codes"] == 1
        assert summary["errors"] == []
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sales_product_quantities WHERE account=? AND code=?", ("A", "NO-SALES-1")).fetchone()
        conn.close()
        assert row["stock_new"] == 11
        assert row["stock_transit"] == 22
        assert row["stock_local"] == 33
        assert row["stock_factory"] == 128
        assert row["factory_qty"] == 128
    finally:
        server._sales_cache_conn = original_conn
        server._sales_sources_for_account = original_sources
        server._client_for_sync_account = original_client
        os.remove(path)


def test_daily_catalog_quantity_refresh_preserves_factory_qty_when_legacy_source_unavailable():
    path = make_quantity_cache_path()
    original_conn = server._sales_cache_conn
    original_sources = server._sales_sources_for_account
    original_client = server._client_for_sync_account
    client = QuantitySyncClient()
    server._sales_cache_conn = quantity_cache_conn_factory(path)
    server._sales_sources_for_account = lambda account='all': [(lambda: client, "A")]
    server._client_for_sync_account = lambda account_name, cli_fn: client
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO jdy_products (account, product_number, product_name, barcode) VALUES (?, ?, ?, ?)",
        ("A", "NO-SOURCE-1", "No source product", "BC0"),
    )
    conn.execute(
        """
        INSERT INTO sales_product_quantities
        (account, code, stock_new, stock_transit, stock_local, stock_factory, factory_qty, updated_at, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("A", "NO-SOURCE-1", 1, 2, 3, 4, 456, "2026-06-17 00:00:00", ""),
    )
    conn.commit()
    conn.close()
    insert_purchase_inbound(path, "A", "GH20251031001", "2025-10-31", "NO-SOURCE-1")
    try:
        summary = server._refresh_all_local_product_quantities(account="all", batch_size=10)
        assert summary["total_codes"] == 1
        assert summary["refreshed_codes"] == 1
        assert summary["errors"] == []
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sales_product_quantities WHERE account=? AND code=?", ("A", "NO-SOURCE-1")).fetchone()
        conn.close()
        assert row["stock_factory"] == 128
        assert row["factory_qty"] == 456
    finally:
        server._sales_cache_conn = original_conn
        server._sales_sources_for_account = original_sources
        server._client_for_sync_account = original_client
        os.remove(path)


def test_daily_catalog_quantity_refresh_zeroes_factory_qty_when_legacy_source_available_and_not_matched():
    path = make_quantity_cache_path()
    original_conn = server._sales_cache_conn
    original_sources = server._sales_sources_for_account
    original_client = server._client_for_sync_account
    client = QuantitySyncClient()
    server._sales_cache_conn = quantity_cache_conn_factory(path)
    server._sales_sources_for_account = lambda account='all': [(lambda: client, "A")]
    server._client_for_sync_account = lambda account_name, cli_fn: client
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO jdy_products (account, product_number, product_name, barcode) VALUES (?, ?, ?, ?)",
        ("A", "NO-MATCH-1", "No matched product", "BC9"),
    )
    conn.execute(
        """
        INSERT INTO sales_product_quantities
        (account, code, stock_new, stock_transit, stock_local, stock_factory, factory_qty, updated_at, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("A", "NO-MATCH-1", 1, 2, 3, 4, 456, "2026-06-17 00:00:00", ""),
    )
    conn.commit()
    conn.close()
    insert_purchase_inbound(path, "A", "GH20251101001", "2025-11-01", "OTHER-1")
    try:
        summary = server._refresh_all_local_product_quantities(account="all", batch_size=10)
        assert summary["total_codes"] == 1
        assert summary["refreshed_codes"] == 1
        assert summary["errors"] == []
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sales_product_quantities WHERE account=? AND code=?", ("A", "NO-MATCH-1")).fetchone()
        conn.close()
        assert row["stock_factory"] == 128
        assert row["factory_qty"] == 0
    finally:
        server._sales_cache_conn = original_conn
        server._sales_sources_for_account = original_sources
        server._client_for_sync_account = original_client
        os.remove(path)


def test_daily_catalog_quantity_refresh_counts_checked_purchase_orders_only():
    path = make_quantity_cache_path()
    original_conn = server._sales_cache_conn
    original_sources = server._sales_sources_for_account
    original_client = server._client_for_sync_account
    original_config = server._sales_sync_config
    client = QuantitySyncClient([
        {
            "number": "PO-BEFORE-BEGIN",
            "date": "2026-05-28",
            "checkStatus": True,
            "entries": [{"productNumber": "NEW-1", "qty": 500}],
        },
        {
            "number": "PO-CHECKED",
            "date": "2026-05-29",
            "checkStatus": True,
            "entries": [{"productNumber": "NEW-1", "qty": 100}],
        },
        {
            "number": "PO-LINKED",
            "date": "2026-05-30",
            "checkStatus": True,
            "sourceBillNo": "GH20260601001",
            "entries": [{"productNumber": "NEW-1", "qty": 200}],
        },
        {
            "number": "PO-UNCHECKED",
            "date": "2026-05-30",
            "checkStatus": False,
            "entries": [{"productNumber": "NEW-1", "qty": 999}],
        },
    ])
    server._sales_cache_conn = quantity_cache_conn_factory(path)
    server._sales_sources_for_account = lambda account='all': [(lambda: client, "A")]
    server._client_for_sync_account = lambda account_name, cli_fn: client
    server._sales_sync_config = lambda: {
        "factory_new_logic_begin_date": "2026-05-29",
        "factory_purchase_begin_date": "2026-05-29",
        "factory_disabled_suppliers": "",
    }
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO jdy_products (account, product_number, product_name, barcode) VALUES (?, ?, ?, ?)",
        ("A", "NEW-1", "New purchase product", "BC2"),
    )
    conn.commit()
    conn.close()
    try:
        summary = server._refresh_all_local_product_quantities(account="all", batch_size=10)
        assert summary["total_codes"] == 1
        assert summary["refreshed_codes"] == 1
        assert summary["errors"] == []
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sales_product_quantities WHERE account=? AND code=?", ("A", "NEW-1")).fetchone()
        conn.close()
        assert row["factory_qty"] == 300
        assert any(call.get("check_status") == 1 for call in client.purchase_calls)
    finally:
        server._sales_cache_conn = original_conn
        server._sales_sources_for_account = original_sources
        server._client_for_sync_account = original_client
        server._sales_sync_config = original_config
        os.remove(path)


if __name__ == "__main__":
    test_checked_factory_to_transit_fallback_ignores_unchecked_deduction()
    test_transfer_fallback_matches_without_prefix_and_by_barcode()
    test_factory_logic_effective_date_starts_2026_05_29()
    test_legacy_factory_qty_before_2025_11_01_is_zero()
    test_legacy_factory_qty_on_2025_11_01_uses_factory_order_stock()
    test_legacy_factory_qty_on_2026_05_28_uses_factory_order_stock()
    test_new_factory_qty_on_2026_05_29_uses_checked_purchase_orders()
    test_checked_purchase_fetch_requests_only_checked_orders()
    test_checked_purchase_factory_qty_counts_linked_orders_without_stock_deduction()
    test_fetch_sales_quantity_map_writes_legacy_factory_qty_from_factory_order_stock()
    test_fetch_sales_quantity_map_writes_new_factory_qty_from_checked_purchase_orders()
    test_daily_catalog_quantity_refresh_covers_product_without_sales_order()
    test_daily_catalog_quantity_refresh_preserves_factory_qty_when_legacy_source_unavailable()
    test_daily_catalog_quantity_refresh_zeroes_factory_qty_when_legacy_source_available_and_not_matched()
    test_daily_catalog_quantity_refresh_counts_checked_purchase_orders_only()
    print("local products transfer fallback tests passed")
