import json
import sqlite3

import server


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
        CREATE TABLE sales_details (
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
        CREATE TABLE sales_order_requests (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            internal_id TEXT,
            date TEXT,
            customer_name TEXT,
            total_qty REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            bill_status TEXT,
            bill_status_name TEXT,
            check_status TEXT,
            source TEXT,
            sync_status TEXT,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE sales_order_request_details (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            internal_id TEXT,
            date TEXT,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
        """
    )
    return conn


def test_sales_order_webhook_writes_separate_request_cache(monkeypatch):
    conn = make_conn()
    monkeypatch.setattr(server, "_sales_cache_conn", lambda: conn)
    monkeypatch.setattr(
        server,
        "_refresh_sales_order_request_from_jdy",
        lambda account, query, mode="number", endpoint="": {
            "success": False,
            "error": "endpoint disabled in unit test",
        },
    )
    payload = {
        "accountId": "7958910093110",
        "bizType": "sal_bill_order",
        "operation": "update",
        "msgId": "msg-1",
        "data": {"id": 7958910390071, "billstatus": True},
    }

    result = server._cache_sales_order_request_from_webhook(
        "Accessories",
        "7958910390071",
        payload,
    )

    assert result["ok"] is True
    assert result["reason"] == "sales_order_request_webhook_cached"
    req = conn.execute("SELECT * FROM sales_order_requests").fetchone()
    detail = conn.execute("SELECT * FROM sales_order_request_details").fetchone()
    assert req["account"] == "Accessories"
    assert req["number"] == "internal:7958910390071"
    assert req["internal_id"] == "7958910390071"
    assert req["sync_status"] == "webhook_placeholder"
    assert detail["number"] == "internal:7958910390071"
    data = json.loads(req["data_json"])
    assert data["webhookBizType"] == "sal_bill_order"
    assert data["webhookAction"] == "update"
    assert conn.execute("SELECT COUNT(*) FROM sales_orders").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sales_details").fetchone()[0] == 0


def test_process_sales_order_webhook_does_not_touch_sales_cache_or_jdy(monkeypatch):
    conn = make_conn()
    monkeypatch.setattr(server, "_sales_cache_conn", lambda: conn)
    monkeypatch.setattr(
        server,
        "_refresh_sales_order_request_from_jdy",
        lambda account, query, mode="number", endpoint="": {
            "success": True,
            "written": 1,
        },
    )
    payload = {
        "accountId": "7958910093110",
        "bizType": "sal_bill_order",
        "operation": "update",
        "data": {"id": 7958910390072, "billstatus": True},
    }
    event = {
        "account": "Accessories",
        "resource": "sales_order",
        "bill_no": "7958910390072",
        "payload_json": json.dumps(payload, ensure_ascii=False),
    }

    result = server._process_webhook_event(event)

    assert result["ok"] is True
    assert result["reason"] == "sales_order_request_webhook_refreshed"
    assert conn.execute("SELECT COUNT(*) FROM sales_order_requests").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sales_order_request_details").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sales_orders").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sales_details").fetchone()[0] == 0


def test_sales_order_request_preview_requires_configured_endpoint(monkeypatch):
    monkeypatch.setattr(server, "_sales_order_request_list_path", lambda: "")
    monkeypatch.setattr(
        server,
        "_sales_client_for_account",
        lambda account: (_raise("missing endpoint must not call JDY"), account),
    )

    result = server._sales_order_request_fetch_preview("Accessories", "7958910390073", mode="internal_id")

    assert result["success"] is False
    assert result["called_jdy"] is False
    assert "endpoint" in result["error"]


def test_sales_order_request_refresh_replaces_internal_placeholder(monkeypatch):
    conn = make_conn()
    monkeypatch.setattr(server, "_sales_cache_conn", lambda: conn)
    monkeypatch.setattr(server, "_sales_order_request_list_path", lambda: "/jdyscm/saleOrder/list")

    placeholder = {
        "account": "Accessories",
        "number": "internal:7958910390074",
        "internalId": "7958910390074",
        "date": "",
        "customerName": "",
        "totalQty": 0,
        "totalAmount": 0,
        "billStatus": "",
        "billStatusName": "",
        "checkStatus": "",
        "source": "webhook",
        "syncStatus": "webhook_placeholder",
        "entries": [],
    }
    server._cache_upsert_sales_order_request(conn, placeholder)
    server._cache_upsert_sales_order_request_detail(conn, placeholder)
    conn.commit()

    class FakeClient:
        def get_sales_order_requests(self, list_path, page=1, page_size=100, filters=None):
            assert list_path == "/jdyscm/saleOrder/list"
            return {
                "list": [{
                    "id": 7958910390074,
                    "number": "XSDD20260616001",
                    "date": "2026-06-16",
                    "customerName": "Test Customer",
                    "entries": [{"productNumber": "P001", "productName": "Item", "qty": 2}],
                    "totalQty": 2,
                    "totalAmount": 30,
                }],
                "total": 1,
                "filter": filters or {},
            }

    monkeypatch.setattr(server, "_sales_client_for_account", lambda account: (FakeClient(), "Accessories"))

    result = server._refresh_sales_order_request_from_jdy(
        "Accessories",
        "7958910390074",
        mode="internal_id",
    )

    assert result["success"] is True
    assert result["called_jdy"] is True
    assert result["written"] == 1
    rows = conn.execute("SELECT number, internal_id, data_json FROM sales_order_requests ORDER BY number").fetchall()
    assert [r["number"] for r in rows] == ["XSDD20260616001"]
    data = json.loads(rows[0]["data_json"])
    assert data["syncStatus"] == "synced"
    assert data["entries"][0]["code"] == "P001"
    assert conn.execute("SELECT COUNT(*) FROM sales_orders").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sales_details").fetchone()[0] == 0


def _raise(message):
    raise AssertionError(message)


if __name__ == "__main__":
    class MonkeyPatch:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    test_sales_order_webhook_writes_separate_request_cache(MonkeyPatch())
    test_process_sales_order_webhook_does_not_touch_sales_cache_or_jdy(MonkeyPatch())
    test_sales_order_request_preview_requires_configured_endpoint(MonkeyPatch())
    test_sales_order_request_refresh_replaces_internal_placeholder(MonkeyPatch())
    print("sales order request cache tests passed")
