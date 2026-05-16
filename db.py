import os
import psycopg2
from psycopg2 import pool
import structlog

log = structlog.get_logger()

_pool: pool.ThreadedConnectionPool | None = None


def init_pool(min_conn: int = 2, max_conn: int = 10) -> None:
    global _pool
    conn_str = os.environ["DB_CONNECTION_STRING"]
    _pool = pool.ThreadedConnectionPool(min_conn, max_conn, dsn=conn_str)
    _check_health()
    log.info("db_pool_ready", min=min_conn, max=max_conn)


def _check_health() -> None:
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    finally:
        _pool.putconn(conn)


def get_conn():
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() at startup")
    return _pool.getconn()


def put_conn(conn) -> None:
    if _pool:
        _pool.putconn(conn)


class db_conn:
    """Context manager: borrows a connection from the pool and returns it."""

    def __enter__(self):
        self._conn = get_conn()
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        put_conn(self._conn)
        return False
