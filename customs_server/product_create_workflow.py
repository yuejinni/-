"""Local-first product creation workflow for the main Flask service.

The module owns draft storage, validation, JDY payload dry-runs, and the
guarded submit path. Real JDY writes are only reached when a route explicitly
injects ``RealJdySubmitter`` after admin and environment checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import date, datetime
from typing import Any, Mapping


CONFIRM_SUBMIT_CODE = "SUBMIT_PRODUCT_CREATE"
CONFIRM_REAL_JDY_CODE = "REAL_JDY_PRODUCT_CREATE"

WRITE_ACTIONS = (
    "product_add",
    "product_image_update",
    "product_price_update",
    "purchase_order_add",
)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def num_value(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def json_dumps(data: Any) -> str:
    return json.dumps(data if data is not None else {}, ensure_ascii=False, sort_keys=True)


def json_loads(text: Any, default: Any = None) -> Any:
    if default is None:
        default = {}
    try:
        if not text:
            return default
        return json.loads(text)
    except Exception:
        return default


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def short_hash(payload: Any, size: int = 12) -> str:
    return payload_hash(payload)[:size]


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_create_sequences (
            account_key TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'global',
            current_seq INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account_key, scope)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_create_draft (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_no TEXT UNIQUE NOT NULL,
            account_key TEXT NOT NULL,
            account TEXT NOT NULL DEFAULT '',
            title TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            idempotency_key TEXT NOT NULL UNIQUE,
            payload_hash TEXT DEFAULT '',
            source TEXT DEFAULT 'quick_create',
            created_by TEXT DEFAULT '',
            created_by_name TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            validated_at TEXT DEFAULT '',
            dry_run_at TEXT DEFAULT '',
            approved_at TEXT DEFAULT '',
            approved_by TEXT DEFAULT '',
            submitted_at TEXT DEFAULT '',
            submitted_by TEXT DEFAULT '',
            submit_confirm_code TEXT DEFAULT '',
            locked_at TEXT DEFAULT '',
            locked_by TEXT DEFAULT '',
            lock_token TEXT DEFAULT '',
            last_error TEXT DEFAULT '',
            jdy_result_json TEXT DEFAULT '{}',
            sync_status TEXT DEFAULT 'not_needed',
            data_json TEXT DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_create_draft_item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER NOT NULL,
            line_no INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'draft',
            account_key TEXT NOT NULL,
            account TEXT NOT NULL DEFAULT '',
            product_number TEXT DEFAULT '',
            product_name TEXT NOT NULL,
            spec TEXT DEFAULT '',
            category_id TEXT DEFAULT '',
            category_name TEXT DEFAULT '',
            category_prefix TEXT DEFAULT '',
            category_small_code TEXT DEFAULT '',
            category_code_2d TEXT DEFAULT '',
            unit_id TEXT DEFAULT '',
            unit_name TEXT DEFAULT '',
            supplier_id TEXT DEFAULT '',
            supplier_number TEXT DEFAULT '',
            supplier_name TEXT DEFAULT '',
            tax_no TEXT DEFAULT '',
            transformed_code TEXT DEFAULT '',
            purchase_price REAL DEFAULT 0,
            purchase_qty REAL DEFAULT 1,
            sale_price REAL DEFAULT 0,
            image_local_path TEXT DEFAULT '',
            image_b64 TEXT DEFAULT '',
            image_sha256 TEXT DEFAULT '',
            ka_barcode TEXT DEFAULT '',
            remark TEXT DEFAULT '',
            color TEXT DEFAULT '',
            seq_preview INTEGER DEFAULT 0,
            seq_reserved INTEGER DEFAULT 0,
            ean13 TEXT DEFAULT '',
            jdy_product_id TEXT DEFAULT '',
            jdy_error TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}',
            validation_json TEXT DEFAULT '{}',
            customs_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(draft_id, line_no)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_create_submit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER NOT NULL,
            item_id INTEGER DEFAULT 0,
            action TEXT NOT NULL,
            endpoint TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            response_json TEXT DEFAULT '{}',
            error TEXT DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            called_jdy INTEGER NOT NULL DEFAULT 0,
            dry_run INTEGER NOT NULL DEFAULT 1,
            actor TEXT DEFAULT '',
            actor_role TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            finished_at TEXT DEFAULT '',
            UNIQUE(idempotency_key, action, payload_hash)
        )
        """
    )
    _ensure_columns(
        conn,
        "product_create_draft",
        {
            "account_key": "TEXT NOT NULL DEFAULT 'account1'",
            "account": "TEXT NOT NULL DEFAULT ''",
            "payload_hash": "TEXT DEFAULT ''",
            "approved_at": "TEXT DEFAULT ''",
            "approved_by": "TEXT DEFAULT ''",
            "locked_at": "TEXT DEFAULT ''",
            "locked_by": "TEXT DEFAULT ''",
            "lock_token": "TEXT DEFAULT ''",
            "sync_status": "TEXT DEFAULT 'not_needed'",
        },
    )
    _ensure_columns(
        conn,
        "product_create_draft_item",
        {
            "account_key": "TEXT NOT NULL DEFAULT 'account1'",
            "account": "TEXT NOT NULL DEFAULT ''",
            "purchase_qty": "REAL DEFAULT 1",
            "image_sha256": "TEXT DEFAULT ''",
            "seq_reserved": "INTEGER DEFAULT 0",
            "customs_json": "TEXT DEFAULT '{}'",
        },
    )
    _ensure_columns(
        conn,
        "product_create_submit_log",
        {
            "endpoint": "TEXT NOT NULL DEFAULT ''",
            "called_jdy": "INTEGER NOT NULL DEFAULT 0",
            "dry_run": "INTEGER NOT NULL DEFAULT 1",
            "actor": "TEXT DEFAULT ''",
            "actor_role": "TEXT DEFAULT ''",
        },
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_product_create_draft_status ON product_create_draft(status, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_product_create_draft_account ON product_create_draft(account_key, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_product_create_item_draft ON product_create_draft_item(draft_id, line_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_product_create_item_product ON product_create_draft_item(account_key, product_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_product_create_log_draft ON product_create_submit_log(draft_id, created_at)")


def _ensure_columns(conn: sqlite3.Connection, table: str, additions: Mapping[str, str]) -> None:
    cols = {
        str(row["name"]).lower()
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, ddl in additions.items():
        if name.lower() not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            cols.add(name.lower())


def load_json_file(path: str) -> dict:
    try:
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def category_map_path(base_dir: str) -> str:
    return os.path.join(base_dir, "category_code_map.json")


def read_categories(base_dir: str, account_key: str = "account1") -> list[dict]:
    raw = load_json_file(category_map_path(base_dir))
    source = raw.get(account_key) if isinstance(raw.get(account_key), dict) else raw
    rows = []
    for cat_id, info in (source or {}).items():
        if str(cat_id).startswith("_") or not isinstance(info, Mapping):
            continue
        path = clean_text(info.get("path"))
        prefix = clean_text(info.get("prefix") or info.get("letter")).upper()
        small_code = clean_text(info.get("small_code") or info.get("smallCode") or info.get("code_2d"))
        code_2d = clean_text(info.get("code_2d") or info.get("code2d") or small_code)
        rows.append(
            {
                "id": str(cat_id),
                "path": path,
                "prefix": prefix,
                "small_code": small_code.zfill(2) if small_code else "",
                "code_2d": code_2d.zfill(2) if code_2d else "",
                "ready": bool(prefix and small_code and code_2d),
            }
        )
    rows.sort(key=lambda x: (not x["ready"], x["path"], x["id"]))
    return rows


def get_category_info(base_dir: str, account_key: str, category_id: str) -> dict:
    category_id = clean_text(category_id)
    for row in read_categories(base_dir, account_key):
        if row["id"] == category_id:
            return row
    return {}


def _sequence_current(conn: sqlite3.Connection, account_key: str, scope: str = "global") -> int:
    if not _table_exists(conn, "product_create_sequences"):
        return 0
    row = conn.execute(
        "SELECT current_seq FROM product_create_sequences WHERE account_key = ? AND scope = ?",
        (account_key, scope),
    ).fetchone()
    return int(row["current_seq"] or 0) if row else 0


def peek_next_seq(conn: sqlite3.Connection, account_key: str, scope: str = "global") -> int:
    return _sequence_current(conn, account_key, scope) + 1


def reserve_next_seq(conn: sqlite3.Connection, account_key: str, scope: str = "global") -> int:
    now = now_text()
    row = conn.execute(
        "SELECT current_seq FROM product_create_sequences WHERE account_key = ? AND scope = ?",
        (account_key, scope),
    ).fetchone()
    if row:
        next_seq = int(row["current_seq"] or 0) + 1
        conn.execute(
            "UPDATE product_create_sequences SET current_seq = ?, updated_at = ? WHERE account_key = ? AND scope = ?",
            (next_seq, now, account_key, scope),
        )
    else:
        next_seq = 1
        conn.execute(
            "INSERT INTO product_create_sequences (account_key, scope, current_seq, updated_at) VALUES (?, ?, ?, ?)",
            (account_key, scope, next_seq, now),
        )
    return next_seq


def transform_vendor_code(tax_no: str) -> tuple[str, str]:
    try:
        from jdy_register.code_gen import transform_vendor_code as _transform

        transformed, error = _transform(tax_no)
        return clean_text(transformed), clean_text(error)
    except Exception:
        s = clean_text(tax_no).upper()
        if len(s) < 2:
            return s, "tax_no must have at least two chars"
        c1, c2, rest = s[0], s[1], s[2:]
        digit_to_letter = {
            "0": "J",
            "1": "A",
            "2": "B",
            "3": "C",
            "4": "D",
            "5": "E",
            "6": "F",
            "7": "G",
            "8": "H",
            "9": "I",
        }
        if c1.isdigit() and c2.isdigit():
            return digit_to_letter.get(c1, c1) + str((int(c2) + 1) % 10) + rest, ""
        if c1.isalpha() and c2.isdigit():
            return str(ord(c1) - ord("A") + 1) + str((int(c2) + 1) % 10) + rest, ""
        return s, "unsupported tax_no prefix"


def generate_product_number(prefix: str, transformed: str, small_code: str, seq: int) -> str:
    try:
        from jdy_register.code_gen import generate_product_number as _generate

        return _generate(prefix, transformed, small_code, seq)
    except Exception:
        return f"{clean_text(prefix).upper()}.{clean_text(transformed).upper()}{str(small_code).zfill(2)}-{int(seq):04d}"


def generate_ean13(code_2d: str, seq: int, dt: date | datetime | None = None) -> str:
    try:
        from jdy_register.code_gen import generate_ean13 as _generate

        return _generate(code_2d, seq, dt)
    except Exception:
        dt = dt or datetime.now()
        raw12 = f"{dt.strftime('%y%m%d')}{str(code_2d).zfill(2)}{int(seq):04d}"
        total = sum(int(raw12[i]) * (1 if i % 2 == 0 else 3) for i in range(12))
        return raw12 + str((10 - (total % 10)) % 10)


def _supplier_from_cache(
    conn: sqlite3.Connection,
    account: str,
    supplier_number: str = "",
    supplier_id: str = "",
) -> dict:
    if not _table_exists(conn, "jdy_suppliers"):
        return {}
    supplier_number = clean_text(supplier_number)
    supplier_id = clean_text(supplier_id)
    account = clean_text(account)
    row = None
    if supplier_number:
        row = conn.execute(
            "SELECT * FROM jdy_suppliers WHERE account = ? AND number = ?",
            (account, supplier_number),
        ).fetchone()
    if not row and supplier_id:
        for candidate in conn.execute("SELECT * FROM jdy_suppliers WHERE account = ?", (account,)).fetchall():
            raw = json_loads(candidate["data_json"])
            if clean_text(raw.get("id") or raw.get("supplierId")) == supplier_id:
                row = candidate
                break
    if not row:
        return {}
    raw = json_loads(row["data_json"])
    return {
        "number": clean_text(row["number"]),
        "name": clean_text(row["name"]),
        "id": clean_text(raw.get("id") or raw.get("supplierId") or supplier_id),
        "tax_no": clean_text(
            raw.get("taxPayerNo")
            or raw.get("taxpayerNo")
            or raw.get("tax_no")
            or raw.get("contact")
            or ""
        ),
        "raw": raw,
    }


def search_suppliers(conn: sqlite3.Connection, account: str = "all", q: str = "", limit: int = 50) -> list[dict]:
    if not _table_exists(conn, "jdy_suppliers"):
        return []
    limit = max(1, min(int(limit or 50), 200))
    clauses = []
    params: list[Any] = []
    if account and account != "all":
        clauses.append("account = ?")
        params.append(account)
    if q:
        clauses.append("(LOWER(number) LIKE ? OR LOWER(name) LIKE ? OR LOWER(data_json) LIKE ?)")
        kw = f"%{q.lower()}%"
        params.extend([kw, kw, kw])
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"""
        SELECT account, number, name, data_json
        FROM jdy_suppliers
        {where}
        ORDER BY name, number
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    items = []
    for row in rows:
        raw = json_loads(row["data_json"])
        items.append(
            {
                "account": row["account"] or "",
                "number": row["number"] or "",
                "name": row["name"] or "",
                "id": clean_text(raw.get("id") or raw.get("supplierId")),
                "tax_no": clean_text(raw.get("taxPayerNo") or raw.get("tax_no") or raw.get("contact")),
            }
        )
    return items


def read_units(conn: sqlite3.Connection, account: str = "all", limit: int = 100) -> list[dict]:
    if not _table_exists(conn, "jdy_products"):
        return []
    clauses = ["COALESCE(unit_name, '') <> ''"]
    params: list[Any] = []
    if account and account != "all":
        clauses.append("account = ?")
        params.append(account)
    rows = conn.execute(
        f"""
        SELECT unit_id, unit_name, COUNT(*) AS c
        FROM jdy_products
        WHERE {' AND '.join(clauses)}
        GROUP BY unit_id, unit_name
        ORDER BY c DESC, unit_name
        LIMIT ?
        """,
        [*params, max(1, min(int(limit or 100), 200))],
    ).fetchall()
    return [{"unit_id": r["unit_id"] or "", "unit_name": r["unit_name"] or "", "count": r["c"]} for r in rows]


def normalize_items(input_items: Any) -> list[dict]:
    items = input_items or []
    if isinstance(items, Mapping):
        items = [items]
    normalized = []
    for idx, item in enumerate(items, start=1):
        item = dict(item or {})
        normalized.append(
            {
                "line_no": int(item.get("line_no") or idx),
                "product_name": clean_text(item.get("product_name") or item.get("productName") or item.get("name")),
                "spec": clean_text(item.get("spec")),
                "category_id": clean_text(item.get("category_id") or item.get("cat_id") or item.get("categoryId")),
                "unit_id": clean_text(item.get("unit_id") or item.get("unitId")),
                "unit_name": clean_text(item.get("unit_name") or item.get("unit") or item.get("unitName")),
                "supplier_id": clean_text(item.get("supplier_id") or item.get("supplierId") or item.get("defaultSupplierId")),
                "supplier_number": clean_text(item.get("supplier_number") or item.get("supplierNumber")),
                "supplier_name": clean_text(item.get("supplier_name") or item.get("supplierName")),
                "tax_no": clean_text(item.get("tax_no") or item.get("taxPayerNo")),
                "purchase_price": num_value(item.get("purchase_price") if "purchase_price" in item else item.get("price")),
                "purchase_qty": num_value(item.get("purchase_qty") if "purchase_qty" in item else item.get("qty"), 1.0) or 1.0,
                "sale_price": num_value(item.get("sale_price")),
                "image_local_path": clean_text(item.get("image_local_path")),
                "image_b64": clean_text(item.get("image_b64")),
                "ka_barcode": clean_text(item.get("ka_barcode")),
                "remark": clean_text(item.get("remark")),
                "color": clean_text(item.get("color")),
                "customs": item.get("customs") if isinstance(item.get("customs"), Mapping) else {},
            }
        )
    return normalized


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return bool(row)


def validate_and_enrich_item(
    conn: sqlite3.Connection,
    base_dir: str,
    account_key: str,
    account: str,
    item: dict,
    seq: int,
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    enriched = dict(item)
    if not enriched["product_name"]:
        errors.append("missing product_name")

    category = get_category_info(base_dir, account_key, enriched["category_id"])
    if not category:
        errors.append("category not found in local category_code_map")
        category = {}
    if not category.get("prefix"):
        errors.append("category missing prefix")
    if not category.get("small_code"):
        errors.append("category missing small_code")
    if not category.get("code_2d"):
        errors.append("category missing code_2d")
    enriched.update(
        {
            "category_name": category.get("path", ""),
            "category_prefix": category.get("prefix", ""),
            "category_small_code": category.get("small_code", ""),
            "category_code_2d": category.get("code_2d", ""),
        }
    )

    supplier = _supplier_from_cache(conn, account, enriched["supplier_number"], enriched["supplier_id"])
    if supplier:
        enriched["supplier_number"] = enriched["supplier_number"] or supplier["number"]
        enriched["supplier_name"] = enriched["supplier_name"] or supplier["name"]
        enriched["supplier_id"] = enriched["supplier_id"] or supplier["id"]
        enriched["tax_no"] = enriched["tax_no"] or supplier["tax_no"]
    if not enriched["supplier_number"] and not enriched["supplier_id"]:
        warnings.append("supplier not selected from local supplier cache")
    if not enriched["tax_no"]:
        errors.append("missing supplier tax_no")

    transformed, transform_error = transform_vendor_code(enriched["tax_no"])
    if transform_error:
        errors.append(f"tax_no transform failed: {transform_error}")
    enriched["transformed_code"] = transformed

    if enriched["purchase_price"] < 0:
        errors.append("purchase_price cannot be negative")
    if not enriched["unit_name"] and not enriched["unit_id"]:
        warnings.append("unit not selected")

    if not errors:
        enriched["seq_preview"] = seq
        enriched["product_number"] = generate_product_number(
            enriched["category_prefix"],
            transformed,
            enriched["category_small_code"],
            seq,
        )
        enriched["ean13"] = generate_ean13(enriched["category_code_2d"], seq)
        if _table_exists(conn, "jdy_products"):
            dup = conn.execute(
                "SELECT 1 FROM jdy_products WHERE account = ? AND product_number = ? LIMIT 1",
                (account, enriched["product_number"]),
            ).fetchone()
            if dup:
                errors.append(f"product_number already exists in local jdy_products: {enriched['product_number']}")
        if _table_exists(conn, "customs_product_master"):
            dup2 = conn.execute(
                "SELECT 1 FROM customs_product_master WHERE product_code = ? LIMIT 1",
                (enriched["product_number"],),
            ).fetchone()
            if dup2:
                errors.append(f"product_number already exists in customs_product_master: {enriched['product_number']}")

    if enriched.get("image_b64"):
        enriched["image_sha256"] = hashlib.sha256(enriched["image_b64"].encode("utf-8")).hexdigest()
    else:
        enriched["image_sha256"] = ""
    enriched["validation"] = {"errors": errors, "warnings": warnings}
    enriched["ready"] = not errors
    return enriched


def build_product_add_payload(item: Mapping[str, Any]) -> dict:
    payload = {
        "productNumber": item.get("product_number") or "",
        "productName": item.get("product_name") or "",
        "barcode": item.get("ean13") or "",
    }
    if item.get("category_id"):
        payload["categoryId"] = item["category_id"]
    if item.get("unit_id"):
        payload["unitId"] = item["unit_id"]
    elif item.get("unit_name"):
        payload["unit"] = item["unit_name"]
    if item.get("spec"):
        payload["spec"] = item["spec"]
    if item.get("remark"):
        payload["remark"] = item["remark"]
    if item.get("supplier_id"):
        payload["defaultSupplierId"] = item["supplier_id"]
    elif item.get("supplier_number"):
        payload["defaultSupplierNumber"] = item["supplier_number"]
    return payload


def build_item_payloads(item: Mapping[str, Any]) -> dict:
    product_add = build_product_add_payload(item)
    image_update = None
    if item.get("image_b64"):
        image_update = {
            "items": [
                {
                    "productNumber": item.get("product_number") or "",
                    "multiImg": [item.get("image_b64")],
                }
            ]
        }
    price_update = None
    if num_value(item.get("purchase_price")) > 0:
        price_update = {
            "items": [
                {
                    "productNumber": item.get("product_number") or "",
                    "elsPurPrice": item.get("purchase_price"),
                }
            ]
        }
    return {
        "product_add": product_add,
        "product_image_update": image_update,
        "product_price_update": price_update,
    }


def build_purchase_order_payload(draft: Mapping[str, Any], items: list[dict]) -> dict | None:
    entries = []
    supplier_number = ""
    for item in items:
        validation = item.get("validation") or {}
        if validation.get("errors"):
            continue
        supplier_number = supplier_number or item.get("supplier_number") or ""
        if num_value(item.get("purchase_price")) > 0:
            entries.append(
                {
                    "productNumber": item.get("product_number"),
                    "qty": num_value(item.get("purchase_qty"), 1.0) or 1.0,
                    "price": num_value(item.get("purchase_price")),
                    "unit": item.get("unit_id") or item.get("unit_name") or "",
                    "location": draft.get("purchase_location") or "",
                }
            )
    if not entries or not supplier_number:
        return None
    return {
        "supplier_number": supplier_number,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "entries": entries,
    }


def preview_payload(conn: sqlite3.Connection, base_dir: str, account_key: str, account: str, items: Any) -> dict:
    normalized = normalize_items(items)
    start_seq = peek_next_seq(conn, account_key)
    enriched = []
    for offset, item in enumerate(normalized):
        row = validate_and_enrich_item(conn, base_dir, account_key, account, item, start_seq + offset)
        row["payloads"] = build_item_payloads(row) if row.get("ready") else {}
        enriched.append(row)
    ready = all(item.get("ready") for item in enriched) and bool(enriched)
    return {
        "success": True,
        "local_first": True,
        "dry_run": True,
        "called_jdy": False,
        "would_call_jdy": False,
        "account_key": account_key,
        "account": account,
        "ready": ready,
        "items": enriched,
        "summary": {
            "total": len(enriched),
            "ready": sum(1 for item in enriched if item.get("ready")),
            "errors": sum(len(item.get("validation", {}).get("errors") or []) for item in enriched),
            "warnings": sum(len(item.get("validation", {}).get("warnings") or []) for item in enriched),
        },
    }


def draft_idempotency_key(account_key: str, created_by: str, items: list[dict]) -> str:
    seed = {
        "account_key": clean_text(account_key),
        "created_by": clean_text(created_by),
        "items": [
            {
                "supplier_number": clean_text(x.get("supplier_number")),
                "category_id": clean_text(x.get("category_id")),
                "product_name": clean_text(x.get("product_name")),
                "spec": clean_text(x.get("spec")),
            }
            for x in (items or [])
        ],
    }
    return payload_hash(seed)[:32]


def create_draft(
    conn: sqlite3.Connection,
    base_dir: str,
    account_key: str,
    account: str,
    title: str,
    items: Any,
    user: Mapping[str, Any],
    source: str = "quick_create",
) -> dict:
    ensure_schema(conn)
    preview = preview_payload(conn, base_dir, account_key, account, items)
    now = now_text()
    username = clean_text((user or {}).get("username"))
    display_name = clean_text((user or {}).get("name"))
    draft_no = "PCD" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4].upper()
    idempotency = payload_hash(
        {
            "draft_no": draft_no,
            "account_key": account_key,
            "created_by": username,
            "items": preview["items"],
        }
    )[:32]
    phash = payload_hash(preview.get("items") or [])
    conn.execute(
        """
        INSERT INTO product_create_draft
        (draft_no, account_key, account, title, status, idempotency_key, payload_hash, source,
         created_by, created_by_name, created_at, updated_at, validated_at, data_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            draft_no,
            account_key,
            account,
            clean_text(title) or "Quick product draft",
            "validated" if preview.get("ready") else "draft",
            idempotency,
            phash,
            source,
            username,
            display_name,
            now,
            now,
            now if preview.get("ready") else "",
            json_dumps({"preview_summary": preview.get("summary")}),
        ),
    )
    draft_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    _replace_draft_items(conn, draft_id, account_key, account, preview["items"], now)
    conn.commit()
    return read_draft(conn, draft_id) or {}


def update_draft(
    conn: sqlite3.Connection,
    base_dir: str,
    draft_id: int,
    account_key: str,
    account: str,
    title: str,
    items: Any,
    user: Mapping[str, Any],
) -> dict:
    ensure_schema(conn)
    draft = read_draft(conn, draft_id)
    if not draft:
        return {"success": False, "error": "draft not found"}
    if draft["status"] in ("submitting", "submitted"):
        return {"success": False, "error": "submitted draft cannot be edited"}
    preview = preview_payload(conn, base_dir, account_key, account, items)
    now = now_text()
    conn.execute(
        """
        UPDATE product_create_draft
        SET account_key=?, account=?, title=?, status=?, payload_hash=?, updated_at=?,
            validated_at=?, last_error='', data_json=?
        WHERE id=?
        """,
        (
            account_key,
            account,
            clean_text(title) or draft.get("title") or "Quick product draft",
            "validated" if preview.get("ready") else "draft",
            payload_hash(preview.get("items") or []),
            now,
            now if preview.get("ready") else "",
            json_dumps({"preview_summary": preview.get("summary"), "updated_by": clean_text((user or {}).get("username"))}),
            draft_id,
        ),
    )
    conn.execute("DELETE FROM product_create_draft_item WHERE draft_id = ?", (draft_id,))
    _replace_draft_items(conn, draft_id, account_key, account, preview["items"], now)
    conn.commit()
    return {"success": True, "draft": read_draft(conn, draft_id)}


def _replace_draft_items(
    conn: sqlite3.Connection,
    draft_id: int,
    account_key: str,
    account: str,
    items: list[dict],
    now: str,
) -> None:
    for item in items:
        payloads = item.get("payloads") or {}
        conn.execute(
            """
            INSERT INTO product_create_draft_item
            (draft_id, line_no, status, account_key, account, product_number, product_name, spec,
             category_id, category_name, category_prefix, category_small_code, category_code_2d,
             unit_id, unit_name, supplier_id, supplier_number, supplier_name, tax_no,
             transformed_code, purchase_price, purchase_qty, sale_price, image_local_path, image_b64,
             image_sha256, ka_barcode, remark, color, seq_preview, ean13, payload_json,
             validation_json, customs_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft_id,
                item["line_no"],
                "ready" if item.get("ready") else "draft",
                account_key,
                account,
                item.get("product_number", ""),
                item.get("product_name", ""),
                item.get("spec", ""),
                item.get("category_id", ""),
                item.get("category_name", ""),
                item.get("category_prefix", ""),
                item.get("category_small_code", ""),
                item.get("category_code_2d", ""),
                item.get("unit_id", ""),
                item.get("unit_name", ""),
                item.get("supplier_id", ""),
                item.get("supplier_number", ""),
                item.get("supplier_name", ""),
                item.get("tax_no", ""),
                item.get("transformed_code", ""),
                item.get("purchase_price", 0),
                item.get("purchase_qty", 1),
                item.get("sale_price", 0),
                item.get("image_local_path", ""),
                item.get("image_b64", ""),
                item.get("image_sha256", ""),
                item.get("ka_barcode", ""),
                item.get("remark", ""),
                item.get("color", ""),
                item.get("seq_preview", 0),
                item.get("ean13", ""),
                json_dumps(payloads),
                json_dumps(item.get("validation")),
                json_dumps(item.get("customs")),
                now,
                now,
            ),
        )


def _draft_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "draft_no": row["draft_no"],
        "account_key": row["account_key"],
        "account": row["account"],
        "title": row["title"],
        "status": row["status"],
        "idempotency_key": row["idempotency_key"],
        "payload_hash": row["payload_hash"],
        "source": row["source"],
        "created_by": row["created_by"],
        "created_by_name": row["created_by_name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "validated_at": row["validated_at"],
        "dry_run_at": row["dry_run_at"],
        "approved_at": row["approved_at"],
        "approved_by": row["approved_by"],
        "submitted_at": row["submitted_at"],
        "submitted_by": row["submitted_by"],
        "last_error": row["last_error"],
        "sync_status": row["sync_status"],
        "jdy_result": json_loads(row["jdy_result_json"]),
        "data": json_loads(row["data_json"]),
    }


def _item_row_to_dict(row: sqlite3.Row, include_image: bool = False) -> dict:
    data = {
        "id": row["id"],
        "draft_id": row["draft_id"],
        "line_no": row["line_no"],
        "status": row["status"],
        "account_key": row["account_key"],
        "account": row["account"],
        "product_number": row["product_number"],
        "product_name": row["product_name"],
        "spec": row["spec"],
        "category_id": row["category_id"],
        "category_name": row["category_name"],
        "category_prefix": row["category_prefix"],
        "category_small_code": row["category_small_code"],
        "category_code_2d": row["category_code_2d"],
        "unit_id": row["unit_id"],
        "unit_name": row["unit_name"],
        "supplier_id": row["supplier_id"],
        "supplier_number": row["supplier_number"],
        "supplier_name": row["supplier_name"],
        "tax_no": row["tax_no"],
        "transformed_code": row["transformed_code"],
        "purchase_price": row["purchase_price"],
        "purchase_qty": row["purchase_qty"],
        "sale_price": row["sale_price"],
        "image_local_path": row["image_local_path"],
        "has_image_b64": bool(row["image_b64"]),
        "image_sha256": row["image_sha256"],
        "ka_barcode": row["ka_barcode"],
        "remark": row["remark"],
        "color": row["color"],
        "seq_preview": row["seq_preview"],
        "seq_reserved": row["seq_reserved"],
        "ean13": row["ean13"],
        "jdy_product_id": row["jdy_product_id"],
        "jdy_error": row["jdy_error"],
        "payloads": json_loads(row["payload_json"]),
        "validation": json_loads(row["validation_json"]),
        "customs": json_loads(row["customs_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_image:
        data["image_b64"] = row["image_b64"]
    return data


def read_draft(conn: sqlite3.Connection, draft_id: int, include_image: bool = False) -> dict | None:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM product_create_draft WHERE id = ?", (draft_id,)).fetchone()
    if not row:
        return None
    draft = _draft_row_to_dict(row)
    item_rows = conn.execute(
        "SELECT * FROM product_create_draft_item WHERE draft_id = ? ORDER BY line_no, id",
        (draft_id,),
    ).fetchall()
    draft["items"] = [_item_row_to_dict(item, include_image=include_image) for item in item_rows]
    return draft


def list_drafts(conn: sqlite3.Connection, account_key: str = "all", status: str = "", limit: int = 50) -> list[dict]:
    ensure_schema(conn)
    clauses = []
    params: list[Any] = []
    if account_key and account_key != "all":
        clauses.append("account_key = ?")
        params.append(account_key)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM product_create_draft {where} ORDER BY updated_at DESC, id DESC LIMIT ?",
        [*params, max(1, min(int(limit or 50), 200))],
    ).fetchall()
    result = []
    for row in rows:
        item = _draft_row_to_dict(row)
        counts = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) AS ready
            FROM product_create_draft_item
            WHERE draft_id = ?
            """,
            (item["id"],),
        ).fetchone()
        item["item_count"] = int(counts["total"] or 0)
        item["ready_count"] = int(counts["ready"] or 0)
        result.append(item)
    return result


def delete_draft(conn: sqlite3.Connection, draft_id: int) -> dict:
    ensure_schema(conn)
    draft = read_draft(conn, draft_id)
    if not draft:
        return {"success": False, "error": "draft not found"}
    if draft["status"] in ("submitting", "submitted"):
        return {"success": False, "error": "submitted draft cannot be cancelled"}
    now = now_text()
    conn.execute(
        "UPDATE product_create_draft SET status = 'cancelled', updated_at = ?, last_error = '' WHERE id = ?",
        (now, draft_id),
    )
    conn.commit()
    return {"success": True, "draft": read_draft(conn, draft_id)}


def dry_run_draft(conn: sqlite3.Connection, draft_id: int) -> dict:
    ensure_schema(conn)
    draft = read_draft(conn, draft_id)
    if not draft:
        return {"success": False, "error": "draft not found"}
    items = []
    all_ready = True
    for item in draft["items"]:
        errors = item.get("validation", {}).get("errors") or []
        if errors:
            all_ready = False
        payloads = item.get("payloads") or {}
        items.append(
            {
                "item_id": item["id"],
                "line_no": item["line_no"],
                "product_number": item["product_number"],
                "product_name": item["product_name"],
                "ready": not bool(errors),
                "validation": item.get("validation"),
                "payloads": payloads,
                "payload_hash": payload_hash(payloads),
                "would_write_jdy": {
                    "product_add": True,
                    "product_image_update": bool(payloads.get("product_image_update")),
                    "product_price_update": bool(payloads.get("product_price_update")),
                    "purchase_order_add": False,
                },
            }
        )
    purchase_payload = build_purchase_order_payload(draft, draft["items"])
    now = now_text()
    next_status = draft["status"]
    if draft["status"] not in ("submitting", "submitted", "cancelled"):
        next_status = "dry_run" if all_ready else "draft"
    conn.execute(
        "UPDATE product_create_draft SET status = ?, dry_run_at = ?, updated_at = ? WHERE id = ?",
        (next_status, now, now, draft_id),
    )
    conn.commit()
    return {
        "success": True,
        "local_first": True,
        "dry_run": True,
        "called_jdy": False,
        "would_call_jdy": True,
        "ready": all_ready,
        "draft_id": draft_id,
        "draft_payload_hash": draft.get("payload_hash") or "",
        "items": items,
        "purchase_order_payload": purchase_payload,
        "write_actions": list(WRITE_ACTIONS),
        "jdy_write_points": {
            "product_add": "/jdyscm/product/add",
            "purchase_order_add": "/jdyscm/purchaseOrder/add",
            "product_image_update": "/jdyscm/product/update",
            "product_price_update": "/jdyscm/product/update",
        },
    }


class MockJdySubmitter:
    called_real_jdy = False

    def create_product(self, account_key: str, payload: dict) -> dict:
        product_number = clean_text(payload.get("productNumber"))
        return {"ok": True, "mock": True, "data": {"id": f"mock-{product_number}", "productNumber": product_number}}

    def update_product_image(self, account_key: str, product_id: str, product_number: str, image_b64: str) -> dict:
        return {"ok": True, "mock": True, "skipped": not bool(image_b64)}

    def update_product_price(self, account_key: str, product_number: str, price: float) -> dict:
        return {"ok": True, "mock": True, "skipped": not bool(price and price > 0)}

    def create_purchase_order(self, account_key: str, payload: dict) -> dict:
        return {"ok": True, "mock": True, "order_number": "MOCK-PO-" + uuid.uuid4().hex[:8].upper(), "data": payload}


class RealJdySubmitter:
    called_real_jdy = True

    def __init__(self, cfg_path: str | None = None):
        from jdy_register.jdy_cache import JDYCache

        self.cache = JDYCache(cfg_path=cfg_path)

    def create_product(self, account_key: str, payload: dict) -> dict:
        return self.cache.create_product(account_key, payload)

    def update_product_image(self, account_key: str, product_id: str, product_number: str, image_b64: str) -> dict:
        return self.cache.update_product_image(account_key, product_id, product_number, image_b64)

    def update_product_price(self, account_key: str, product_number: str, price: float) -> dict:
        return self.cache.update_product_price(account_key, product_number, price)

    def create_purchase_order(self, account_key: str, payload: dict) -> dict:
        return self.cache.create_purchase_order(account_key, payload)


def _insert_submit_log(
    conn: sqlite3.Connection,
    draft_id: int,
    item_id: int,
    action: str,
    endpoint: str,
    status: str,
    idempotency_key: str,
    payload: dict,
    response: dict | None = None,
    error: str = "",
    user: Mapping[str, Any] | None = None,
    called_jdy: bool = False,
    dry_run: bool = True,
) -> None:
    now = now_text()
    phash = payload_hash(payload)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO product_create_submit_log
        (draft_id, item_id, action, endpoint, status, idempotency_key, payload_hash, payload_json,
         response_json, error, attempts, called_jdy, dry_run, actor, actor_role, created_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            draft_id,
            item_id or 0,
            action,
            endpoint,
            status,
            idempotency_key,
            phash,
            json_dumps(payload),
            json_dumps(response or {}),
            clean_text(error),
            1,
            1 if called_jdy else 0,
            1 if dry_run else 0,
            clean_text((user or {}).get("username")),
            clean_text((user or {}).get("role")),
            now,
            now if status in ("success", "failed", "mock_success", "skipped") else "",
        ),
    )
    if cur.rowcount:
        return
    conn.execute(
        """
        UPDATE product_create_submit_log
        SET status = ?, endpoint = ?, response_json = ?, error = ?, attempts = attempts + 1,
            called_jdy = CASE WHEN ? THEN 1 ELSE called_jdy END,
            dry_run = ?, actor = ?, actor_role = ?, finished_at = ?
        WHERE idempotency_key = ? AND action = ? AND payload_hash = ?
          AND status NOT IN ('success', 'mock_success', 'skipped')
        """,
        (
            status,
            endpoint,
            json_dumps(response or {}),
            clean_text(error),
            1 if called_jdy else 0,
            1 if dry_run else 0,
            clean_text((user or {}).get("username")),
            clean_text((user or {}).get("role")),
            now if status in ("success", "failed", "mock_success", "skipped") else "",
            idempotency_key,
            action,
            phash,
        ),
    )


def _submit_log_existing(conn: sqlite3.Connection, idempotency_key: str, action: str, payload: dict) -> dict | None:
    phash = payload_hash(payload)
    row = conn.execute(
        """
        SELECT status, response_json, error
        FROM product_create_submit_log
        WHERE idempotency_key = ? AND action = ? AND payload_hash = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (idempotency_key, action, phash),
    ).fetchone()
    if not row:
        return None
    return {
        "status": row["status"],
        "response": json_loads(row["response_json"]),
        "error": row["error"] or "",
        "idempotent_replay": True,
    }


def _submit_log_success(entry: dict | None) -> bool:
    return bool(entry and entry.get("status") in ("success", "mock_success", "skipped"))


def _safe_response_payload(response: dict) -> dict:
    if not isinstance(response, dict):
        return {"raw": str(response)}
    safe = dict(response)
    for key in ("token", "accessToken", "secret", "appSecret"):
        if key in safe:
            safe[key] = "***"
    return safe


def _reserve_sequences_for_draft(conn: sqlite3.Connection, draft: dict) -> list[dict]:
    reserved = []
    for item in draft["items"]:
        if item.get("seq_reserved"):
            reserved.append(item)
            continue
        seq = reserve_next_seq(conn, draft["account_key"])
        product_number = generate_product_number(
            item["category_prefix"],
            item["transformed_code"],
            item["category_small_code"],
            seq,
        )
        ean13 = generate_ean13(item["category_code_2d"], seq)
        updated = dict(item, seq_reserved=seq, product_number=product_number, ean13=ean13)
        payloads = build_item_payloads(updated)
        conn.execute(
            """
            UPDATE product_create_draft_item
            SET seq_reserved=?, product_number=?, ean13=?, payload_json=?, updated_at=?
            WHERE id=?
            """,
            (seq, product_number, ean13, json_dumps(payloads), now_text(), item["id"]),
        )
        updated["payloads"] = payloads
        reserved.append(updated)
    return reserved


def submit_draft(
    conn: sqlite3.Connection,
    draft_id: int,
    user: Mapping[str, Any],
    confirm_code: str,
    dry_run_confirmed: bool = False,
    submitter: Any = None,
    mock: bool = True,
    confirm_real_jdy: str = "",
) -> dict:
    ensure_schema(conn)
    if confirm_code != CONFIRM_SUBMIT_CODE:
        return {"success": False, "error": f"missing confirm token {CONFIRM_SUBMIT_CODE}"}
    if not dry_run_confirmed:
        return {"success": False, "error": "dry_run_confirmed is required before submit"}
    if not mock and confirm_real_jdy != CONFIRM_REAL_JDY_CODE:
        return {"success": False, "error": f"missing real JDY confirm token {CONFIRM_REAL_JDY_CODE}"}

    draft = read_draft(conn, draft_id, include_image=True)
    if not draft:
        return {"success": False, "error": "draft not found"}
    if draft["status"] == "submitted":
        return {"success": True, "already_submitted": True, "called_jdy": False, "draft": draft}
    if draft["status"] == "submitting":
        return {"success": False, "error": "draft is already submitting"}
    dry = dry_run_draft(conn, draft_id)
    if not dry.get("ready"):
        return {"success": False, "error": "draft validation failed", "dry_run": dry}

    now = now_text()
    lock_token = uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        """
        UPDATE product_create_draft
        SET status='submitting', locked_at=?, locked_by=?, lock_token=?, approved_at=?, approved_by=?,
            updated_at=?, submitted_by=?, submit_confirm_code=?
        WHERE id=? AND status <> 'submitting'
        """,
        (
            now,
            clean_text((user or {}).get("username")),
            lock_token,
            now,
            clean_text((user or {}).get("username")),
            now,
            clean_text((user or {}).get("username")),
            confirm_code,
            draft_id,
        ),
    )
    if cur.rowcount != 1:
        conn.commit()
        return {"success": False, "error": "failed to acquire draft lock"}
    items = _reserve_sequences_for_draft(conn, draft)
    conn.commit()

    submitter = submitter or MockJdySubmitter()
    called_jdy = not mock
    results = []
    all_ok = True
    for item in items:
        item_ok = True
        action_results: dict[str, Any] = {}
        item_payloads = item.get("payloads") or {}
        item_key = f"{draft['idempotency_key']}:{item['id']}:{item['product_number']}"

        product_payload = item_payloads.get("product_add") or {}
        existing = _submit_log_existing(conn, item_key, "product_add", product_payload)
        if _submit_log_success(existing):
            product_result = existing["response"]
            product_result.setdefault("ok", True)
            product_result["idempotent_replay"] = True
        else:
            product_result = submitter.create_product(item["account_key"], product_payload)
        product_ok = bool(product_result.get("ok"))
        item_ok = item_ok and product_ok
        action_results["product_add"] = product_result
        _insert_submit_log(
            conn,
            draft_id,
            item["id"],
            "product_add",
            "/jdyscm/product/add",
            "mock_success" if mock and product_ok else ("success" if product_ok else "failed"),
            item_key,
            product_payload,
            _safe_response_payload(product_result),
            "" if product_ok else clean_text(product_result.get("msg") or product_result.get("error") or "product_add failed"),
            user,
            called_jdy=called_jdy,
            dry_run=mock,
        )
        conn.commit()
        if not product_ok:
            all_ok = False
            conn.execute(
                "UPDATE product_create_draft_item SET status='failed', jdy_error=?, updated_at=? WHERE id=?",
                (product_result.get("msg") or product_result.get("error") or "product_add failed", now_text(), item["id"]),
            )
            conn.commit()
            results.append({"item_id": item["id"], "ok": False, **action_results})
            continue

        jdy_data = product_result.get("data") or {}
        jdy_product_id = clean_text(jdy_data.get("id") or jdy_data.get("productId") or f"mock-{item['product_number']}")

        image_payload = item_payloads.get("product_image_update")
        if image_payload:
            image_b64 = ((image_payload.get("items") or [{}])[0].get("multiImg") or [""])[0]
            existing = _submit_log_existing(conn, item_key, "product_image_update", image_payload)
            if _submit_log_success(existing):
                image_result = existing["response"]
                image_result.setdefault("ok", True)
                image_result["idempotent_replay"] = True
            else:
                image_result = submitter.update_product_image(item["account_key"], jdy_product_id, item["product_number"], image_b64)
            image_ok = bool(image_result.get("ok"))
            item_ok = item_ok and image_ok
            action_results["product_image_update"] = image_result
            _insert_submit_log(
                conn,
                draft_id,
                item["id"],
                "product_image_update",
                "/jdyscm/product/update",
                "mock_success" if mock and image_ok else ("success" if image_ok else "failed"),
                item_key,
                image_payload,
                _safe_response_payload(image_result),
                "" if image_ok else clean_text(image_result.get("msg") or image_result.get("error") or "image update failed"),
                user,
                called_jdy=called_jdy,
                dry_run=mock,
            )
            conn.commit()

        price_payload = item_payloads.get("product_price_update")
        if price_payload:
            existing = _submit_log_existing(conn, item_key, "product_price_update", price_payload)
            if _submit_log_success(existing):
                price_result = existing["response"]
                price_result.setdefault("ok", True)
                price_result["idempotent_replay"] = True
            else:
                price_result = submitter.update_product_price(item["account_key"], item["product_number"], item.get("purchase_price") or 0)
            price_ok = bool(price_result.get("ok"))
            item_ok = item_ok and price_ok
            action_results["product_price_update"] = price_result
            _insert_submit_log(
                conn,
                draft_id,
                item["id"],
                "product_price_update",
                "/jdyscm/product/update",
                "mock_success" if mock and price_ok else ("success" if price_ok else "failed"),
                item_key,
                price_payload,
                _safe_response_payload(price_result),
                "" if price_ok else clean_text(price_result.get("msg") or price_result.get("error") or "price update failed"),
                user,
                called_jdy=called_jdy,
                dry_run=mock,
            )
            conn.commit()

        all_ok = all_ok and item_ok
        conn.execute(
            """
            UPDATE product_create_draft_item
            SET status=?, jdy_product_id=?, jdy_error=?, updated_at=?
            WHERE id=?
            """,
            ("submitted" if item_ok else "failed", jdy_product_id, "" if item_ok else "optional JDY update failed", now_text(), item["id"]),
        )
        conn.commit()
        results.append({"item_id": item["id"], "ok": item_ok, **action_results})

    refreshed_draft = read_draft(conn, draft_id, include_image=True) or draft
    purchase_payload = build_purchase_order_payload(refreshed_draft, refreshed_draft["items"])
    purchase_result = None
    if all_ok and purchase_payload:
        existing = _submit_log_existing(conn, refreshed_draft["idempotency_key"], "purchase_order_add", purchase_payload)
        if _submit_log_success(existing):
            purchase_result = existing["response"]
            purchase_result.setdefault("ok", True)
            purchase_result["idempotent_replay"] = True
        else:
            purchase_result = submitter.create_purchase_order(refreshed_draft["account_key"], purchase_payload)
        po_ok = bool(purchase_result.get("ok"))
        _insert_submit_log(
            conn,
            draft_id,
            0,
            "purchase_order_add",
            "/jdyscm/purchaseOrder/add",
            "mock_success" if mock and po_ok else ("success" if po_ok else "failed"),
            refreshed_draft["idempotency_key"],
            purchase_payload,
            _safe_response_payload(purchase_result),
            "" if po_ok else clean_text(purchase_result.get("msg") or purchase_result.get("error") or "purchase order failed"),
            user,
            called_jdy=called_jdy,
            dry_run=mock,
        )
        conn.commit()
        all_ok = all_ok and po_ok

    final_status = "submitted" if all_ok else "failed"
    conn.execute(
        """
        UPDATE product_create_draft
        SET status=?, submitted_at=?, updated_at=?, locked_at='', locked_by='', lock_token='',
            last_error=?, jdy_result_json=?, sync_status=?
        WHERE id=?
        """,
        (
            final_status,
            now_text() if all_ok else "",
            now_text(),
            "" if all_ok else "one or more JDY submit actions failed",
            json_dumps({"mock": mock, "items": results, "purchase_order": purchase_result, "needs_jdy_products_sync": all_ok}),
            "needs_jdy_products_sync" if all_ok else "not_needed",
            draft_id,
        ),
    )
    conn.commit()
    return {
        "success": all_ok,
        "mock": mock,
        "called_jdy": called_jdy,
        "would_call_jdy": True,
        "dry_run": mock,
        "draft": read_draft(conn, draft_id),
        "results": results,
        "purchase_order": purchase_result,
        "needs_jdy_products_sync": all_ok,
    }


def customs_item_from_draft_item(item: Mapping[str, Any]) -> dict:
    customs = dict(item.get("customs") or {})
    return {
        "account": item.get("account") or "",
        "product_code": item.get("product_number") or "",
        "product_name": item.get("product_name") or "",
        "category_name": item.get("category_name") or "",
        "spec": item.get("spec") or "",
        "unit": item.get("unit_name") or item.get("unit_id") or "",
        "barcode": item.get("ean13") or "",
        "jdy_product_id": item.get("jdy_product_id") or "",
        "customs_name_cn": clean_text(customs.get("customs_name_cn") or customs.get("name_cn")),
        "customs_name_en": clean_text(customs.get("customs_name_en") or customs.get("name_en")),
        "hs_code": clean_text(customs.get("hs_code")),
        "customs_unit": clean_text(customs.get("customs_unit") or item.get("unit_name")),
        "origin": clean_text(customs.get("origin") or "CHINA"),
        "material": clean_text(customs.get("material")),
        "usage": clean_text(customs.get("usage")),
        "tax_refund_rate": num_value(customs.get("tax_refund_rate")),
        "is_tax_refund": clean_text(customs.get("is_tax_refund")),
        "gross_weight_per_pkg": num_value(customs.get("gross_weight_per_pkg")),
        "net_weight_per_pkg": num_value(customs.get("net_weight_per_pkg")),
        "carton_length": num_value(customs.get("carton_length")),
        "carton_width": num_value(customs.get("carton_width")),
        "carton_height": num_value(customs.get("carton_height")),
        "carton_volume": num_value(customs.get("carton_volume")),
        "pcs_per_pkg": num_value(customs.get("pcs_per_pkg")),
        "supplier_number": item.get("supplier_number") or "",
        "tax_code": clean_text(customs.get("tax_code")),
        "tax_category_short_name": clean_text(customs.get("tax_category_short_name")),
        "source": "quick_create",
    }
