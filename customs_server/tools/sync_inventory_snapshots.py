"""Preview or cache JDY inventory snapshots.

The tool only uses JDY read-only inventory APIs. It refuses to write the default
production SQLite path; pass a copied temporary SQLite path with --write --db.
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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


TOOL_DIR = Path(__file__).resolve().parent
CUSTOMS_DIR = TOOL_DIR.parent
REPO_DIR = CUSTOMS_DIR.parent
if str(CUSTOMS_DIR) not in sys.path:
    sys.path.insert(0, str(CUSTOMS_DIR))

from jdy_api import JDYClient


DEFAULT_PROD_DB = Path(r"G:\祺航本地项目运行\服务端\_sales_cache\sales_cache.sqlite3")
DEFAULT_PROD_CONFIG = Path(r"G:\祺航本地项目运行\服务端\ai_config.json")
IMPORTANT_WAREHOUSE_TERMS = ("厂家订单", "工厂订单", "工厂", "在途仓库", "在途", "新大仓库", "新大", "金华")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value if value is not None else "").strip().replace(",", "")
        return float(text) if text else default
    except Exception:
        return default


def _without_alpha_prefix(code: str) -> str:
    return re.sub(r"^[A-Za-z]\.", "", str(code or "").strip())


def _first(row: Dict[str, Any], keys: Sequence[str], default: Any = "") -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _config_candidates(explicit: str = "") -> List[Path]:
    candidates = []
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


def _load_config(explicit: str = "") -> Tuple[Dict[str, Any], Path]:
    for path in _config_candidates(explicit):
        if path.exists():
            with path.open("r", encoding="utf-8-sig") as f:
                return json.load(f), path
    raise FileNotFoundError("ai_config.json not found")


def _init_jdy_client(cfg: Dict[str, Any], account: str) -> Tuple[JDYClient, str]:
    account = str(account or "").strip()
    name1 = str(cfg.get("jdy_name") or "祺航饰品")
    name2 = str(cfg.get("jdy2_name") or "祺航箱包")
    use_second = bool(account and (account == name2 or name2 in account or ("箱包" in account and "箱包" in name2)))
    prefix = "jdy2_" if use_second else "jdy_"
    label = name2 if use_second else name1
    missing = [
        prefix + key
        for key in ("client_id", "app_key", "app_secret", "db_id")
        if not str(cfg.get(prefix + key) or "").strip()
    ]
    if missing:
        raise RuntimeError("missing JDY config keys: " + ", ".join(missing))
    return (
        JDYClient(
            cfg.get(prefix + "client_id"),
            cfg.get(prefix + "app_key"),
            cfg.get(prefix + "app_secret"),
            cfg.get(prefix + "db_id"),
            cfg.get(prefix + "domain", ""),
            cfg.get(prefix + "app_signature", ""),
            cfg.get(prefix + "client_secret", ""),
        ),
        label,
    )


def _jdy_inventory(client: JDYClient, product: str, verbose: bool) -> Dict[str, Any]:
    if verbose:
        return client.get_inventory_by_product(product, page_size=100)
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        return client.get_inventory_by_product(product, page_size=100)


def _inventory_rows(client: JDYClient, product: str, barcode: str, verbose: bool) -> List[Dict[str, Any]]:
    codes = [product, _without_alpha_prefix(product)]
    rows: List[Dict[str, Any]] = []
    seen = set()
    for code in [c for c in dict.fromkeys(codes) if c]:
        result = _jdy_inventory(client, code, verbose)
        for row in result.get("list") or []:
            if not isinstance(row, dict):
                continue
            product_number = str(_first(row, ["productNumber", "number"], "")).strip()
            row_barcode = str(_first(row, ["barCode", "barcode"], "")).strip()
            if product_number and product_number not in codes and _without_alpha_prefix(product_number) not in codes and row_barcode != barcode:
                continue
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            warehouse = str(_first(row, ["locationName", "warehouseName", "stockName"], "")).strip()
            rows.append(
                {
                    "product_number": product_number or product,
                    "normalized_product_number": _without_alpha_prefix(product_number or product),
                    "barcode": row_barcode or barcode,
                    "product_name": _first(row, ["productName", "name"], ""),
                    "warehouse_name": warehouse,
                    "quantity": _num(_first(row, ["qty", "quantity", "stockQty", "inventoryQty", "availableQty", "baseQty"], 0)),
                    "unit": _first(row, ["unitName", "unit", "baseUnitName"], ""),
                    "raw": row,
                    "important": any(term in warehouse for term in IMPORTANT_WAREHOUSE_TERMS),
                }
            )
    return rows


def ensure_inventory_snapshot_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory_snapshots (
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
    for name, cols in [
        ("idx_inventory_snapshots_account_product", "account, product_number"),
        ("idx_inventory_snapshots_account_normalized_product", "account, normalized_product_number"),
        ("idx_inventory_snapshots_account_barcode", "account, barcode"),
        ("idx_inventory_snapshots_account_warehouse", "account, warehouse_name"),
        ("idx_inventory_snapshots_account_snapshot", "account, snapshot_at"),
    ]:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON inventory_snapshots ({cols})")


def _refuse_production_write(path: Path) -> None:
    target = os.path.normcase(str(path.resolve()))
    prod = os.path.normcase(str(DEFAULT_PROD_DB.resolve()))
    if target == prod:
        raise RuntimeError("refusing to write production SQLite; copy it to a temporary path and pass --db")


def write_snapshots(db_path: Path, account: str, rows: Sequence[Dict[str, Any]], snapshot_at: str) -> int:
    _refuse_production_write(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        ensure_inventory_snapshot_schema(conn)
        count = 0
        for row in rows:
            conn.execute(
                """
                INSERT INTO inventory_snapshots (
                    account, product_number, normalized_product_number, barcode, product_name,
                    warehouse_name, quantity, unit, snapshot_at, source, data_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'jdy_inventory', ?, CURRENT_TIMESTAMP)
                """,
                (
                    account,
                    row.get("product_number") or "",
                    row.get("normalized_product_number") or "",
                    row.get("barcode") or "",
                    row.get("product_name") or "",
                    row.get("warehouse_name") or "",
                    _num(row.get("quantity")),
                    row.get("unit") or "",
                    snapshot_at,
                    json.dumps(row.get("raw") or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()


def print_preview(account: str, rows: Sequence[Dict[str, Any]], snapshot_at: str) -> None:
    print("account:", account)
    print("snapshot_at:", snapshot_at)
    print("rows:", len(rows))
    print()
    print("仓库数量:")
    totals: Dict[str, float] = {}
    for row in rows:
        wh = str(row.get("warehouse_name") or "")
        totals[wh] = totals.get(wh, 0.0) + _num(row.get("quantity"))
    for wh, qty in sorted(totals.items()):
        marker = "*" if any(term in wh for term in IMPORTANT_WAREHOUSE_TERMS) else " "
        print(f"{marker} {wh}: {qty:g}")
    print()
    print("明细:")
    for row in rows:
        marker = "*" if row.get("important") else " "
        print(
            f"{marker} {row.get('product_number')} | {row.get('warehouse_name')} | "
            f"{_num(row.get('quantity')):g} | {row.get('unit') or ''}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读拉取 JDY 库存并可写入临时 inventory_snapshots。")
    parser.add_argument("--account", required=True, help="账套，例如 祺航饰品")
    parser.add_argument("--product", required=True, help="商品编号，例如 H.78669H-9")
    parser.add_argument("--barcode", default="", help="条码")
    parser.add_argument("--config", default="", help="ai_config.json 路径")
    parser.add_argument("--db", default="", help="写入的临时 SQLite 路径；--write 时必填")
    parser.add_argument("--preview", action="store_true", help="只预览，不写库")
    parser.add_argument("--write", action="store_true", help="写入指定临时 SQLite")
    parser.add_argument("--snapshot-at", default="", help="快照时间，默认当前时间")
    parser.add_argument("--verbose-jdy-logs", action="store_true", help="显示 JDY 客户端请求日志")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.write == args.preview:
        raise SystemExit("必须且只能指定 --preview 或 --write")
    if args.write and not args.db:
        raise SystemExit("--write 必须指定 --db <临时SQLite路径>")

    cfg, cfg_path = _load_config(args.config)
    client, account_label = _init_jdy_client(cfg, args.account)
    snapshot_at = args.snapshot_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = _inventory_rows(client, args.product, args.barcode, args.verbose_jdy_logs)
    print("config:", cfg_path)
    print("jdy_account:", account_label)
    print_preview(args.account, rows, snapshot_at)
    if args.write:
        written = write_snapshots(Path(args.db), args.account, rows, snapshot_at)
        print()
        print("written:", written)
        print("db:", args.db)
    else:
        print()
        print("preview_only: true")
    print("jdy_write_called: false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
