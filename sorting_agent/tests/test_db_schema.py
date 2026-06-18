import unittest
from unittest.mock import Mock

from core.db import ensure_runtime_schema


class RuntimeSchemaTests(unittest.TestCase):
    def test_runtime_migrations_are_committed(self):
        conn = Mock()
        cursor = conn.cursor.return_value

        ensure_runtime_schema(conn)

        sql = "\n".join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn("COL_LENGTH('sorting_rules', 'queue_seq')", sql)
        self.assertIn("ADD queue_seq INT NOT NULL", sql)
        self.assertIn("COL_LENGTH('sorting_rules', 'unit')", sql)
        self.assertIn("ADD unit NVARCHAR(50) NULL", sql)
        conn.commit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
