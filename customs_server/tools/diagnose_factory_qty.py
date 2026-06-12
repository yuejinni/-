"""Read-only factory quantity diagnostic tool.

This script inspects local caches and JDY read-only endpoints for a single
product. It never writes SQLite, never calls JDY write APIs, and never changes
local product display logic.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


TOOL_DIR = Path(__file__).resolve().parent
CUSTOMS_DIR = TOOL_DIR.parent
REPO_DIR = CUSTOMS_DIR.parent
if str(CUSTOMS_DIR) not in sys.path:
    sys.path.insert(0, str(CUSTOMS_DIR))

try:
    from jdy_api import JDYClient
except Exception as exc:  # pragma: no cover - reported at runtime
    JDYClient = None  # type: ignore[assignment]
    JDY_IMPORT_ERROR = exc
else:
    JDY_IMPORT_ERROR = None


DEFAULT_PROD_DB = Path(r"G:\祺航本地项目运行\服务端\_sales_cache\sales_cache.sqlite3")
DEFAULT_PROD_CONFIG = Path(r"G:\祺航本地项目运行\服务端\ai_config.json")
FACTORY_LOC_TERMS = ("厂家订单", "工厂订单", "工厂")
TRANSIT_LOC_TERM = "在途"
NEW_WAREHOUSE_TERM = "新大"
DEFAULT_GH_NUMBER = "GH20260114067"


def _num(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        text = str(value).strip().replace(",", "")
        if not text:
            return default
        return float(text)
    except Exception:
        return default


def _first(data: Dict[str, Any], keys: Sequence[str], default: Any = "") -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def _short_json(value: Any, max_len: int = 800) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= max_len else text[:max_len] + "...(truncated)"


def _without_alpha_prefix(code: str) -> str:
    return re.sub(r"^[A-Za-z]\.", "", str(code or "").strip())


def _candidate_codes(product: str, rows: Sequence[Dict[str, Any]]) -> List[str]:
    result: List[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
        no_prefix = _without_alpha_prefix(text)
        if no_prefix and no_prefix not in result:
            result.append(no_prefix)

    add(product)
    for row in rows:
        add(row.get("product_number"))
    return result


def _match_keys(product: str = "", barcode: str = "") -> set:
    keys = set()
    code = str(product or "").strip()
    if code:
        keys.add("code:" + code.upper())
        no_prefix = _without_alpha_prefix(code)
        if no_prefix:
            keys.add("code:" + no_prefix.upper())
    bc = str(barcode or "").strip()
    if bc:
        keys.add("barcode:" + bc.upper())
    return keys


def _entry_keys(entry: Dict[str, Any]) -> set:
    return _match_keys(
        str(_first(entry, ["productNumber", "productCode", "product_number", "code", "number"], "")),
        str(_first(entry, ["barCode", "barcode", "productBarcode"], "")),
    )


def _checked(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if not text:
        return False
    if text in ("false", "0", "no", "n", "unchecked", "unapproved", "未审核", "未審核"):
        return False
    if "未审核" in text or "未審核" in text:
        return False
    return (
        text in ("true", "1", "yes", "y", "checked", "approved", "已审核", "已審核")
        or "已审核" in text
        or "已審核" in text
    )


def _is_factory_source(name: Any) -> bool:
    text = str(name or "").strip()
    return bool(text and ("厂家" in text or "工厂" in text))


def _is_factory_location(name: Any) -> bool:
    text = str(name or "").strip()
    return bool(text and any(term in text for term in FACTORY_LOC_TERMS))


def _is_transit(name: Any) -> bool:
    return TRANSIT_LOC_TERM in str(name or "")


def _is_new_warehouse(name: Any) -> bool:
    return NEW_WAREHOUSE_TERM in str(name or "")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    if not _table_exists(conn, table):
        return []
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"] or 0)


def _open_readonly_db(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(str(path))
    uri = "file:" + str(path.resolve()).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _db_candidates(explicit: str = "") -> List[Path]:
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for key in ("QIHANG_SALES_CACHE_DB", "SALES_CACHE_DB"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value))
    candidates.extend(
        [
            CUSTOMS_DIR / "_sales_cache" / "sales_cache.sqlite3",
            DEFAULT_PROD_DB,
        ]
    )
    seen = set()
    result = []
    for path in candidates:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _resolve_db(explicit: str = "") -> Optional[Path]:
    for path in _db_candidates(explicit):
        if path.exists():
            return path
    return None


def _config_candidates(explicit: str = "") -> List[Path]:
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for key in ("QIHANG_CONFIG_PATH", "QIHANG_CONFIG_FILE"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value))
    candidates.extend(
        [
            Path.cwd() / "ai_config.json",
            Path.cwd() / "customs_server" / "ai_config.json",
            CUSTOMS_DIR / "ai_config.json",
            REPO_DIR / "ai_config.json",
            DEFAULT_PROD_CONFIG,
            DEFAULT_PROD_CONFIG.parent.parent / "ai_config.json",
        ]
    )
    seen = set()
    result = []
    for path in candidates:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _load_config(explicit: str = "") -> Tuple[Dict[str, Any], Optional[Path], str]:
    for path in _config_candidates(explicit):
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8-sig") as f:
                return json.load(f), path, ""
        except Exception as exc:
            return {}, path, f"{type(exc).__name__}: {exc}"
    return {}, None, "ai_config.json not found"


def _init_jdy_client(cfg: Dict[str, Any], account: str) -> Tuple[Optional[Any], str, str]:
    if JDY_IMPORT_ERROR:
        return None, "", f"jdy_api import failed: {JDY_IMPORT_ERROR}"
    if JDYClient is None:
        return None, "", "jdy_api client unavailable"
    account = str(account or "").strip()
    name1 = str(cfg.get("jdy_name") or "祺航饰品")
    name2 = str(cfg.get("jdy2_name") or "祺航箱包")
    use_second = bool(account and (account == name2 or name2 in account or ("箱包" in account and "箱包" in name2)))
    prefix = "jdy2_" if use_second else "jdy_"
    label = name2 if use_second else name1
    required = {
        "client_id": cfg.get(prefix + "client_id"),
        "app_key": cfg.get(prefix + "app_key"),
        "app_secret": cfg.get(prefix + "app_secret"),
        "db_id": cfg.get(prefix + "db_id"),
    }
    missing = [k for k, v in required.items() if not str(v or "").strip()]
    if missing:
        return None, label, "missing JDY config keys: " + ", ".join(prefix + k for k in missing)
    client = JDYClient(
        required["client_id"],
        required["app_key"],
        required["app_secret"],
        required["db_id"],
        cfg.get(prefix + "domain", ""),
        cfg.get(prefix + "app_signature", ""),
        cfg.get(prefix + "client_secret", ""),
    )
    return client, label, ""


def _local_products(conn: sqlite3.Connection, product: str, barcode: str, account: str) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "jdy_products"):
        return []
    codes = [product, _without_alpha_prefix(product)]
    codes = [c for c in dict.fromkeys(str(c or "").strip() for c in codes) if c]
    clauses = []
    params: List[Any] = []
    if codes:
        clauses.append("product_number IN (" + ",".join("?" for _ in codes) + ")")
        params.extend(codes)
    if barcode:
        clauses.append("barcode = ?")
        params.append(barcode)
    if product:
        clauses.append("data_json LIKE ?")
        params.append("%" + product + "%")
    if barcode:
        clauses.append("data_json LIKE ?")
        params.append("%" + barcode + "%")
    if not clauses:
        return []
    where = "(" + " OR ".join(clauses) + ")"
    if account:
        where += " AND account = ?"
        params.append(account)
    sql = f"""
        SELECT id, account, product_id, product_number, product_name, spec, barcode,
               unit_name, category_name, default_supplier_number, default_supplier_name,
               status, updated_at, last_seen_at, data_json
        FROM jdy_products
        WHERE {where}
        ORDER BY account, product_number
        LIMIT 20
    """
    rows = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        data = {}
        try:
            data = json.loads(item.get("data_json") or "{}")
        except Exception:
            data = {}
        item["propertys_count"] = len(data.get("propertys") or []) if isinstance(data, dict) else 0
        item.pop("data_json", None)
        rows.append(item)
    return rows


def _local_sales_quantities(conn: sqlite3.Connection, account: str, codes: Sequence[str]) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "sales_product_quantities") or not codes:
        return []
    params: List[Any] = []
    where = "code IN (" + ",".join("?" for _ in codes) + ")"
    params.extend(codes)
    if account:
        where += " AND account = ?"
        params.append(account)
    return [dict(row) for row in conn.execute(f"SELECT * FROM sales_product_quantities WHERE {where}", params).fetchall()]


def _local_purchase_cache(conn: sqlite3.Connection, account: str, codes: Sequence[str], barcode: str, gh: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for table in ("purchase_inbounds", "accessory_purchase_orders", "purchase_order_prices", "purchase_attachment_items"):
        info = {
            "exists": _table_exists(conn, table),
            "count": _table_count(conn, table),
            "matches": [],
        }
        if not info["exists"]:
            result[table] = info
            continue
        cols = _table_columns(conn, table)
        clauses = []
        params: List[Any] = []
        if account and "account" in cols:
            account_clause = "account = ?"
            account_param = [account]
        else:
            account_clause = ""
            account_param = []
        for col in ("number", "order_number", "source_number"):
            if gh and col in cols:
                clauses.append(f"{col} = ?")
                params.append(gh)
        for col in ("product_number", "product_code", "code"):
            if col in cols and codes:
                clauses.append(f"{col} IN (" + ",".join("?" for _ in codes) + ")")
                params.extend(codes)
        if barcode and "barcode" in cols:
            clauses.append("barcode = ?")
            params.append(barcode)
        if "data_json" in cols:
            if gh:
                clauses.append("data_json LIKE ?")
                params.append("%" + gh + "%")
            for code in codes:
                clauses.append("data_json LIKE ?")
                params.append("%" + code + "%")
            if barcode:
                clauses.append("data_json LIKE ?")
                params.append("%" + barcode + "%")
        if clauses:
            where = "(" + " OR ".join(clauses) + ")"
            if account_clause:
                where += " AND " + account_clause
                params.extend(account_param)
            sql = f"SELECT * FROM {table} WHERE {where} LIMIT 20"
            for row in conn.execute(sql, params).fetchall():
                compact = dict(row)
                for key, value in list(compact.items()):
                    if isinstance(value, str) and len(value) > 800:
                        compact[key] = value[:800] + "...(truncated)"
                info["matches"].append(compact)
        result[table] = info
    return result


def _local_transfer_diagnostic(
    conn: sqlite3.Connection,
    account: str,
    codes: Sequence[str],
    barcode: str,
) -> Dict[str, Any]:
    result = {"stock_transit": 0.0, "counted_sources": [], "related_orders": []}
    if not _table_exists(conn, "transfer_details"):
        return result
    target_keys = set()
    for code in codes:
        target_keys.update(_match_keys(code, barcode))
    clauses = []
    params: List[Any] = []
    if account:
        clauses.append("account = ?")
        params.append(account)
    likes = []
    for code in codes:
        likes.append("data_json LIKE ?")
        params.append("%" + code + "%")
    if barcode:
        likes.append("data_json LIKE ?")
        params.append("%" + barcode + "%")
    if likes:
        clauses.append("(" + " OR ".join(likes) + ")")
    where = " AND ".join(clauses) if clauses else "1=1"
    rows = conn.execute(
        f"SELECT account, number, date, data_json FROM transfer_details WHERE {where} ORDER BY date, number",
        params,
    ).fetchall()
    for row in rows:
        try:
            order = json.loads(row["data_json"] or "{}")
        except Exception:
            continue
        check_status = _first(order, ["checkStatus", "checkStatusName", "statusName", "status"], "")
        checked = _checked(check_status)
        matches = []
        counted_delta = 0.0
        for entry in order.get("entries") or order.get("details") or order.get("items") or []:
            if not isinstance(entry, dict):
                continue
            if not (target_keys & _entry_keys(entry)):
                continue
            out_loc = str(_first(entry, ["outLocationName", "outWarehouseName", "outStockName"], ""))
            in_loc = str(_first(entry, ["inLocationName", "inWarehouseName", "inStockName"], ""))
            qty = _num(_first(entry, ["qty", "baseQty", "mainQty", "quantity"], 0))
            delta = 0.0
            reason = "not_counted"
            if checked and _is_factory_source(out_loc) and _is_transit(in_loc):
                delta = qty
                reason = "checked_factory_to_transit"
            elif checked and _is_transit(out_loc) and _is_new_warehouse(in_loc):
                delta = -qty
                reason = "checked_transit_to_new_warehouse"
            elif not checked:
                reason = "unchecked_not_counted"
            counted_delta += delta
            matches.append(
                {
                    "productNumber": _first(entry, ["productNumber", "productCode", "code"], ""),
                    "barcode": _first(entry, ["barCode", "barcode", "productBarcode"], ""),
                    "qty": qty,
                    "unit": _first(entry, ["unitName", "unit", "baseUnitName"], ""),
                    "outLocationName": out_loc,
                    "inLocationName": in_loc,
                    "delta": delta,
                    "reason": reason,
                    "remark": _first(entry, ["remark"], ""),
                }
            )
        if not matches:
            continue
        if counted_delta:
            result["stock_transit"] += counted_delta
            result["counted_sources"].append(
                {
                    "number": order.get("number") or row["number"],
                    "date": order.get("date") or row["date"],
                    "checkStatus": check_status,
                    "delta": counted_delta,
                }
            )
        result["related_orders"].append(
            {
                "number": order.get("number") or row["number"],
                "date": order.get("date") or row["date"],
                "checkStatus": check_status,
                "checked": checked,
                "matches": matches,
            }
        )
    result["stock_transit"] = max(_num(result["stock_transit"]), 0.0)
    return result


def _jdy_call(label: str, fn, verbose: bool) -> Tuple[Any, str]:
    try:
        if verbose:
            return fn(), ""
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            return fn(), ""
    except Exception as exc:
        return None, f"{label}: {type(exc).__name__}: {exc}"


def _inventory_qty(row: Dict[str, Any]) -> float:
    return _num(_first(row, ["qty", "quantity", "stockQty", "inventoryQty", "availableQty", "baseQty"], 0))


def _entry_qty(entry: Dict[str, Any]) -> float:
    return _num(_first(entry, ["qty", "mainQty", "quantity", "baseQty"], 0))


def _linked_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "sourceBillNo",
        "sourceNumber",
        "linkNumber",
        "relationNumber",
        "associatedNumber",
        "convertNumber",
        "purchaseNumber",
        "sourceBillId",
        "sourceBillNoList",
    ]
    return {key: data.get(key) for key in keys if data.get(key) not in (None, "", [], {})}


def _order_entries(order: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    entries = order.get("entries") or order.get("items") or order.get("details") or order.get("entryList") or []
    return entries if isinstance(entries, list) else []


def _entry_matches(entry: Dict[str, Any], codes: Sequence[str], barcode: str) -> bool:
    target = set()
    for code in codes:
        target.update(_match_keys(code, barcode))
    return bool(target & _entry_keys(entry))


def _jdy_inventory(client: Any, codes: Sequence[str], barcode: str, verbose: bool) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen = set()
    for code in codes:
        data, err = _jdy_call(
            f"inventory {code}",
            lambda code=code: client.get_inventory_by_product(code, page_size=100),
            verbose,
        )
        if err:
            errors.append(err)
            continue
        for row in (data or {}).get("list") or []:
            if not isinstance(row, dict):
                continue
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            product_no = str(row.get("productNumber") or row.get("number") or "").strip()
            row_barcode = str(row.get("barCode") or row.get("barcode") or "").strip()
            if product_no and product_no not in codes and _without_alpha_prefix(product_no) not in codes and row_barcode != barcode:
                continue
            location = str(_first(row, ["locationName", "warehouseName", "stockName"], ""))
            qty = _inventory_qty(row)
            rows.append(
                {
                    "productNumber": product_no,
                    "barcode": row_barcode,
                    "locationName": location,
                    "qty": qty,
                    "unit": _first(row, ["unitName", "unit", "baseUnitName"], ""),
                    "is_factory_related": _is_factory_location(location),
                    "is_transit": _is_transit(location),
                    "raw_head": _short_json(row, 500),
                }
            )
    factory_total = sum(r["qty"] for r in rows if r["is_factory_related"])
    transit_total = sum(r["qty"] for r in rows if r["is_transit"])
    return {
        "rows": rows,
        "factory_related_total": factory_total,
        "transit_total": transit_total,
        "errors": errors,
    }


def _summarize_purchase_order(
    order: Dict[str, Any],
    codes: Sequence[str],
    barcode: str,
    begin_date: str,
) -> Optional[Dict[str, Any]]:
    matching_entries = []
    for entry in _order_entries(order):
        if not isinstance(entry, dict) or not _entry_matches(entry, codes, barcode):
            continue
        matching_entries.append(
            {
                "productNumber": _first(entry, ["productNumber", "productCode", "code"], ""),
                "barcode": _first(entry, ["barCode", "barcode", "productBarcode"], ""),
                "productName": _first(entry, ["productName", "name"], ""),
                "qty": _entry_qty(entry),
                "unit": _first(entry, ["unitName", "unit", "baseUnitName"], ""),
                "linked_fields": _linked_fields(entry),
                "raw_head": _short_json(entry, 500),
            }
        )
    if not matching_entries:
        return None
    linked = _linked_fields(order)
    bill_status = _first(order, ["billStatus", "billStatusName"], "")
    check_status = _first(order, ["checkStatus", "checkStatusName"], "")
    entry_linked = any(bool(x["linked_fields"]) for x in matching_entries)
    order_linked = bool(linked)
    date_str = str(_first(order, ["date", "billDate", "createTime"], ""))[:10]
    unfinished = not order_linked and not entry_linked
    not_counted_reasons = []
    if begin_date and date_str and date_str < begin_date:
        unfinished = False
        not_counted_reasons.append(f"before_new_logic_begin_date:{begin_date}")
    if not _checked(check_status):
        unfinished = False
        not_counted_reasons.append("unchecked")
    if str(bill_status).strip() not in ("", "0", "未转", "未生成", "未完成"):
        unfinished = False
        not_counted_reasons.append(f"bill_status:{bill_status}")
    if order_linked or entry_linked:
        not_counted_reasons.append("linked")
    return {
        "number": _first(order, ["number", "billNo", "orderNo"], ""),
        "date": date_str,
        "supplierNumber": _first(order, ["supplierNumber"], ""),
        "supplierName": _first(order, ["supplierName"], ""),
        "billStatus": bill_status,
        "checkStatus": check_status,
        "linked_fields": linked,
        "entries": matching_entries,
        "qty_total": sum(x["qty"] for x in matching_entries),
        "unfinished_by_diagnostic_rule": unfinished,
        "not_counted_reasons": not_counted_reasons,
        "raw_head": _short_json(order, 700),
    }


def _jdy_purchase_orders(
    client: Any,
    codes: Sequence[str],
    barcode: str,
    begin_date: str,
    max_pages: int,
    verbose: bool,
) -> Dict[str, Any]:
    attempts = []
    summaries: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen_numbers = set()
    for code in codes:
        filters = [
            {"check_status": 1, "bill_status": 0, "begin_date": begin_date},
            {"check_status": 1, "bill_status": None, "begin_date": begin_date},
            {"check_status": 2, "bill_status": None, "begin_date": ""},
        ]
        for flt in filters:
            for page in range(1, max_pages + 1):
                attempts.append({"product_number": code, "page": page, **flt})
                data, err = _jdy_call(
                    f"purchaseOrder {code} page {page}",
                    lambda code=code, page=page, flt=flt: client.get_purchase_order_requests(
                        page=page,
                        page_size=100,
                        product_number=code,
                        begin_date=flt.get("begin_date") or "",
                        check_status=flt.get("check_status"),
                        bill_status=flt.get("bill_status"),
                    ),
                    verbose,
                )
                if err:
                    errors.append(err)
                    break
                batch = (data or {}).get("list") or []
                for order in batch:
                    if not isinstance(order, dict):
                        continue
                    summary = _summarize_purchase_order(order, codes, barcode, begin_date)
                    if not summary:
                        continue
                    key = summary.get("number") or _short_json(summary.get("raw_head", ""), 200)
                    if key in seen_numbers:
                        continue
                    seen_numbers.add(key)
                    summaries.append(summary)
                total = int((data or {}).get("total") or 0)
                if len(batch) < 100 or (total and page * 100 >= total):
                    break
    unfinished_total = sum(x["qty_total"] for x in summaries if x["unfinished_by_diagnostic_rule"])
    return {
        "attempts": attempts,
        "orders": summaries,
        "unfinished_qty_total": unfinished_total,
        "errors": errors,
    }


def _summarize_purchase_bill(order: Dict[str, Any], codes: Sequence[str], barcode: str) -> Dict[str, Any]:
    entries = []
    for entry in _order_entries(order):
        if isinstance(entry, dict) and _entry_matches(entry, codes, barcode):
            entries.append(
                {
                    "productNumber": _first(entry, ["productNumber", "productCode", "code"], ""),
                    "barcode": _first(entry, ["barCode", "barcode", "productBarcode"], ""),
                    "qty": _entry_qty(entry),
                    "unit": _first(entry, ["unitName", "unit", "baseUnitName"], ""),
                    "raw_head": _short_json(entry, 500),
                }
            )
    return {
        "number": _first(order, ["number", "billNo", "orderNo"], ""),
        "date": _first(order, ["date", "billDate", "createTime"], ""),
        "supplierNumber": _first(order, ["supplierNumber"], ""),
        "supplierName": _first(order, ["supplierName"], ""),
        "checkStatus": _first(order, ["checkStatus", "checkStatusName"], ""),
        "entries": entries,
        "qty_total": sum(x["qty"] for x in entries),
        "raw_head": _short_json(order, 700),
    }


def _jdy_purchase_bills(
    client: Any,
    codes: Sequence[str],
    barcode: str,
    gh: str,
    verbose: bool,
) -> Dict[str, Any]:
    result = {"by_number": [], "by_product": [], "errors": []}
    if gh:
        data, err = _jdy_call(
            f"purchase/list {gh}",
            lambda: client.get_purchase_orders(search=gh, page=1, page_size=20),
            verbose,
        )
        if err:
            result["errors"].append(err)
        else:
            for order in (data or {}).get("list") or []:
                if isinstance(order, dict):
                    result["by_number"].append(_summarize_purchase_bill(order, codes, barcode))
    for code in codes[:2]:
        data, err = _jdy_call(
            f"purchase/list product {code}",
            lambda code=code: client.get_purchase_orders_by_product(code, page=1, page_size=20),
            verbose,
        )
        if err:
            result["errors"].append(err)
            continue
        for order in (data or {}).get("list") or []:
            if isinstance(order, dict):
                summary = _summarize_purchase_bill(order, codes, barcode)
                if summary["entries"]:
                    result["by_product"].append(summary)
    return result


def _source_judgement(
    inventory: Dict[str, Any],
    purchase_orders: Dict[str, Any],
    expected: float,
) -> str:
    inv_total = _num(inventory.get("factory_related_total"))
    po_total = _num(purchase_orders.get("unfinished_qty_total"))
    if expected and abs(inv_total - expected) < 0.0001:
        return f"{expected:g}DZ 更符合 JDY 库存接口中“厂家订单/工厂订单/工厂”相关仓库数量。"
    if expected and abs(po_total - expected) < 0.0001:
        return f"{expected:g}DZ 更符合 JDY 未完成采购订单明细数量。"
    if inv_total > 0 and po_total <= 0:
        return f"当前更偏向库存仓库来源；JDY 相关仓库合计 {inv_total:g}，未完成采购订单合计 {po_total:g}。"
    if po_total > 0 and inv_total <= 0:
        return f"当前更偏向未完成采购订单来源；JDY 相关仓库合计 {inv_total:g}，未完成采购订单合计 {po_total:g}。"
    return f"未能唯一判断；JDY 相关仓库合计 {inv_total:g}，未完成采购订单合计 {po_total:g}。"


def _print_section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def run(args: argparse.Namespace) -> int:
    db_path = _resolve_db(args.db)
    local = {
        "db_path": str(db_path) if db_path else "",
        "db_open_mode": "ro" if db_path else "",
        "product_rows": [],
        "sales_product_quantities": [],
        "transfer_details": {},
        "purchase_cache": {},
        "errors": [],
    }
    product_rows: List[Dict[str, Any]] = []
    codes = [args.product, _without_alpha_prefix(args.product)]
    codes = [c for c in dict.fromkeys(str(c or "").strip() for c in codes) if c]
    if db_path:
        try:
            with _open_readonly_db(db_path) as conn:
                product_rows = _local_products(conn, args.product, args.barcode, args.account)
                codes = _candidate_codes(args.product, product_rows)
                local["product_rows"] = product_rows
                local["sales_product_quantities"] = _local_sales_quantities(conn, args.account, codes)
                local["transfer_details"] = _local_transfer_diagnostic(conn, args.account, codes, args.barcode)
                local["purchase_cache"] = _local_purchase_cache(conn, args.account, codes, args.barcode, args.gh_number)
        except Exception as exc:
            local["errors"].append(f"{type(exc).__name__}: {exc}")
    else:
        local["errors"].append("sales_cache.sqlite3 not found")

    cfg, cfg_path, cfg_error = _load_config(args.config)
    jdy = {
        "enabled": not args.no_jdy,
        "config_path": str(cfg_path) if cfg_path else "",
        "account_label": "",
        "read_only_endpoints": [
            "GET /jdyscm/inventory/list",
            "POST /jdyscm/purchaseOrder/list",
            "POST /jdyscm/purchase/list",
        ],
        "inventory": {},
        "purchase_orders": {},
        "purchase_bills": {},
        "errors": [],
    }
    if cfg_error:
        jdy["errors"].append(cfg_error)

    if not args.no_jdy:
        client, label, err = _init_jdy_client(cfg, args.account)
        jdy["account_label"] = label
        if err:
            jdy["errors"].append(err)
        elif client:
            jdy["inventory"] = _jdy_inventory(client, codes, args.barcode, args.verbose_jdy_logs)
            jdy["purchase_orders"] = _jdy_purchase_orders(
                client,
                codes,
                args.barcode,
                args.purchase_begin_date,
                args.max_pages,
                args.verbose_jdy_logs,
            )
            jdy["purchase_bills"] = _jdy_purchase_bills(
                client,
                codes,
                args.barcode,
                args.gh_number,
                args.verbose_jdy_logs,
            )

    _print_section("1. 本地商品主档")
    _print_json({"db_path": local["db_path"], "db_open_mode": local["db_open_mode"], "rows": local["product_rows"], "errors": local["errors"]})

    _print_section("2. 本地 sales_product_quantities")
    _print_json(local["sales_product_quantities"])

    _print_section("3. 本地 transfer_details 在途推导")
    _print_json(local["transfer_details"])

    _print_section("4. 本地采购/购货缓存命中情况")
    _print_json(local["purchase_cache"])

    _print_section("5. JDY 库存接口返回的相关仓库数量")
    _print_json(jdy["inventory"] if jdy["enabled"] else {"skipped": True})

    _print_section("6. JDY 采购订单接口返回的未完成数量和关联单号")
    _print_json(jdy["purchase_orders"] if jdy["enabled"] else {"skipped": True})

    _print_section(f"7. JDY 购货单接口是否查到 {args.gh_number}")
    _print_json(jdy["purchase_bills"] if jdy["enabled"] else {"skipped": True})

    _print_section("8. 128DZ 来源判断")
    if jdy["enabled"] and jdy.get("inventory") and jdy.get("purchase_orders"):
        print(_source_judgement(jdy["inventory"], jdy["purchase_orders"], args.expected_factory_qty))
    else:
        print("JDY 只读查询未完成，无法判断 128DZ 来源。")
    if jdy["errors"]:
        print("JDY 查询/配置提示：")
        _print_json(jdy["errors"])

    _print_section("9. 后续应新增的本地缓存表字段建议")
    _print_json(
        {
            "purchase_order_headers": [
                "account",
                "number",
                "date",
                "supplier_number",
                "supplier_name",
                "check_status",
                "bill_status",
                "linked_bill_no",
                "data_json",
                "updated_at",
            ],
            "purchase_order_items": [
                "account",
                "order_number",
                "product_number",
                "barcode",
                "product_name",
                "qty",
                "unit",
                "linked_bill_no",
                "line_status",
                "data_json",
                "updated_at",
            ],
            "inventory_snapshots": [
                "account",
                "product_number",
                "barcode",
                "warehouse_name",
                "qty",
                "unit",
                "snapshot_at",
                "data_json",
            ],
        }
    )

    _print_section("安全声明")
    _print_json(
        {
            "sqlite_mode": "read-only mode=ro",
            "jdy_write_called": False,
            "production_deploy": False,
            "page_display_logic_changed": False,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读诊断单个商品厂家订单/工厂数量来源。")
    parser.add_argument("--product", required=True, help="商品编号，例如 H.78669H-9 或 78669H-9")
    parser.add_argument("--barcode", default="", help="条码，例如 260114H036001")
    parser.add_argument("--account", default="祺航饰品", help="账套名称，默认 祺航饰品")
    parser.add_argument("--db", default="", help="sales_cache.sqlite3 路径；默认自动查找，只读 mode=ro 打开")
    parser.add_argument("--config", default="", help="ai_config.json 路径；默认自动查找，只读读取")
    parser.add_argument("--gh-number", default=DEFAULT_GH_NUMBER, help="要核验的购货单号")
    parser.add_argument("--purchase-begin-date", default="2026-05-29", help="采购订单新逻辑开始日期")
    parser.add_argument("--expected-factory-qty", type=float, default=128.0, help="期望核对的厂家订单数量")
    parser.add_argument("--max-pages", type=int, default=2, help="采购订单列表每个过滤条件最多读取页数")
    parser.add_argument("--no-jdy", action="store_true", help="跳过 JDY 只读接口，仅检查本地缓存")
    parser.add_argument("--verbose-jdy-logs", action="store_true", help="显示 jdy_api 客户端请求日志")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
