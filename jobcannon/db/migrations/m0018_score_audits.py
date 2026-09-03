"""PORTED from job_finder/web/migrations/m206169048_add_score_audits_table.py
+ m207063516_add_skipped_verdict_to_score_audits.py
@ dcbde72e65d42662d6790ec53bd619e87fd1d2a0 (private job-cannon).
Ledger L-0079, L-0282.
# PORT-SEAM: header inserted above; the private modules' own docstrings and
# body below follow verbatim except where another PORT-SEAM note says
# otherwise.

Migration 206169048 -- add score audits table.

Nightly Sonnet scoring-audit results (NIGHTLY_MONITOR_SPEC.md §5.1). One row
per audit pass over a job's then-current sub-scores; "previously reviewed" is
snapshot-based — a job is re-auditable when jobs.sub_scores_json no longer
equals the latest row's audited_sub_scores_json.

# PORT-SEAM: private shipped this as two migrations -- m206169048 created the
# table with `verdict CHECK (agree, dispute)`, then m207063516 (Migration
# 207063516 -- add skipped verdict to score audits; "Nightly audit batch
# resilience (#1404) needs a `skipped` verdict for items that cannot be
# audited individually") widened the CHECK via SQLite's rename/recreate/copy
# ALTER-TABLE workaround. The host has no pre-existing score_audits data to
# migrate, so both are collapsed into ONE migration that creates the table
# with the final three-way CHECK directly -- same precedent as m0014
# (scan_title_outcomes): "Schema-only port... no row lands until [the]
# writer is ported separately." Both private ledger rows (L-0079 writer,
# L-0282 skip-bound cutover) land against this single CREATE TABLE; there is
# no separate host migration for L-0282's cutover watermark -- see the
# PORT-SEAM below and jobcannon/db/_score_audits.py's module docstring for
# why the #1806 cutover mechanism itself is dropped, not reproduced.

# PORT-SEAM: version renumbered to 18 (host's sequential-integer scheme; the
# private originals above are epoch-second stamps -- see
# jobcannon/db/migrations/types.py module docstring). DDL dialect-translated
# SQLite -> Postgres (bigserial id, text columns, timestamptz for
# audited_at rather than private's TEXT-ISO -- see below). No FK from
# dedup_key to postings(dedup_key): private never enforced referential
# integrity here either (plain indexed TEXT column), and this table is
# unwired in this PR (no writer call site yet), so adding one now would be
# a schema decision ahead of the caller that needs it. A brand-new table
# with no existing column touched, so this migration is expand-safe by
# shape -- no contract_step needed, and the one index is built on the table
# this same migration creates so lock_step (issue #219) does not apply.

# PORT-SEAM: `audited_at` is `timestamptz NOT NULL DEFAULT now()` rather
# than private's `TEXT NOT NULL` (populated by the writer via
# `utc_now_iso()`). record_score_audit does not pass a value for this
# column -- matching every other host single-writer INSERT's own
# "when written" column (companies.created_at, scan_title_outcomes.seen_at,
# etc.), all DB-generated via DEFAULT now() rather than app-generated and
# passed explicitly. See _score_audits.py.

# PORT-SEAM: `audited_sub_scores_json` / `axis_deltas_json` stay `text`
# (matching private's SQLite TEXT), NOT `jsonb`. is_audit_eligible's
# snapshot equality (#1799) and select_audit_candidates' SQL equality join
# both compare these as opaque strings; a bare Python str bound to a jsonb
# column has no implicit assignment cast in psycopg (DatatypeMismatch), and
# even routed through Jsonb() the value would come back out through
# jsonb's own key-order/whitespace-normalizing round trip -- neither
# preserves the byte-identical string-snapshot contract this module's
# fidelity anchors depend on. See _score_audits.py's module docstring for
# the calling contract this implies for future callers.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=18,  # PORT-SEAM: see docstring
    description="score_audits table (nightly scoring-audit results, unwired)",
    sql=[
        # PORT-SEAM: dialect-translated from the private SQLite DDL above;
        # verdict CHECK includes 'skipped' from the start (see docstring).
        """
        CREATE TABLE IF NOT EXISTS score_audits (
            id bigserial PRIMARY KEY,
            dedup_key text NOT NULL,
            audited_at timestamptz NOT NULL DEFAULT now(),
            model text NOT NULL,
            verdict text NOT NULL CHECK (verdict IN ('agree', 'dispute', 'skipped')),
            audited_sub_scores_json text NOT NULL,
            axis_deltas_json text,
            jd_quality_flag text,
            notes text
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_score_audits_dedup_key ON score_audits(dedup_key)",
    ],
)
