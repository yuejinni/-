import json
import sqlite3

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
        assert server._sales_order_uses_new_factory_logic("2026-05-28") is False
        assert server._sales_order_uses_new_factory_logic("2026-05-29") is True
        assert server._sales_factory_purchase_filter_config()["begin_date"] == "2026-05-29"
    finally:
        server._sales_sync_config = original


if __name__ == "__main__":
    test_checked_factory_to_transit_fallback_ignores_unchecked_deduction()
    test_transfer_fallback_matches_without_prefix_and_by_barcode()
    test_factory_logic_effective_date_starts_2026_05_29()
    print("local products transfer fallback tests passed")
