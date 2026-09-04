"""PORTED from job_finder/web/ingestion_runner.py::_log_per_sender_email_parse
@ bc30befa311b5c78868ece3dddd60b44d018f444 (private job-cannon).
Ledger L-0279.
# PORT-SEAM: header inserted above; the private function's own docstring and
# body below follow verbatim except where a PORT-SEAM note says otherwise.
# The rest of ingestion_runner.py (the orchestration that calls ImapSource,
# upsert_job, etc.) is a SEPARATE ledger row (L-0188), out of this unit's
# scope -- only this one aggregation+write function ports here, because it
# is the sole writer of the table L-0279's migration creates.

Write one email_parse_log_sender row PER registered IMAP sender label.

Aggregates a run's extraction_records (one entry per message the caller
actually processed, keyed by canonical sender label -- see
host/ingestion/imap_intake.py) and parse_failures (one entry per parser
exception, also labeled) into per-sender counts.

Every label in the sender registry (jobcannon.engine.email_senders.SENDERS)
gets a row even when it saw zero emails this run: a ZERO-COUNT row, not an
absent one -- D19: without it, "ZipRecruiter's alert subscription lapsed (0
emails ever)" and "ZipRecruiter's parser silently yields nothing" are
indistinguishable in the DB.

Never raises -- observability must not break ingestion (matches the
private function's own contract exactly).

# PORT-SEAM: PII chokepoint (design-aggregators-imap.md §6). The private
# function stored `last_error` (a parser exception's str(e)) with NO
# scrubbing -- a parser exception message can embed a slice of the raw
# email body it choked on. This module is the SOLE writer of
# email_parse_log_sender (single-writer-per-table convention, see
# jobcannon/db/_mailbox_credentials.py's docstring for the sibling
# convention on mailbox_credentials) AND the sole chokepoint for any
# captured email-derived text in this port -- record_run always routes
# last_error through jobcannon.engine._pii_scrub.scrub_text before it is
# ever bound into an INSERT. identifiers are resolved HERE, per-tenant,
# from this user_id's own users.email at call time -- never a
# caller-supplied or process-global value (§6: "NEVER a process-global";
# jobcannon/host/health_recorder.py is the counter-example this module
# deliberately does NOT follow -- see that module's own docstring).
#
# No recipient headers, To/Cc, or tracking-query-params are ever stored
# here -- the only column that can carry email-derived text at all is
# last_error, and it is always scrubbed. There is no run-level
# email_parse_log table in this port (design note §1.7); this per-sender
# table is the entirety of parse-outcome persistence.

# PORT-SEAM: multi-tenant widening. `user_id` threads through every row
# (m0026); the INSERT's ON CONFLICT target widens from
# (run_id, sender_label) to (user_id, run_id, sender_label) to match.
# `INSERT OR IGNORE` (SQLite) -> `ON CONFLICT (...) DO NOTHING` (Postgres).
# `now` (private: utc_now_iso()) -> `processed_at`, passed in by the caller
# rather than computed here, so a caller can attribute every sender's row
# in one run to the same instant without a second clock read (mirrors
# jobcannon/db/_events.py's db_now_iso() rationale: no datetime.now() in a
# persistence path -- see arch_store_utc_render_local).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from jobcannon.db.pool import commit_unless_nested
from jobcannon.engine._pii_scrub import scrub_text
from jobcannon.engine.email_senders import SENDERS

logger = logging.getLogger(__name__)

_KNOWN_SENDER_LABELS: frozenset[str] = frozenset(spec.label for spec in SENDERS)


def _tenant_identifiers(raw: Any, user_id: str) -> tuple[str, ...]:
    """This tenant's own email, resolved fresh at call time -- never cached,
    never process-global (§6)."""
    row = raw.execute("SELECT email FROM users WHERE id = %s", (user_id,)).fetchone()
    email = row["email"] if row else None
    return (email,) if email else ()


def record_run(
    conn: Any,
    user_id: str,
    *,
    run_id: str,
    processed_at: datetime,
    extraction_records: list[dict],
    parse_failures: list[dict],
) -> None:
    """Write this run's per-sender email_parse_log_sender rows for `user_id`.

    `extraction_records`: one dict per message processed, each carrying at
    least `label` (str) and `job_count` (int) -- the same shape
    host/ingestion/imap_intake.py builds per message (mirrors the private
    ImapSource.extraction_records entries).

    `parse_failures`: one dict per parser exception, each carrying `label`
    (falls back to `sender` then the literal "unknown") and `error` (str).

    Never raises: a failure here must not break ingestion (matches the
    private function's contract). Commits on write, best-effort.
    """
    try:
        raw = conn.raw if hasattr(conn, "raw") else conn

        emails_seen: dict[str, int] = {}
        jobs_parsed: dict[str, int] = {}
        error_counts: dict[str, int] = {}
        last_error: dict[str, str] = {}

        for rec in extraction_records:
            label = rec["label"]
            emails_seen[label] = emails_seen.get(label, 0) + 1
            jobs_parsed[label] = jobs_parsed.get(label, 0) + rec.get("job_count", 0)

        for fail in parse_failures:
            label = fail.get("label") or fail.get("sender", "unknown")
            error_counts[label] = error_counts.get(label, 0) + 1
            last_error[label] = fail.get("error", "")

        identifiers = _tenant_identifiers(raw, user_id)

        labels = _KNOWN_SENDER_LABELS | set(emails_seen) | set(error_counts)
        for label in sorted(labels):
            raw_error = last_error.get(label)
            scrubbed_error = scrub_text(raw_error, identifiers=identifiers) if raw_error else None
            raw.execute(
                """INSERT INTO email_parse_log_sender
                   (user_id, run_id, sender_label, processed_at, emails_seen,
                    jobs_parsed, error_count, last_error)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (user_id, run_id, sender_label) DO NOTHING""",
                (
                    user_id,
                    run_id,
                    label,
                    processed_at,
                    emails_seen.get(label, 0),
                    jobs_parsed.get(label, 0),
                    error_counts.get(label, 0),
                    scrubbed_error,
                ),
            )
        commit_unless_nested(raw)
    except Exception as exc:
        logger.warning("Failed to write to email_parse_log_sender: %s", exc)
