#!/usr/bin/env python3
"""
sync_from_cloud.py
从云端 (gongdashuai.top:5008) 拉取销售订单数据，增量同步到本地 SQLite。

用法：
    python tools/sync_from_cloud.py
    python tools/sync_from_cloud.py --from-date 2026-04-01  # 只同步指定日期之后
    python tools/sync_from_cloud.py --dry-run               # 只统计不写入
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ─── 配置 ────────────────────────────────────────────────────────────────────
CLOUD_BASE = "http://gongdashuai.top:5008"
CLOUD_USER = "admin"
CLOUD_PASS = "qaz123456"

LOCAL_DB = Path(__file__).parent.parent / "_sales_cache" / "sales_cache.sqlite3"

RETRY_DELAY = 2   # 失败后等待秒数
MAX_RETRIES = 3
REQUEST_DELAY = 0.05  # 每次请求间隔（秒），避免压垮云端


# ─── 登录 ─────────────────────────────────────────────────────────────────────
def cloud_login() -> requests.Session:
    sess = requests.Session()
    r = sess.post(
        f"{CLOUD_BASE}/login",
        data={"username": CLOUD_USER, "password": CLOUD_PASS, "remember": "1"},
        timeout=15,
        allow_redirects=True,
    )
    # 验证登录成功（能访问需要鉴权的接口）
    r2 = sess.get(f"{CLOUD_BASE}/sales-cache/status", timeout=15)
    data = r2.json()
    if not data.get("success"):
        raise RuntimeError(f"登录失败或鉴权无效: {data}")
    print(f"✅ 登录成功，云端数据库: {data['stats']['orders_count']} 条订单")
    return sess, data["stats"]


# ─── 本地 DB ──────────────────────────────────────────────────────────────────
def local_conn():
    conn = sqlite3.connect(str(LOCAL_DB), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def local_order_updated_at(conn, account: str, number: str) -> str | None:
    """返回本地该订单的 updated_at，没有则返回 None"""
    row = conn.execute(
        "SELECT updated_at FROM sales_orders WHERE account=? AND number=?",
        (account, number),
    ).fetchone()
    return row["updated_at"] if row else None


def local_detail_exists(conn, account: str, number: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sales_details WHERE account=? AND number=?",
        (account, number),
    ).fetchone()
    return bool(row)


def local_order_request_updated_at(conn, account: str, number: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT updated_at FROM sales_order_requests WHERE account=? AND number=?",
            (account, number),
        ).fetchone()
        return row["updated_at"] if row else None
    except Exception:
        return None


def local_order_request_detail_exists(conn, account: str, number: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sales_order_request_details WHERE account=? AND number=?",
            (account, number),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def ensure_order_request_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sales_order_requests (
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
            delivery_type TEXT DEFAULT '',
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
    """)
    # 兼容旧表（无 delivery_type 列）
    try:
        conn.execute("ALTER TABLE sales_order_requests ADD COLUMN delivery_type TEXT DEFAULT ''")
    except Exception:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sales_order_request_details (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            internal_id TEXT,
            date TEXT,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
    """)
    conn.commit()


def _extract_delivery_type(data: dict) -> str:
    """从 _raw.udfValue[index=1] 提取配送类型。"""
    raw = data.get("_raw") or {}
    for item in (raw.get("udfValue") or []):
        if str(item.get("index", "")) == "1":
            return str(item.get("value") or "").strip()
    return ""


def upsert_order_request(conn, data: dict, now: str):
    account      = data.get("account") or ""
    number       = data.get("number") or ""
    internal_id  = data.get("internalId") or data.get("internal_id") or ""
    date         = data.get("date") or ""
    customer_name = data.get("customerName") or data.get("customer_name") or ""
    total_qty    = float(data.get("totalQty") or data.get("total_qty") or 0)
    total_amount = float(data.get("totalAmount") or data.get("total_amount") or 0)
    bill_status      = data.get("billStatus") or data.get("bill_status") or ""
    bill_status_name = data.get("billStatusName") or data.get("bill_status_name") or ""
    check_status     = data.get("checkStatusName") or data.get("check_status") or ""
    source       = data.get("source") or ""
    sync_status  = data.get("syncStatus") or data.get("sync_status") or ""
    delivery_type = _extract_delivery_type(data)
    data_json    = json.dumps(data, ensure_ascii=False)

    conn.execute(
        """INSERT OR REPLACE INTO sales_order_requests
           (account, number, internal_id, date, customer_name, total_qty, total_amount,
            bill_status, bill_status_name, check_status, source, sync_status, delivery_type, data_json, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (account, number, internal_id, date, customer_name, total_qty, total_amount,
         bill_status, bill_status_name, check_status, source, sync_status, delivery_type, data_json, now),
    )


def upsert_order_request_detail(conn, data: dict, now: str):
    account     = data.get("account") or ""
    number      = data.get("number") or ""
    internal_id = data.get("internalId") or data.get("internal_id") or ""
    date        = data.get("date") or ""
    data_json   = json.dumps(data, ensure_ascii=False)

    conn.execute(
        """INSERT OR REPLACE INTO sales_order_request_details
           (account, number, internal_id, date, data_json, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (account, number, internal_id, date, data_json, now),
    )


def upsert_order(conn, order_data: dict, now: str):
    """order_data 是云端 /sales-order/<no> 返回的 data 字段（含 _raw）"""
    account = order_data.get("account") or ""
    number = order_data.get("number") or (order_data.get("_raw") or {}).get("number") or ""
    date = order_data.get("date") or (order_data.get("_raw") or {}).get("date") or ""
    raw = order_data.get("_raw") or {}
    customer_name = raw.get("customerName") or order_data.get("customerName") or ""
    total_qty = float(raw.get("qty") or raw.get("totalQty") or order_data.get("totalQty") or 0)
    total_amount = float(raw.get("amount") or order_data.get("totalAmount") or 0)
    check_status = str(raw.get("checkStatusName") or order_data.get("checkStatusName") or "")
    data_json = json.dumps(order_data, ensure_ascii=False)

    conn.execute(
        """INSERT OR REPLACE INTO sales_orders
           (account, number, date, customer_name, total_qty, total_amount, check_status, data_json, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (account, number, date, customer_name, total_qty, total_amount, check_status, data_json, now),
    )


def upsert_detail(conn, order_data: dict, now: str):
    account = order_data.get("account") or ""
    number = order_data.get("number") or (order_data.get("_raw") or {}).get("number") or ""
    date = order_data.get("date") or (order_data.get("_raw") or {}).get("date") or ""
    data_json = json.dumps(order_data, ensure_ascii=False)

    conn.execute(
        """INSERT OR REPLACE INTO sales_details
           (account, number, date, data_json, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (account, number, date, data_json, now),
    )


# ─── 主逻辑 ──────────────────────────────────────────────────────────────────
def fetch_with_retry(sess: requests.Session, url: str, **kwargs):
    for attempt in range(MAX_RETRIES):
        try:
            r = sess.get(url, timeout=30, **kwargs)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                print(f"  ⚠️ 重试 ({attempt+1}/{MAX_RETRIES}): {e}")
                time.sleep(RETRY_DELAY)
            else:
                raise


def date_range(start: str, end: str):
    d = datetime.strptime(start, "%Y-%m-%d")
    end_d = datetime.strptime(end, "%Y-%m-%d")
    while d <= end_d:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def sync_order_requests(sess: requests.Session, conn, dry_run: bool = False):
    """从云端拉取 sales_order_requests（销货订单）并增量写入本地。"""
    print("\n📋 同步销货订单（sales_order_requests）...")
    ensure_order_request_tables(conn)

    try:
        list_data = fetch_with_retry(
            sess,
            f"{CLOUD_BASE}/sales-order-requests",
            params={"account": "all", "limit": 1000},
        )
    except Exception as e:
        print(f"  ❌ 列表拉取失败: {e}")
        return

    items = list_data.get("items") or []
    print(f"  云端共 {len(items)} 条")

    upserted = skipped = errors = 0
    for summary in items:
        number  = summary.get("number") or ""
        account = summary.get("account") or ""
        cloud_updated = summary.get("updatedAt") or ""
        if not number:
            continue

        local_updated = local_order_request_updated_at(conn, account, number)
        detail_ok     = local_order_request_detail_exists(conn, account, number)
        if local_updated and detail_ok and local_updated >= cloud_updated:
            skipped += 1
            continue

        time.sleep(REQUEST_DELAY)
        try:
            detail_resp = fetch_with_retry(
                sess,
                f"{CLOUD_BASE}/sales-order-request/{number}",
                params={"account": account},
            )
        except Exception as e:
            print(f"  ❌ {number} 详情失败: {e}")
            errors += 1
            continue

        if not detail_resp.get("success"):
            print(f"  ⚠️ {number}: {detail_resp.get('error')}")
            errors += 1
            continue

        data = detail_resp.get("data") or {}
        if not data:
            errors += 1
            continue

        if not dry_run:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            upsert_order_request(conn, data, now)
            upsert_order_request_detail(conn, data, now)
        upserted += 1

    if not dry_run and upserted:
        conn.commit()
    print(f"  ✅ 写入: {upserted}  跳过: {skipped}  错误: {errors}")


def sync(from_date: str | None = None, dry_run: bool = False):
    sess, cloud_stats = cloud_login()
    cloud_min = cloud_stats["min_date"]
    cloud_max = cloud_stats["max_date"]

    start = from_date or cloud_min
    print(f"📅 同步范围: {start} ~ {cloud_max}")
    print(f"📊 本地DB: {LOCAL_DB}")

    conn = local_conn()

    total_dates = 0
    total_fetched = 0
    total_skipped = 0
    total_upserted = 0
    total_errors = 0

    try:
        for date_str in date_range(start, cloud_max):
            total_dates += 1
            # 拉当天订单列表
            time.sleep(REQUEST_DELAY)
            try:
                list_data = fetch_with_retry(
                    sess,
                    f"{CLOUD_BASE}/sales-orders",
                    params={"date": date_str, "source": "cache"},
                )
            except Exception as e:
                print(f"  ❌ {date_str} 列表拉取失败: {e}")
                total_errors += 1
                continue

            orders_in_day = list_data.get("list") or []
            if not orders_in_day:
                continue

            day_upserted = 0
            for summary in orders_in_day:
                total_fetched += 1
                number = summary.get("number") or ""
                account = summary.get("account") or ""
                cloud_updated = summary.get("updatedAt") or ""

                # 检查是否需要更新
                local_updated = local_order_updated_at(conn, account, number)
                detail_ok = local_detail_exists(conn, account, number)
                if local_updated and detail_ok and local_updated >= cloud_updated:
                    total_skipped += 1
                    continue

                # 拉详情
                time.sleep(REQUEST_DELAY)
                try:
                    detail_resp = fetch_with_retry(
                        sess,
                        f"{CLOUD_BASE}/sales-order/{number}",
                        params={"source": "cache"},
                    )
                except Exception as e:
                    print(f"  ❌ {number} 详情拉取失败: {e}")
                    total_errors += 1
                    continue

                if not detail_resp.get("success"):
                    print(f"  ⚠️ {number} 详情返回失败: {detail_resp.get('error')}")
                    total_errors += 1
                    continue

                order_data = detail_resp.get("data") or {}
                if not order_data:
                    print(f"  ⚠️ {number} 详情为空，跳过")
                    continue

                if not dry_run:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    upsert_order(conn, order_data, now)
                    upsert_detail(conn, order_data, now)

                total_upserted += 1
                day_upserted += 1

            if day_upserted:
                if not dry_run:
                    conn.commit()
                print(f"  ✅ {date_str}: 更新 {day_upserted} 条 (当天共 {len(orders_in_day)} 条)")

        # ── 同步销货订单 ─────────────────────────────────────────────────────
        sync_order_requests(sess, conn, dry_run=dry_run)

    except KeyboardInterrupt:
        print("\n⏹ 用户中断")
    finally:
        conn.close()

    print()
    print("─" * 50)
    print(f"📅 处理天数:   {total_dates}")
    print(f"📦 云端总条数: {total_fetched}")
    print(f"⏩ 已是最新:   {total_skipped}")
    print(f"✅ 写入/更新:  {total_upserted}")
    print(f"❌ 错误:       {total_errors}")
    if dry_run:
        print("ℹ️  dry-run 模式，未实际写入")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从云端增量同步销售订单到本地")
    parser.add_argument("--from-date", help="起始日期 YYYY-MM-DD（默认从云端最早日期）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写入数据库")
    args = parser.parse_args()

    sync(from_date=args.from_date, dry_run=args.dry_run)
