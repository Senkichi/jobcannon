"""Structural axes: four zero-LLM per-posting scores computed at ingest.

Freshness / seniority-clarity / comp-transparency / jd-quality — all derived
from data already on the `postings` row (no model call, no network fetch).
Persisted into `postings.structural_axes` (jsonb) alongside
`structural_scoring_method` / `structural_scored_at` (m0001, Wave-1 storage;
this package is the first writer — no migration needed for Wave-2 PR 7).

This is Option 1 only (pure host code + one additive engine wrapper,
`jobcannon.engine.jd_content_contract.has_recognizable_jd_shape`).
Exemplar-similarity (Option 2) is deferred to PR 7b.
"""

from __future__ import annotations

from typing import Any, Mapping

from psycopg.types.json import Jsonb

from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency
from jobcannon.host.structural_axes.freshness import score_freshness
from jobcannon.host.structural_axes.jd_quality import score_jd_quality
from jobcannon.host.structural_axes.seniority import score_seniority_clarity

STRUCTURAL_SCORING_METHOD_V1 = "rules_v1"

__all__ = [
    "STRUCTURAL_SCORING_METHOD_V1",
    "score_posting",
    "score_pending_structural_axes",
]


def score_posting(row: Mapping[str, Any], sibling_jds: list[str]) -> dict:
    return {
        "freshness": score_freshness(
            row["posted_date"],
            row["posted_date_precision"],
            row["last_seen"],
            row["is_stale"],
            row["expiry_status"],
        ),
        "seniority_clarity": score_seniority_clarity(row["title"]),
        "comp_transparency": score_comp_transparency(
            row["salary_min"], row["salary_max"], row["jd_full"]
        ),
        "jd_quality": score_jd_quality(row["jd_full"], sibling_jds),
    }


def score_pending_structural_axes(conn: Any, config: Any, *, batch_size: int = 500) -> int:
    """Batch-score postings not yet scored under STRUCTURAL_SCORING_METHOD_V1.

    `conn` may be a bare psycopg connection (as `tests/host/conftest.py`'s
    `db_conn` fixture yields) or an `EngineCompatConnection` (as
    `jobcannon.db.connection_factory()` yields) — unwrapped the same way
    `jobcannon.db._companies` / `_jobs` / `_jd_full` do, so this host-native
    `%s`-placeholder SQL never routes through `EngineCompatConnection`'s
    qmark-translation shim (`jobcannon.db.compat.engine_sql_to_host`), which
    is built for engine-authored `?`-placeholder SQL and would otherwise
    double-escape our literal `%s` placeholders.

    `config` is accepted for call-site parity with other engine-facing
    entry points but is currently unused (no tunables yet).

    Uses `IS DISTINCT FROM` (not `!=`) so a never-scored row (NULL method)
    is picked up identically to a row scored under a stale method value —
    the same versioned re-sweep idiom used elsewhere in this codebase
    (JD_CONTENT_VERSION), so a future `STRUCTURAL_SCORING_METHOD` bump
    re-arms scoring for the whole corpus without a separate backfill path.

    The outer pending SELECT carries NO `jd_full IS NOT NULL` gate — every
    not-yet-scored posting is eligible, JD or no JD. `freshness` and
    `seniority_clarity` are derived from `title`/dates that are always present
    on the row; `comp_transparency` and `jd_quality` degrade gracefully to
    their no-JD verdict (`False` / `0.0`) rather than requiring one. Only the
    sibling-JD subquery below keeps its own `jd_full IS NOT NULL` filter —
    those rows feed `jd_quality`'s boilerplate comparison, which needs JD text
    to compare against.

    Concurrency semantics change (PR-6 debt, same shape as
    jobcannon.host.embeddings.embed_pending_postings): the whole claim+score+
    write cycle is now ONE batch transaction with `FOR UPDATE SKIP LOCKED` row
    claiming, so concurrent sweeps (N>1 workers) PARTITION the pending
    backlog instead of racing to double-score or blocking on each other's
    rows. Commit is batch-atomic — a crash mid-batch loses at most that
    batch's work, and the versioned re-sweep self-heals it next run.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    with raw.transaction():
        pending = raw.execute(
            "SELECT id, title, jd_full, salary_min, salary_max, posted_date, "
            "posted_date_precision, is_stale, expiry_status, company_id, last_seen "
            "FROM postings WHERE structural_scoring_method IS DISTINCT FROM %s "
            "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
            (STRUCTURAL_SCORING_METHOD_V1, batch_size),
        ).fetchall()

        for row in pending:
            siblings = raw.execute(
                "SELECT jd_full FROM postings WHERE company_id = %s AND id <> %s "
                "AND jd_full IS NOT NULL ORDER BY last_seen DESC LIMIT 5",
                (row["company_id"], row["id"]),
            ).fetchall()
            axes = score_posting(row, [s["jd_full"] for s in siblings])
            raw.execute(
                "UPDATE postings SET structural_axes = %s, structural_scoring_method = %s, "
                "structural_scored_at = now() WHERE id = %s",
                (Jsonb(axes), STRUCTURAL_SCORING_METHOD_V1, row["id"]),
            )
    return len(pending)
