from pathlib import Path

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


if __name__ == "__main__":
    test_production_db_requires_allow_flag()
    test_production_db_requires_confirm_token()
    test_production_db_rejects_wrong_confirm_token()
    test_production_db_allows_explicit_confirm_token()
    test_non_production_db_does_not_require_confirm_token()
    print("inventory snapshot write guard tests passed")
