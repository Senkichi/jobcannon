"""upsert_company — Postgres port of the private repo's single company-write
chokepoint. Same signature, same monotonic probe-status rule
(hit=2 > pending=1 > miss=0; UPDATE applies at new_rank >= current_rank so an
equal-rank sighting still refreshes fields via COALESCE, but a downgrade
never lands), same collision semantics (UNIQUE(ats_platform, ats_slug):
another owner of the pair -> log, leave ATS fields untouched, return the id).

Row access note: all internal row reads use STRING keys only (never
positional). This function is called both through the pooled
connection_factory (HybridRow — supports both access styles) and directly
against a bare psycopg connection in tests (`dict_row` factory — a plain
dict, which does NOT support integer indexing). String-key access is the
only style both row shapes share.

Transaction-boundary note (recorded port deviation): every write is wrapped
in its own `with raw.transaction():` block rather than calling
raw.commit()/raw.rollback() directly. This function is exercised both
against a bare pooled connection (no ambient transaction — the `with` block
is a real transaction) AND against tests/host/conftest.py's `db_conn`
fixture, which itself wraps the whole test in `with conn.transaction():`
for rollback-based isolation. psycopg3 forbids explicit commit()/rollback()
while an outer Transaction() context is active on the connection
("Explicit commit() forbidden within a Transaction context" — verified
empirically 2026-07-17); nesting `with raw.transaction():` is the psycopg3-
idiomatic fix — it degrades to a SAVEPOINT when already inside a
transaction, so the UniqueViolation-retry and the outer fail-closed
exception handler both get automatic rollback-to-savepoint without ever
calling .rollback() explicitly. On the bare pooled connection this is a
real transaction that auto-commits on successful exit.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

logger = logging.getLogger(__name__)

_PROBE_STATUS_PRECEDENCE = {"hit": 2, "pending": 1, "miss": 0}
_MAX_NAME_LEN = 200


def upsert_company(
    conn: Any,
    name: str,
    ats_platform: str | None = None,
    ats_slug: str | None = None,
    ats_probe_status: str = "pending",
    homepage_url: str | None = None,
) -> int | None:
    raw = conn.raw if hasattr(conn, "raw") else conn
    normalized = (name or "").strip()
    if not normalized or len(normalized) > _MAX_NAME_LEN or not any(c.isalpha() for c in normalized):
        return None
    try:
        existing = raw.execute(
            "SELECT id, ats_probe_status FROM companies WHERE name = %s", (normalized,)
        ).fetchone()
        if existing is None:
            try:
                with raw.transaction():
                    row = raw.execute(
                        "INSERT INTO companies (name, ats_platform, ats_slug, ats_probe_status, homepage_url) "
                        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                        (normalized, ats_platform, ats_slug, ats_probe_status, homepage_url),
                    ).fetchone()
            except psycopg.errors.UniqueViolation:
                # (ats_platform, ats_slug) collision on INSERT: retry without ATS fields.
                logger.warning(
                    "upsert_company: ATS collision for %r on %s/%s — inserting without ATS fields",
                    normalized, ats_platform, ats_slug,
                )
                with raw.transaction():
                    row = raw.execute(
                        "INSERT INTO companies (name, homepage_url) VALUES (%s, %s) RETURNING id",
                        (normalized, homepage_url),
                    ).fetchone()
            return row["id"]

        company_id, current_status = existing["id"], existing["ats_probe_status"] or "pending"
        current_rank = _PROBE_STATUS_PRECEDENCE.get(current_status, 0)
        new_rank = _PROBE_STATUS_PRECEDENCE.get(ats_probe_status, 0)
        if new_rank >= current_rank:
            try:
                with raw.transaction():
                    raw.execute(
                        "UPDATE companies SET "
                        "  ats_platform = COALESCE(%s, ats_platform), "
                        "  ats_slug = COALESCE(%s, ats_slug), "
                        "  ats_probe_status = %s, "
                        "  homepage_url = COALESCE(%s, homepage_url), "
                        "  consecutive_empty_scans = CASE WHEN %s IS NOT NULL AND %s IS NOT NULL "
                        "      THEN 0 ELSE consecutive_empty_scans END, "
                        "  updated_at = now() "
                        "WHERE id = %s",
                        (ats_platform, ats_slug, ats_probe_status, homepage_url,
                         ats_platform, ats_slug, company_id),
                    )
            except psycopg.errors.UniqueViolation as exc:
                logger.warning(
                    "upsert_company: ATS collision for %r on %s/%s — leaving ATS fields untouched. exc=%s",
                    normalized, ats_platform, ats_slug, exc,
                )
                with raw.transaction():
                    raw.execute(
                        "UPDATE companies SET homepage_url = COALESCE(%s, homepage_url), updated_at = now() "
                        "WHERE id = %s",
                        (homepage_url, company_id),
                    )
        else:
            with raw.transaction():
                raw.execute(
                    "UPDATE companies SET homepage_url = COALESCE(%s, homepage_url), updated_at = now() "
                    "WHERE id = %s",
                    (homepage_url, company_id),
                )
        return company_id
    except Exception:
        logger.warning("upsert_company failed for %r", name, exc_info=True)
        return None
