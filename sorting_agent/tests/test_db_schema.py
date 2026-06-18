import unittest
from unittest.mock import Mock

from core.db import ensure_runtime_schema


class RuntimeSchemaTests(unittest.TestCase):
    def test_queue_sequence_migration_is_committed(self):
        conn = Mock()
        cursor = conn.cursor.return_value

        ensure_runtime_schema(conn)

        sql = cursor.execute.call_args.args[0]
        self.assertIn("COL_LENGTH('sorting_rules', 'queue_seq')", sql)
        self.assertIn("ADD queue_seq INT NOT NULL", sql)
        conn.commit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
