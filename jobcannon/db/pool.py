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
import ipaddress
import logging
import os
import socket
import threading
import time
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool

from jobcannon.db.compat import engine_sql_to_host
from jobcannon.db.rows import hybrid_row

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None

# Boot-time resolution bounds (see _pin_hostaddr): each attempt gets its own
# wall-clock budget because a HUNG resolver (the 2026-08-26 failure mode) never
# returns at all — an unbounded call here would wedge worker boot instead of
# failing it.
_RESOLVE_ATTEMPTS = 3
_RESOLVE_TIMEOUT_S = 5.0
_RESOLVE_BACKOFF_S = 1.0


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
    # Server-side attribution: pg_stat_activity.application_name says WHICH
    # service a backend belongs to (web/worker share this module, and Render's
    # external-endpoint proxy rewrites client_addr, which made backends
    # unattributable during the 2026-08-26 incident diagnosis). Render sets
    # RENDER_SERVICE_NAME on every service; an explicit application_name in
    # the DSN wins.
    params.setdefault("application_name", os.environ.get("RENDER_SERVICE_NAME", "jobcannon"))
    # connect_timeout bounds only connection ESTABLISHMENT. Once a
    # connection exists, a silently blackholed socket (established TCP,
    # peer gone, no RST — the 2026-08-26 mode) hangs every later
    # round-trip (queries, the pool's checkout liveness probe, commits)
    # until kernel TCP retransmission gives up (~15+ min). These bound
    # that at the TCP layer: an active query on a dead socket aborts once
    # unacked data is ~30 s old, and keepalives reap dead IDLE
    # connections in ~60 s so the pool's next checkout gets a fast error
    # instead of a hang. Both are no-ops where the OS lacks the option
    # (tcp_user_timeout and keepalives_count are ignored on Windows dev
    # boxes; production is Linux). Explicit DSN values win.
    params.setdefault("tcp_user_timeout", "30000")
    params.setdefault("keepalives", "1")
    params.setdefault("keepalives_idle", "30")
    params.setdefault("keepalives_interval", "10")
    params.setdefault("keepalives_count", "3")
    _pin_hostaddr(params)
    return make_conninfo(**params)


def _getaddrinfo_bounded(host: str, timeout: float) -> list:
    """getaddrinfo with a wall-clock bound, hang included.

    socket.getaddrinfo has no timeout parameter, and the 2026-08-26 incident's
    resolver didn't fail — it HUNG, which no try/except can bound. Running it
    on a daemon thread converts a hang into a TimeoutError after `timeout`
    seconds; the abandoned thread cannot block interpreter exit.
    """
    result: dict[str, Any] = {}

    def _run() -> None:
        try:
            result["infos"] = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except OSError as ex:
            result["error"] = ex

    worker = threading.Thread(target=_run, daemon=True, name=f"resolve-{host}")
    worker.start()
    worker.join(timeout)
    if "infos" in result:
        return result["infos"]
    if "error" in result:
        raise result["error"]
    raise TimeoutError(f"resolution of {host!r} did not complete within {timeout:.0f}s")


def _pin_hostaddr(params: dict) -> None:
    """Resolve the DB hostname ONCE at pool-open time and pin `hostaddr`.

    connect_timeout (above) bounds only libpq's timed phase; name
    resolution happens before it and is unbounded. 2026-08-26 incident:
    in-container DNS died shortly after boot while the network itself
    stayed up — every post-boot connection attempt hung inside
    getaddrinfo, before the timed phase, wedging the pool's worker
    threads with nothing logged (internal and external DB hostnames
    alike). Pinning hostaddr makes every later connect dial the IP
    directly (libpq still uses `host` for TLS/auth), so a resolver that
    dies after boot cannot take the pool down with it.

    Resolution failure at boot is FATAL, by design. The first version of
    this pin fell back to dialing by name, which silently re-admitted the
    exact untimed getaddrinfo hang the pin exists to prevent (observed in
    production the same day: the fallback engaged, nothing was logged, and
    every request rode the pool's full acquire deadline again). A process
    that cannot resolve its database at boot cannot serve anything useful,
    so it must die loudly here — the platform's health checks then replace
    the instance (and a zero-downtime deploy keeps the previous instance
    serving) instead of leaving a permanently wedged one in rotation.

    Trade-off, accepted deliberately: if the server's IP changes while an
    instance is running (provider failover), connects fail until the
    instance restarts and re-resolves. Skipped when the DSN already
    carries hostaddr, has no host, uses an IP literal or a unix-socket
    path, or lists multiple hosts.
    """
    host = params.get("host")
    if not host or "hostaddr" in params or "," in host or host.startswith("/"):
        return
    try:
        ipaddress.ip_address(host)
        return  # already an IP literal — nothing to pin
    except ValueError:
        pass
    last_error: Exception | None = None
    for attempt in range(1, _RESOLVE_ATTEMPTS + 1):
        try:
            infos = _getaddrinfo_bounded(host, _RESOLVE_TIMEOUT_S)
        except (OSError, TimeoutError) as ex:
            last_error = ex
            logger.warning(
                "boot-time resolution of DB host failed (attempt %d/%d): %s",
                attempt,
                _RESOLVE_ATTEMPTS,
                ex,
            )
            if attempt < _RESOLVE_ATTEMPTS:
                time.sleep(_RESOLVE_BACKOFF_S)
            continue
        for family in (socket.AF_INET, socket.AF_INET6):
            for info in infos:
                if info[0] == family:
                    params["hostaddr"] = info[4][0]
                    logger.info("pinned DB hostaddr %s for %r", info[4][0], host)
                    return
        last_error = OSError(f"no usable address family in resolution of {host!r}")
        break
    logger.critical(
        "refusing to open DB pool: could not resolve %r at boot (%s) — "
        "dialing by name would hang untimed if the resolver dies post-boot",
        host,
        last_error,
    )
    raise RuntimeError(f"could not resolve database host {host!r} at boot: {last_error}")


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


def is_open() -> bool:
    """Whether open_pool has run in this process.

    /healthz keys on this to keep the DB-free dev/test contract: no pool
    configured means no probe, not an unhealthy verdict.
    """
    return _pool is not None


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
