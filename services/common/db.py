"""Database access. One pool per process, opened lazily.

Registry, geospatial and time-series all live in this one database — that is the
whole point of the TimescaleDB + PostGIS choice, and why there is no second
store to keep consistent.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from services.common.config import settings

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Process-wide connection pool, created on first use."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=settings.dsn,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection. Commits on clean exit, rolls back on exception."""
    with get_pool().connection() as conn:
        yield conn


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    with connection() as conn, conn.cursor() as cur:
        yield cur


def fetch_all(sql: str, params: Any = None) -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: Any = None) -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: Any = None) -> int:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def wait_for_db(timeout_s: float = 60.0, interval_s: float = 1.0) -> None:
    """Block until the database answers, or raise.

    Compose health checks cover the common case; this covers the rest, since
    every one-shot job in the stack races the database on a cold start.
    """
    import time

    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(settings.dsn, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready yet"
            last = exc
            time.sleep(interval_s)
    raise RuntimeError(f"database not reachable within {timeout_s}s: {last}")
