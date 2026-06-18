"""
core/db.py — pyodbc 连接工厂 + 查询辅助函数

⚠️ pyodbc Connection 不是线程安全的。
   每个线程/请求启动时独立调用 get_db_conn()，不要跨线程共享同一个 connection 对象。
"""
import json
import os
import pyodbc

_config_path = os.path.join(os.path.dirname(__file__), '..', 'config.json')

def _load_conn_str() -> str:
    with open(_config_path, encoding='utf-8') as f:
        return json.load(f)['db_conn_str']


def get_db_conn() -> pyodbc.Connection:
    """
    返回新的 pyodbc 连接（autocommit=False）。
    各后台线程启动时各自调用一次，线程生命周期内复用，不每次迭代新建。
    """
    conn_str = _load_conn_str()
    conn = pyodbc.connect(conn_str, autocommit=False)
    conn.setdecoding(pyodbc.SQL_CHAR, encoding='utf-8')
    conn.setencoding(encoding='utf-8')
    return conn


def ensure_runtime_schema(conn) -> None:
    """补齐可向后兼容的运行时字段；语句必须保持幂等。"""
    cur = conn.cursor()
    cur.execute("""
        IF COL_LENGTH('sorting_rules', 'queue_seq') IS NULL
        BEGIN
            ALTER TABLE sorting_rules
            ADD queue_seq INT NOT NULL
                CONSTRAINT DF_sorting_rules_queue_seq DEFAULT(0) WITH VALUES
        END
    """)
    cur.execute("""
        IF COL_LENGTH('sorting_rules', 'unit') IS NULL
        BEGIN
            ALTER TABLE sorting_rules ADD unit NVARCHAR(50) NULL
        END
    """)
    conn.commit()


def qone(conn, sql: str, params: tuple = ()) -> dict | None:
    """查询首行，返回 dict 或 None。"""
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    return dict(zip([c[0] for c in cur.description], row)) if row else None


def qval(conn, sql: str, params: tuple = ()):
    """查询首行首列标量值，或 None。"""
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def qall(conn, sql: str, params: tuple = ()) -> list[dict]:
    """查询所有行，返回 list[dict]。"""
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def execute(conn, sql: str, params: tuple = ()):
    """
    执行写语句，不自动提交。
    调用方负责 conn.commit() / conn.rollback()。
    """
    conn.cursor().execute(sql, params)
