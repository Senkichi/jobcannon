"""Connection pool + the ScanServices.connection_factory implementation.

Contract facts this file is built on (verified against engine d09f406):
- The engine calls the factory with ZERO positional args; `synchronous` is the
  only (keyword) parameter. The Postgres target is bound HERE, never passed in.
- synchronous="NORMAL" maps to a SESSION-level `SET synchronous_commit = off`,
  NOT `SET LOCAL`: SQLite's PRAGMA is connection-scoped and engine call sites
  run many commit cycles on one factory connection — a transaction-scoped
  SET LOCAL would silently revert after the first commit. The pool's reset
  hook restores the default when the connection returns to the pool.
  (Refines spec §3.4, which sketched SET LOCAL — recorded deviation.)
- Yielded connections must run engine-authored qmark/SQLite-dialect SQL and
  return rows supporting both row["col"] and row[0] (see compat.py / rows.py).
"""

from __future__ import annotations

import contextlib
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from jobcannon.db.compat import qmark_to_format
from jobcannon.db.rows import hybrid_row

_pool: ConnectionPool | None = None


def _reset(conn: psycopg.Connection) -> None:
    # Runs when a connection returns to the pool: undo session-level tweaks.
    with conn.transaction():
        conn.execute("RESET synchronous_commit")


def open_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
    global _pool
    if _pool is not None:
        return
    _pool = ConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": hybrid_row},
        reset=_reset,
        open=False,
    )
    _pool.open()


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("jobcannon.db.pool not opened — call open_pool(dsn) at startup")
    return _pool


class EngineCompatConnection:
    """sqlite3.Connection-shaped facade over a pooled psycopg connection.

    ONLY the surface engine code actually uses: execute / commit / rollback /
    close (close is a no-op — pool owns the lifecycle). Host code should use
    the raw psycopg connection (`.raw`) and psycopg placeholders.
    """

    def __init__(self, conn: psycopg.Connection):
        self.raw = conn

    def execute(self, sql: str, params: Any = ()) -> psycopg.Cursor:
        return self.raw.execute(qmark_to_format(sql), params)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:  # engine never closes factory connections, but be safe
        pass


@contextlib.contextmanager
def connection_factory(*, synchronous: str = "FULL"):
    """THE ScanServices.connection_factory. Zero positional args by contract."""
    with get_pool().connection() as conn:
        if synchronous == "NORMAL":
            with conn.transaction():
                conn.execute("SET SESSION synchronous_commit = off")
        yield EngineCompatConnection(conn)
