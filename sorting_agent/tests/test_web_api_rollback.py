import unittest
from unittest.mock import Mock, call, patch

from api.web_api import app, rollback_batch


class RollbackPickingTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()

    def test_preserves_rules_and_resets_picking_progress(self):
        with (
            app.test_request_context(),
            patch("api.web_api.get_db_conn", return_value=self.db),
            patch("api.web_api.qval", side_effect=[9, 0, 0]),
            patch("api.web_api.execute") as execute,
        ):
            response = rollback_batch("BATCH001")

        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.db.commit.assert_called_once_with()
        self.db.rollback.assert_not_called()

        sql_calls = [args[1] for args, _ in execute.call_args_list]
        self.assertTrue(any(
            "UPDATE sorting_rules SET status=1" in sql
            for sql in sql_calls
        ))
        self.assertFalse(any(
            "DELETE FROM sorting_rules" in sql
            for sql in sql_calls
        ))
        self.assertTrue(any(
            "DELETE FROM scan_events" in sql
            for sql in sql_calls
        ))
        self.assertIn(
            call(
                self.db,
                "UPDATE pick_progress SET anum=0, updated_at=GETDATE() "
                "WHERE batchno=?",
                ("BATCH001",),
            ),
            execute.call_args_list,
        )

    def test_allows_rollback_after_real_scan_and_clears_events(self):
        with (
            app.test_request_context(),
            patch("api.web_api.get_db_conn", return_value=self.db),
            patch("api.web_api.qval", side_effect=[9, 9, 9]),
            patch("api.web_api.execute") as execute,
        ):
            response = rollback_batch("BATCH001")

        self.assertTrue(response.get_json()["ok"])
        self.assertIn("已清除本地落包记录 9 条", response.get_json()["msg"])
        sql_calls = [args[1] for args, _ in execute.call_args_list]
        self.assertTrue(any(
            "status IN (2,3,4)" in sql
            for sql in sql_calls
        ))
        self.assertIn(
            call(
                self.db,
                "DELETE FROM scan_events WHERE batchno=?",
                ("BATCH001",),
            ),
            execute.call_args_list,
        )
        self.db.commit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
