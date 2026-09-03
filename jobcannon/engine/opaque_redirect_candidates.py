# PORTED from job_finder/web/opaque_redirect_candidates.py @ c3ee3350643d5530c2ac869b40a1f671c0f32552 (private job-cannon). Ledger L-0219.
"""Derived opaque-redirect candidate helpers.

Small DB-backed shadow list for hosts whose Signal-0 (direct URL) outcomes are
consistently blocked by auth/anti-bot walls. Populated from observed Phase-C and
scoring liveness outcomes, consulted alongside verification.opaque_redirect_sources
so only redundant Signal-0 GETs are skipped.
"""

from __future__ import annotations

import logging
import sqlite3
from urllib.parse import urlparse

from jobcannon.engine.json_utils import utc_now_iso
from jobcannon.engine.source_registry import is_opaque_redirect_host

logger = logging.getLogger(__name__)

# Two-label public suffixes for registrable-domain reduction.
# Sourced from jobcannon.engine.pipeline_detector._off_platform; inlined here to
# avoid crossing a private-module boundary.
_TWO_LABEL_PUBLIC_SUFFIXES = frozenset(
    {
        "co.uk",
        "co.in",
        "co.jp",
        "co.kr",
        "co.nz",
        "co.za",
        "com.au",
        "com.br",
        "com.mx",
        "com.sg",
    }
)


def _registrable_domain(host: str) -> str | None:
    """Reduce ``careers.acme.co.uk`` → ``acme.co.uk``.

    Strips subdomains and handles two-label public suffixes (co.uk, com.au,
    etc.) so that ``www.jooble.org`` and ``jooble.org`` both resolve to
    ``jooble.org``.
    """
    if not host:
        return None
    parts = host.split(".")
    if len(parts) < 2:
        return None
    if len(parts) >= 3:
        candidate_suffix = ".".join(parts[-2:])
        if candidate_suffix in _TWO_LABEL_PUBLIC_SUFFIXES:
            return ".".join(parts[-3:])
    return ".".join(parts[-2:])


DEFAULT_MIN_SAMPLES = 20
DEFAULT_BLOCK_RATIO = 0.95


def _get_thresholds(config: dict | None) -> tuple[int, float]:
    """Return (min_samples, block_ratio) from config or defaults."""
    v = (config or {}).get("verification") or {}
    try:
        min_samples = int(v.get("opaque_derive_min_samples", DEFAULT_MIN_SAMPLES))
    except (TypeError, ValueError):
        min_samples = DEFAULT_MIN_SAMPLES
    try:
        block_ratio = float(v.get("opaque_derive_block_ratio", DEFAULT_BLOCK_RATIO))
    except (TypeError, ValueError):
        block_ratio = DEFAULT_BLOCK_RATIO
    return min_samples, block_ratio


def registrable_host(url: str | None) -> str | None:
    """Reduce a URL to its registrable domain, lowercased.

    Falls back to the lowercased hostname if the public-suffix reduction fails.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except (ValueError, TypeError):
        return None
    if not host:
        return None
    return _registrable_domain(host) or host


def _query_host(conn: sqlite3.Connection, host: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT attempts, blocked_count FROM opaque_redirect_host_outcomes WHERE host = ?",
        (host,),
    ).fetchone()


def _host_flagged(attempts: int, blocked_count: int, min_samples: int, block_ratio: float) -> bool:
    if attempts < min_samples:
        return False
    return blocked_count / attempts >= block_ratio


def is_opaque_redirect_candidate(
    url: str | None,
    conn: sqlite3.Connection | None = None,
    db_path: str | None = None,
    config: dict | None = None,
) -> bool:
    """Return True if the URL's host is a derived opaque-redirect candidate.

    A host is a candidate when its Signal-0 outcome tally has crossed the
    configured threshold (min_samples + block_ratio). Falls back to False when
    no DB connection or table is unavailable.
    """
    host = registrable_host(url)
    if not host:
        return False
    if is_opaque_redirect_host(host, config):
        return False

    min_samples, block_ratio = _get_thresholds(config)

    try:
        if conn is None:
            # PORT-SEAM: host connection_factory replaces db_helpers.standalone_connection;
            # db_path is host-bound at construction time, not passed per-call.
            from jobcannon.engine.services import get_services

            with get_services().connection_factory() as c:
                row = _query_host(c, host)
        else:
            row = _query_host(conn, host)
    except sqlite3.OperationalError:
        return False

    if row is None:
        return False
    attempts = row["attempts"] or 0
    blocked_count = row["blocked_count"] or 0
    return _host_flagged(attempts, blocked_count, min_samples, block_ratio)


def record_signal0_outcome(
    conn: sqlite3.Connection,
    url: str | None,
    attempted: bool,
    blocked: bool,
    config: dict | None,
) -> bool:
    """Persist one Signal-0 outcome and return True if the host is newly flagged.

    Idempotent upsert: increments attempts, and blocked_count when the outcome
    was an auth/anti-bot block. Logs at INFO the first time a host crosses the
    configured threshold.
    """
    if not attempted or not url:
        return False
    host = registrable_host(url)
    if not host:
        return False
    if is_opaque_redirect_host(host, config):
        return False

    min_samples, block_ratio = _get_thresholds(config)
    now = utc_now_iso()

    try:
        row = _query_host(conn, host)
        pre_attempts = row["attempts"] or 0 if row is not None else 0
        pre_blocked = row["blocked_count"] or 0 if row is not None else 0
        pre_flagged = _host_flagged(pre_attempts, pre_blocked, min_samples, block_ratio)

        # Atomic increment: avoids lost-update races when parallel scoring workers
        # call this on separate connections at the same time.
        conn.execute(
            "INSERT INTO opaque_redirect_host_outcomes (host, attempts, blocked_count, last_seen) "
            "VALUES (?, 1, ?, ?) "
            "ON CONFLICT(host) DO UPDATE SET "
            "attempts = attempts + 1, "
            "blocked_count = blocked_count + excluded.blocked_count, "
            "last_seen = excluded.last_seen",
            (host, 1 if blocked else 0, now),
        )
        conn.commit()

        row = _query_host(conn, host)
        attempts = row["attempts"] or 0
        blocked_count = row["blocked_count"] or 0
        post_flagged = _host_flagged(attempts, blocked_count, min_samples, block_ratio)
        if not pre_flagged and post_flagged:
            logger.info(
                "Opaque-redirect candidate first flagged: %s (%d/%d blocked)",
                host,
                blocked_count,
                attempts,
            )
            return True
        return False
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e).lower():
            logger.warning("Failed to record Signal-0 outcome for %s: %s", host or url, e)
        return False


def get_flagged_opaque_redirect_hosts(conn: sqlite3.Connection, config: dict | None) -> list[dict]:
    """Return all hosts currently above the derived opaque-redirect threshold.

    Filters out hosts already present in verification.opaque_redirect_sources
    (promoted to the YAML registry) so callers surface only actionable shadow
    candidates.
    """
    min_samples, block_ratio = _get_thresholds(config)
    try:
        rows = conn.execute(
            "SELECT host, attempts, blocked_count, last_seen FROM opaque_redirect_host_outcomes"
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    flagged = []
    for row in rows:
        attempts = row["attempts"] or 0
        blocked_count = row["blocked_count"] or 0
        if not _host_flagged(attempts, blocked_count, min_samples, block_ratio):
            continue
        host = row["host"]
        if is_opaque_redirect_host(host, config):
            continue
        flagged.append(
            {
                "host": host,
                "attempts": attempts,
                "blocked_count": blocked_count,
                "block_ratio": round(blocked_count / attempts, 4),
                "last_seen": row["last_seen"],
            }
        )
    return flagged
