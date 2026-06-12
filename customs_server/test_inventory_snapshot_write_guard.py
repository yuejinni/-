from pathlib import Path
import sqlite3

import tools.sync_inventory_snapshots as sync_inventory_snapshots


def expect_refused(fn):
    try:
        fn()
    except RuntimeError as exc:
        return str(exc)
    raise AssertionError("expected RuntimeError")


def test_production_db_requires_allow_flag():
    message = expect_refused(
        lambda: sync_inventory_snapshots.validate_write_target(
            sync_inventory_snapshots.DEFAULT_PROD_DB,
            allow_production_db=False,
            confirm="",
        )
    )
    assert "--allow-production-db" in message


def test_production_db_requires_confirm_token():
    message = expect_refused(
        lambda: sync_inventory_snapshots.validate_write_target(
            sync_inventory_snapshots.DEFAULT_PROD_DB,
            allow_production_db=True,
            confirm="",
        )
    )
    assert "--confirm WRITE_INVENTORY_SNAPSHOT" in message


def test_production_db_rejects_wrong_confirm_token():
    message = expect_refused(
        lambda: sync_inventory_snapshots.validate_write_target(
            sync_inventory_snapshots.DEFAULT_PROD_DB,
            allow_production_db=True,
            confirm="write_inventory_snapshot",
        )
    )
    assert "--confirm WRITE_INVENTORY_SNAPSHOT" in message


def test_production_db_allows_explicit_confirm_token():
    sync_inventory_snapshots.validate_write_target(
        sync_inventory_snapshots.DEFAULT_PROD_DB,
        allow_production_db=True,
        confirm=sync_inventory_snapshots.PRODUCTION_CONFIRM_TOKEN,
    )


def test_non_production_db_does_not_require_confirm_token():
    sync_inventory_snapshots.validate_write_target(Path(r"G:\QihangJDY\repo\_tmp_inventory_snapshot.sqlite3"))


def test_write_snapshots_is_idempotent_for_same_snapshot_key(tmp_path):
    db_path = tmp_path / "snapshots.sqlite3"
    rows = [
        {
            "product_number": "H.78669H-9",
            "normalized_product_number": "78669H-9",
            "barcode": "260114H036001",
            "product_name": "product",
            "warehouse_name": "厂家订单",
            "quantity": 128,
            "unit": "DZ",
            "raw": {"qty": 128},
        }
    ]

    first = sync_inventory_snapshots.write_snapshots(db_path, "祺航饰品", rows, "2026-06-12 14:00:00")
    rows[0]["quantity"] = 130
    second = sync_inventory_snapshots.write_snapshots(db_path, "祺航饰品", rows, "2026-06-12 14:00:00")

    conn = sqlite3.connect(str(db_path))
    try:
        count, qty = conn.execute("SELECT COUNT(*), SUM(quantity) FROM inventory_snapshots").fetchone()
    finally:
        conn.close()
    assert first == 1
    assert second == 1
    assert count == 1
    assert qty == 130


if __name__ == "__main__":
    test_production_db_requires_allow_flag()
    test_production_db_requires_confirm_token()
    test_production_db_rejects_wrong_confirm_token()
    test_production_db_allows_explicit_confirm_token()
    test_non_production_db_does_not_require_confirm_token()
    test_write_snapshots_is_idempotent_for_same_snapshot_key(Path("_tmp_inventory_snapshot_idempotent"))
    print("inventory snapshot write guard tests passed")
