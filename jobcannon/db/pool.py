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
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool

from jobcannon.db.compat import engine_sql_to_host
from jobcannon.db.rows import hybrid_row

_pool: ConnectionPool | None = None


def _conninfo_with_defaults(dsn: str) -> str:
    """Bound the connect phase of every pooled connection attempt.

    libpq's default connect_timeout is unbounded (in practice the OS TCP
    timeout, ~2 minutes), which sits behind the pool's 30 s acquire
    deadline: one hung connect attempt silently consumes the entire window
    and every waiting request times out with nothing logged and nothing
    reaching the server. A bounded connect_timeout turns a blackholed
    attempt into a fast failure the pool can retry (~3 attempts per acquire
    window) and log. An explicit connect_timeout already present in the DSN
    wins.
    """
    params = conninfo_to_dict(dsn)
    params.setdefault("connect_timeout", "10")
    return make_conninfo(**params)


def _configure(conn: psycopg.Connection) -> None:
    # Runs ONCE when a new connection is created (unlike `_reset`, which runs
    # on every return-to-pool): pin the session timezone so timestamptz reads
    # (e.g. corpus_stats' freshest_last_seen) always come back UTC-aware,
    # making the templates' hardcoded "... UTC" label true by construction
    # instead of incidentally matching whatever TimeZone GUC the connecting
    # role happens to default to.
    with conn.transaction():
        conn.execute("SET TIME ZONE 'UTC'")


def _reset(conn: psycopg.Connection) -> None:
    # Runs when a connection returns to the pool: undo session-level tweaks.
    with conn.transaction():
        conn.execute("RESET synchronous_commit")


def open_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
    global _pool
    if _pool is not None:
        return
    _pool = ConnectionPool(
        conninfo=_conninfo_with_defaults(dsn),
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": hybrid_row},
        configure=_configure,
        reset=_reset,
        # Checkout-time liveness probe: a pooled connection whose TCP flow
        # died while idle is discarded and replaced instead of being handed
        # to a request that would then fail mid-query.
        check=ConnectionPool.check_connection,
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

    ONLY the surface engine code actually uses: execute / executemany /
    commit / rollback / close (close is a no-op — pool owns the lifecycle).
    Host code should use the raw psycopg connection (`.raw`) and psycopg
    placeholders.
    """

    def __init__(self, conn: psycopg.Connection):
        self.raw = conn

    def execute(self, sql: str, params: Any = ()) -> psycopg.Cursor:
        return self.raw.execute(engine_sql_to_host(sql), params)

    def executemany(self, sql: str, params_seq: Any) -> None:
        # psycopg.Connection has no executemany shorthand (only Cursor does),
        # unlike its execute() shorthand used above — route through a cursor.
        self.raw.cursor().executemany(engine_sql_to_host(sql), params_seq)

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


def commit_unless_nested(raw: psycopg.Connection) -> None:
    """Best-effort commit shared by _companies.py / _jobs.py / _jd_full.py.

    Those modules are called BOTH through a bare pooled connection (no
    ambient transaction — a real .commit() is required here, mirroring the
    sqlite3-autocommit=False model the engine's own call sites use) AND
    directly against tests/host/conftest.py's `db_conn` fixture, which wraps
    the whole test in `with conn.transaction():` for rollback-based
    isolation. psycopg3 raises ProgrammingError on an explicit .commit() (or
    .rollback()) while a `Transaction()` context is active on the connection
    — verified empirically 2026-07-17 against psycopg 3.3.4 — and tracks
    that via the connection's own `_num_transactions` counter (the same
    counter psycopg3's `_commit_gen`/`_rollback_gen` guard against). When
    nested inside one, the ambient context owns the commit/rollback boundary
    (and read-your-own-writes within that same connection already works
    without an explicit commit), so this is a no-op there.
    """
    if getattr(raw, "_num_transactions", 0) == 0:
        raw.commit()
