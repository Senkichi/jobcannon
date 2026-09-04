"""PORTED from job_finder/web/migrations/m209524590_email_parse_log_per_sender.py
@ bc30befa311b5c78868ece3dddd60b44d018f444 (private job-cannon).
Ledger L-0279.
# PORT-SEAM: header inserted above; the private module's own docstring and
# body below follow verbatim except where a PORT-SEAM note says otherwise.

Migration 209524590 -- email parse log per sender (T2.6, D19).

Adds a new ``email_parse_log_sender`` table so each IMAP run records one row
PER registered alert sender (LinkedIn, Glassdoor, Indeed, ZipRecruiter, ...)
instead of one batched row across all six IMAP senders. A sender with zero
emails this run still writes a zero-count row, so "no alert emails ever
arrived for this sender" is distinguishable from "parser silently yields
nothing" (D19 -- ZipRecruiter had 0 jobs ever and no way to tell which case
it was in).

Deliberately a NEW table, not new rows in an ``email_parse_log`` run-level
table: this port has no run-level ``email_parse_log`` table at all (design
note §1.7 -- out of scope for v1; recommended follow-up: FU-C). The private
repo's adjacent-consumer rationale (``onboarding/inbox_check.py``) has no
analogue here, so this port keeps only the per-sender table.

# PORT-SEAM: multi-tenant widening (design note §1.7, §3 PR-3). The private
# table has no user_id column -- job-cannon (private) is single-user. This
# port adds `user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE`
# and widens the private UNIQUE(run_id, sender_label) constraint to
# UNIQUE(user_id, run_id, sender_label), since run_id is scoped to one
# ingest task invocation but is not itself guaranteed globally unique
# across tenants. No RLS on this table (design note §1.7/§3 name only the
# cascade FK, not a second RLS policy convention) -- jobcannon/host/
# ingestion/capture.py is this table's sole writer and always executes with
# the caller's authenticated user_id, matching the single-writer-per-table
# convention (tests/host/test_events_single_writer.py-style AST guard is
# out of scope for this table; capture.py's docstring states the contract).

# PORT-SEAM: type translation for Postgres (SQLite -> Postgres). INTEGER
# PRIMARY KEY AUTOINCREMENT -> bigserial PRIMARY KEY. TEXT -> text.
# processed_at TEXT (ISO8601 string) -> timestamptz (this repo's uniform
# timestamp convention, see m0001 CREATE TABLE users.created_at and every
# other migration in this directory) -- capture.py passes an explicit
# aware UTC datetime, mirroring the private column's semantics (recorded
# at write time, not a DEFAULT now() -- the private code passes it
# explicitly too, since it wants the recorded time attributable to the
# individual sender's processing step rather than row-commit time).

Note (pre-existing, NOT fixed by this port): the private repo's
``_fetch_imap`` writes its run-level ``email_parse_log`` row unconditionally
whenever IMAP is enabled, regardless of whether any email actually arrived
that run. This port has no run-level table (see above), so the defect has
no analogue to inherit -- noted here only for fidelity-diff context.

Column semantics note: ``email_parse_log_sender.jobs_parsed`` counts jobs
PRE the title-hygiene gate (straight from extraction), matching the
private column's semantics.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=26,
    description="email_parse_log_sender table (per-sender IMAP parse outcomes, multi-tenant)",
    sql=[
        """
        CREATE TABLE email_parse_log_sender (
            id            bigserial PRIMARY KEY,
            user_id       text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            run_id        text NOT NULL,
            sender_label  text NOT NULL,
            processed_at  timestamptz NOT NULL,
            emails_seen   integer NOT NULL DEFAULT 0,
            jobs_parsed   integer NOT NULL DEFAULT 0,
            error_count   integer NOT NULL DEFAULT 0,
            last_error    text,
            UNIQUE (user_id, run_id, sender_label)
        )
        """,
        "CREATE INDEX idx_email_parse_log_sender_label_processed_at"
        " ON email_parse_log_sender(sender_label, processed_at)",
    ],
)
