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
        "_sales_client_for_account",
        lambda account: (_raise("sales order request must not call JDY"), account),
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
    assert result["reason"] == "sales_order_request_webhook_cached"
    assert conn.execute("SELECT COUNT(*) FROM sales_order_requests").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sales_order_request_details").fetchone()[0] == 1
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
    print("sales order request cache tests passed")
