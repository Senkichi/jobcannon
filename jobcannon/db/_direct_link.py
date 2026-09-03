"""PORTED from job_finder/db/_direct_link.py @ c81bb00205e1a87d14c37f855a90e2a8027cabac
(private job-cannon). Ledger L-0068.
# PORT-SEAM: header rewritten from private's one-line "Sanctioned direct_url
# write paths." summary to carry the port provenance instead.

set_direct_url is the ONLY writer for postings.direct_url / direct_url_confidence (jobcannon/db/migrations/m0017),
# PORT-SEAM: jobs.direct_url -> postings.direct_url; m0017 is this host's migration for these columns
with no-downgrade precedence (highest wins, ties do not overwrite):
    strict  — overwrites a NULL or an existing 'loose' link (upgrade); never
              overwrites an existing 'strict' link (stable).
    loose   — fills a NULL slot only; never overwrites any existing link.

Empty URL or a confidence outside {'strict','loose'} is a no-op.

``stamp_direct_url_checks`` is the ONLY writer for the m0017 resolution-state
columns (``direct_url_checked_at`` / ``direct_url_attempts``). Attempts are
owned exclusively by the scheduled resolver -- one increment per board-match
attempt, matching private's own single-writer statement for these columns.

# PORT-SEAM: this port drops private's ``_reopen_if_unverifiable_archive``
# companion behavior entirely (not merely simplified) -- confirmed via
# ledger L-0066/L-0067's own investigation of this same architectural gap:
# this host has no ``pipeline_events`` history table, no 'archived' status
# in ``_user_actions.py``'s ``_PIPELINE_STATUSES`` (a 2-value
# {"dismissed", "applied"} vocabulary), and no
# ``UNVERIFIABLE_EVIDENCE_PREFIX``-equivalent evidence-string convention to
# search for. Private's re-open-on-corroboration behavior needs ALL THREE of
# those to identify "this job was archived specifically for being an
# unverifiable aggregator listing, and a strict direct_url now corroborates
# it" -- none of which this host can express today. The no-downgrade
# precedence write itself has no such dependency and is fully portable on
# its own; only the archive-reopen side effect is dropped. If a
# pipeline_events-equivalent and a richer pipeline_status vocabulary land on
# this host later, re-adding this behavior is a follow-up, not a blocker to
# porting the core writer now.
# PORT-SEAM: this row's carried_files scope is jobcannon/db/ only --
# jobcannon/web/apply_url.py (this ledger row's other cited evidence file)
# is out of this group's scope. apply_url.py's own module docstring
# currently documents `postings.direct_url` as "permanently NULL... no
# UPDATE anywhere refills it"; that statement becomes stale the moment a
# caller starts invoking set_direct_url, but wiring a resolver call site (or
# updating apply_url.py's read-side precedence to prefer direct_url) is a
# separate, follow-on concern -- this row's scope is the writer pair only,
# matching private's own _direct_link.py (a writer-only module; private's
# resolver call site lives elsewhere too).
"""

from __future__ import annotations

from typing import Any  # PORT-SEAM: no sqlite3 dialect on this host (psycopg only)

from jobcannon.db.pool import (
    commit_unless_nested,
)  # PORT-SEAM: replaces private's bare conn.commit() calls, matching _persistence.py/_jobs.py's transaction-boundary convention

_VALID_CONFIDENCE = ("strict", "loose")

# PORT-SEAM: private's _reopen_if_unverifiable_archive helper dropped here --
# see module docstring above for the full rationale (no pipeline_events
# table / 'archived' pipeline_status / UNVERIFIABLE_EVIDENCE_PREFIX
# equivalent on this host to identify what to re-open).


def set_direct_url(
    conn: Any,  # PORT-SEAM: no sqlite3 dialect on this host (psycopg only)
    dedup_key: str,
    url: str | None,
    confidence: str,
) -> bool:
    """Write the direct company-posting link if precedence permits.

    Returns True if a write happened, False otherwise (gated, missing row,
    or invalid input). Commits on write.
    # PORT-SEAM: private's "A strict write additionally reopens a job
    # archived under Section 4's unverifiable-aggregator-listing policy"
    # paragraph is dropped here -- see module docstring, that side effect
    # is not portable on this host and is not performed by this function.

    Args:
        conn: pooled connection (``.raw`` unwrapped) or a bare psycopg
            connection, matching ``_jobs.py`` / ``_persistence.py``'s
            dispatch. (# PORT-SEAM: private said "Open sqlite3 connection".)
        dedup_key: The posting's natural key. (# PORT-SEAM: postings
            replaces private's jobs table throughout this module.)
        url: The direct company-posting URL. Falsy -> no-op, returns False.
        confidence: 'strict' or 'loose'. Any other value -> no-op, returns
            False.
    """
    if not url or confidence not in _VALID_CONFIDENCE:
        return False

    raw = conn.raw if hasattr(conn, "raw") else conn

    row = raw.execute(
        "SELECT direct_url_confidence FROM postings WHERE dedup_key = %s",  # PORT-SEAM: jobs -> postings, ? -> %s (psycopg paramstyle)
        (dedup_key,),
    ).fetchone()
    if row is None:
        return False

    existing = row[
        "direct_url_confidence"
    ]  # PORT-SEAM: string-key row access replaces private's row[0] (sqlite3.Row positional access)
    if existing is not None:
        if confidence == "loose":
            return False  # never overwrite an existing link with a loose one
        if existing == "strict":
            return False  # strict slot is stable

    with raw.transaction():
        raw.execute(
            "UPDATE postings SET direct_url = %s, direct_url_confidence = %s WHERE dedup_key = %s",
            (url, confidence, dedup_key),
        )
    commit_unless_nested(
        raw
    )  # PORT-SEAM: strict-write archive-reopen side effect dropped here (see module docstring); replaces private's bare conn.commit()
    return True


def stamp_direct_url_checks(
    conn: Any,  # PORT-SEAM: no sqlite3 dialect on this host (psycopg only)
    dedup_keys: list[str],
    # PORT-SEAM: private's now_iso: str param dropped -- server-side now() used instead (see docstring below)
) -> None:
    """Record one resolution attempt for each given posting (single writer, m0017).
    # PORT-SEAM: job -> posting; private's m092 -> this host's m0017

    Sets direct_url_checked_at (server-side ``now()``) and increments
    direct_url_attempts. Called by the primary-source resolver after a
    board-match attempt -- whether or not the posting resolved (a resolved
    row leaves the candidate pool via its non-NULL direct_url, so the
    attempt count is only consulted for misses). Commits once for the batch.
    # PORT-SEAM: private took an explicit now_iso: str parameter (its own
    # utc_now_iso() at the call site) and looped conn.executemany over
    # per-row (now_iso, key) pairs; this port uses a server-side now() and
    # a single UPDATE ... WHERE dedup_key = ANY(%s) instead, matching
    # jobcannon/db/_scan_observability.py::get_off_platform_miss_log's
    # = ANY(%s) precedent for a dedup_key-list scoped write -- one round
    # trip instead of N, same all-rows-share-one-timestamp semantics as
    # private's shared now_iso argument.

    Args:
        conn: pooled connection (``.raw`` unwrapped) or a bare psycopg
            connection, matching ``set_direct_url``'s dispatch.
        dedup_keys: The postings' natural keys to stamp. Empty list is a no-op.
    """
    if not dedup_keys:
        return

    raw = conn.raw if hasattr(conn, "raw") else conn

    with raw.transaction():
        raw.execute(
            "UPDATE postings SET direct_url_checked_at = now(), "  # PORT-SEAM: jobs -> postings; server-side now() replaces private's now_iso param
            "direct_url_attempts = COALESCE(direct_url_attempts, 0) + 1 "
            "WHERE dedup_key = ANY(%s)",  # PORT-SEAM: single ANY(%s) UPDATE replaces private's conn.executemany loop
            (dedup_keys,),
        )
    commit_unless_nested(raw)
