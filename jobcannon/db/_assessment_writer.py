"""PORTED from job_finder/db/_assessment_writer.py @ b1f69f3e10a452cc498527f830959b852108f5e9
(private job-cannon). Ledger L-0064.
# PORT-SEAM: this module ports only the ledger's literal "scoring tuple" --
# classification, sub_scores_json, fit_analysis, scoring_provider,
# scoring_model -- not the private module's full column set. Everything the
# private original also wrote is either already owned by another host
# writer, has no host schema to write into yet, or has no host consumer;
# each omission is called out at its own PORT-SEAM below and in
# jobcannon/db/migrations/m0015_postings_scoring_tuple.py's module
# docstring, which is the fuller record of this scoping decision.

Sole sanctioned writer of the scoring tuple
``(classification, sub_scores_json, fit_analysis, scoring_provider,
scoring_model)`` on ``postings``, mirroring the private original's role. No
call site wires this module yet -- like L-0077/L-0078's ``_scan_log.py`` /
``_scan_selection.py``, it lands unwired (a future host wiring
``score_and_persist_job``, per ``jobcannon.engine.job_scorer``'s own module
docstring, is the intended caller; no in-tree caller exists today).
# PORT-SEAM: private enforced the single-writer invariant with a CI grep
# gate (tests/test_assessment_writer_singleton.py) and an SQLite trigger
# (m078 I-05). This port has no call site to grep-guard yet (nothing else
# writes these columns -- confirmed via grep, they are new in m0015) and
# the I-05 equivalent is m0015's `postings_scoring_model_requires_classification`
# CHECK constraint (Postgres has no SQLite-style BEFORE-UPDATE trigger
# story as clean as a CHECK for this shape). A grep-guard test can be added
# once a second scoring-tuple caller exists to guard against.

Classification is ALWAYS derived here at persist time via
``derive_classification`` -- never taken from the LLM-emitted assessment.

# PORT-SEAM: private read ``legitimacy_note`` / ``enrichment_tier`` from the
# existing row unconditionally (its one fixed dev schema always carries
# both columns). This hosted schema does not have either column yet (grep-
# confirmed against every migration through m0015) -- they are
# ``derive_classification`` INPUTS, not part of the ledger's own "scoring
# tuple" output definition, so this migration does not add them. Rather
# than hardcode ``None`` for both (which would make the legitimacy-reject
# and enrichment-exhausted-low_signal branches silently unreachable
# forever, even after a future migration adds the columns), this writer
# probes ``information_schema.columns`` for their presence on every call
# (see ``_postings_optional_columns`` below, mirroring
# ``jobcannon.engine.ats_scanner._scan_log._scan_log_columns``'s live-
# lookup-not-cached rationale) and reads whichever are actually present.
# On the current schema both are absent, so both branches are dead today;
# a future migration adding either column re-arms the corresponding branch
# with no writer-side edit. A log line below records the derivation ran
# without one/both columns.

# PORT-SEAM: private's docstring also described a ``location_policy:
# LocationPolicy | None`` parameter whose ``effective_location_fit``
# overrides ``sub_scores["location_fit"]`` for classification only. No
# ``LocationPolicy`` class exists anywhere in this repo (grep-confirmed).
# This writer instead takes ``location_policy_verdict_json: str | None`` --
# the pre-serialized-JSON-string seam
# ``jobcannon.engine.classification.effective_sub_scores`` already
# established ahead of this writer landing (see that function's own
# docstring: "the helper is ported ahead of that writer"). No
# ``location_policy_*`` columns are added; the verdict JSON is consulted
# for classification and echoed into ``fit_analysis`` for audit purposes
# (mirroring the private original's ``fit_analysis_payload["location_policy"]``
# envelope) but is not itself persisted as a column.

# PORT-SEAM: private's ``JobAssessment`` carries a ``comp_fit_override``
# field this writer read into ``fit_analysis_payload``.
# ``jobcannon.engine.classification.JobAssessment`` (this repo's port of
# that dataclass) has no such field (grep/read-confirmed) -- the comp_fit
# forced-neutral precondition it recorded has no host counterpart yet, so
# that fit_analysis key is dropped rather than read off a field that does
# not exist.

# PORT-SEAM: private also wrote ``jd_content_verdict`` / ``jd_content_signal``
# (#1742 instrumentation) and stamped ``classification_rule_version`` /
# ``sub_score_sum`` / ``classification_rank`` on every call.
# ``jd_content_verdict`` / ``jd_content_signal`` are already owned by
# ``jobcannon/db/_jd_full.py::set_jd_full`` on this host (grep-confirmed:
# the only writer of those two columns in this tree) -- writing them here
# too would create the second-writer hazard the private CI grep gate
# exists to catch, so this port leaves them alone entirely.
# ``classification_rule_version`` has no host column (this migration does
# not add one -- see m0015's docstring); ``sub_score_sum`` /
# ``classification_rank`` are private-side sort-path materialization with
# no host consumer (see m0015's docstring). None of the three are read,
# written, or referenced below.

Pure write: does not commit on its own beyond ``pool.commit_unless_nested``,
matching ``_jobs.py`` / ``_companies.py`` / ``_jd_full.py``'s transaction-
boundary convention (works both through a bare pooled connection and nested
inside ``tests/host/conftest.py``'s ambient ``with conn.transaction():``).
"""

from __future__ import annotations

import json
import logging
from typing import Any  # PORT-SEAM: sqlite3 import dropped, see module docstring

from psycopg.types.json import Jsonb

from jobcannon.db.pool import commit_unless_nested
from jobcannon.engine.classification import (
    DEFAULT_APPLY_MEAN_FLOOR,
    DEFAULT_APPLY_MIN_STRONG_AXES,
    JobAssessment,
    derive_classification,
    effective_sub_scores,  # PORT-SEAM: replaces private LocationPolicy param, see module docstring
    get_effective_location_fit,
)
from jobcannon.engine.constants import SUB_SCORE_KEYS as _SUB_SCORE_KEYS

logger = logging.getLogger(__name__)

# Candidate derive_classification inputs that may or may not exist on the
# live `postings` table yet -- see the module docstring's PORT-SEAM above.
_OPTIONAL_INPUT_COLUMNS = (
    "legitimacy_note",
    "enrichment_tier",
)  # PORT-SEAM: replaces private _CLASSIFICATION_RANK_MAP (dropped, no host sub_score_sum/classification_rank columns)


def _postings_optional_columns(raw: Any) -> set[str]:
    """Return the subset of ``_OPTIONAL_INPUT_COLUMNS`` present on the live
    ``postings`` table. Live lookup on every call, deliberately not cached --
    same rationale as ``_scan_log_columns``: a schema migration can add one
    of these columns mid-process, and a cache keyed on connection identity
    could serve a stale answer to a later caller on a reused id. This
    module is host-dialect only (direct psycopg, never bare sqlite3 -- see
    ``jobcannon/db/_companies.py`` / ``_jobs.py``'s own docstrings), so
    unlike ``_scan_log_columns`` there is no dialect dispatch: only
    ``information_schema.columns`` is needed.
    """
    rows = raw.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = ANY(%s)",
        ("postings", list(_OPTIONAL_INPUT_COLUMNS)),
    ).fetchall()
    return {row["column_name"] for row in rows}


def persist_job_assessment(
    conn: Any,  # PORT-SEAM: no sqlite3 dialect on this host (psycopg only)
    dedup_key: str,
    assessment: JobAssessment,
    provider: str | None = None,
    model: str | None = None,
    *,
    config: dict | None = None,
    location_policy_verdict_json: str
    | None = None,  # PORT-SEAM: replaces private location_policy/commit params, see module docstring
) -> str | None:
    """Persist a v3.0 JobAssessment's scoring tuple onto ``postings``.

    Writes ``classification`` (derived at persist time -- never taken from
    *assessment*), ``sub_scores_json``, ``fit_analysis``, ``scoring_provider``,
    ``scoring_model``. See the module docstring for the columns this port
    deliberately does not write. (# PORT-SEAM: this intro is condensed from
    private's longer docstring -- see the module docstring's own PORT-SEAM
    blocks for the full list of omissions and rationale.)

    Args:
        conn: pooled connection (``.raw`` unwrapped) or a bare psycopg
            connection, matching ``_jobs.py`` / ``_companies.py``'s dispatch.
            (# PORT-SEAM: private said "Open sqlite3 connection".)
        dedup_key: the posting's dedup key.
        assessment: JobAssessment with sub_scores + rationale.
        provider: cascade-attribution string; None preserves the existing
            ``scoring_provider`` value (COALESCE).
        model: model identifier; None preserves the existing ``scoring_model``.
        config: optional config dict. When provided, reads
            ``scoring.low_signal_jd_chars`` / ``scoring.apply_mean_floor`` /
            ``scoring.apply_min_strong_axes``; otherwise engine defaults apply.
        location_policy_verdict_json: pre-serialized LocationPolicy verdict
            JSON string, or None. See the module docstring's PORT-SEAM for
            why this replaces private's ``LocationPolicy`` parameter. (# PORT-SEAM:
            private's ``commit: bool = True`` param is also dropped -- this
            port always commits via commit_unless_nested, matching
            _jobs.py/_companies.py/_jd_full.py; no caller needs batch-commit
            deferral yet.)

    Returns:
        The derived classification just written, or None when *dedup_key*
        matched no ``postings`` row (silent no-op, matching Postgres
        UPDATE-no-match semantics). (# PORT-SEAM: private's longer Returns
        paragraph described a since-superseded orchestrator emission path;
        condensed here.)

    Raises:
        ValueError: propagated from ``derive_classification`` on a malformed
            ``assessment.sub_scores`` dict. (# PORT-SEAM: condensed from private's
            longer Raises paragraph.)
    """
    raw = conn.raw if hasattr(conn, "raw") else conn

    present_optional = _postings_optional_columns(raw)
    read_cols = [c for c in _OPTIONAL_INPUT_COLUMNS if c in present_optional]
    missing = [c for c in _OPTIONAL_INPUT_COLUMNS if c not in present_optional]
    if missing:
        logger.debug(
            "persist_job_assessment: deriving classification for dedup_key=%s "
            "without host column(s) %s -- corresponding derive_classification "
            "branch(es) are unreachable until a future migration adds them",
            dedup_key,
            missing,
        )

    select_list = ", ".join(["COALESCE(LENGTH(jd_full), 0) AS jd_len", *read_cols])
    row = raw.execute(
        f"SELECT {select_list} FROM postings WHERE dedup_key = %s",  # PORT-SEAM: column list built from a hardcoded candidate allow-list, not user input
        (dedup_key,),
    ).fetchone()  # PORT-SEAM: raw.execute(...).fetchone() replaces cursor-based cur.execute()/cur.fetchone() (no separate cursor object with psycopg's connection-level .execute())
    if row is None:
        return None  # PORT-SEAM: matches Postgres UPDATE-no-match semantics (private: SQLite)
    jd_full_length = row["jd_len"] or 0
    legitimacy_note = row["legitimacy_note"] if "legitimacy_note" in read_cols else None
    enrichment_tier = (
        row["enrichment_tier"] if "enrichment_tier" in read_cols else None
    )  # PORT-SEAM: private's #1742 jd_content_verdict stamping dropped here -- owned by _jd_full.py::set_jd_full on this host, see module docstring

    threshold = 1500
    apply_mean_floor = DEFAULT_APPLY_MEAN_FLOOR
    apply_min_strong_axes = DEFAULT_APPLY_MIN_STRONG_AXES
    if config is not None:
        scoring_cfg = config.get("scoring") or {}
        threshold = int(scoring_cfg.get("low_signal_jd_chars", 1500))
        apply_mean_floor = float(scoring_cfg.get("apply_mean_floor", DEFAULT_APPLY_MEAN_FLOOR))
        apply_min_strong_axes = int(
            scoring_cfg.get("apply_min_strong_axes", DEFAULT_APPLY_MIN_STRONG_AXES)
        )

    sub_scores_for_classification = effective_sub_scores(
        assessment.sub_scores, location_policy_verdict_json
    )

    fit_analysis_payload: dict[str, Any] = {
        **assessment.rationale
    }  # PORT-SEAM: pre-initialized for both branches, replacing private's duplicated if/else assignment, see module docstring
    if location_policy_verdict_json is not None:
        effective_location_fit = get_effective_location_fit(location_policy_verdict_json)
        try:
            verdict_dict = json.loads(location_policy_verdict_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            verdict_dict = None
        fit_analysis_payload["location_policy"] = {
            "verdict": verdict_dict,
            "effective_location_fit": effective_location_fit,
        }
    # PORT-SEAM: private's else-branch (no-policy defaults) and its #1969
    # comp_fit_override handling are both dropped here -- fit_analysis_payload
    # is pre-initialized above for both branches, and
    # jobcannon.engine.classification.JobAssessment has no comp_fit_override
    # field on this host (see module docstring).

    final_classification = derive_classification(
        sub_scores_for_classification,
        legitimacy_note,
        enrichment_tier=enrichment_tier,
        jd_full_length=jd_full_length,
        low_signal_threshold=threshold,
        apply_mean_floor=apply_mean_floor,
        apply_min_strong_axes=apply_min_strong_axes,
        degenerate=assessment.degenerate,
    )

    ordered_sub_scores = {  # PORT-SEAM: stable key order for diff-friendliness (private's comment, kept as this marker)
        k: assessment.sub_scores[k] for k in _SUB_SCORE_KEYS if k in assessment.sub_scores
    }

    with raw.transaction():
        raw.execute(
            "UPDATE postings SET "  # PORT-SEAM: single UPDATE replaces private's dynamic set_clauses/params list-building (materialized rank/sum + location_policy_* columns dropped, see module docstring)
            "classification = %s, "
            "sub_scores_json = %s, "
            "fit_analysis = %s, "
            "scoring_provider = COALESCE(%s, scoring_provider), "
            "scoring_model = COALESCE(%s, scoring_model) "
            "WHERE dedup_key = %s",
            (
                final_classification,
                Jsonb(ordered_sub_scores),
                Jsonb(fit_analysis_payload),
                provider or assessment.provider,
                model,
                dedup_key,
            ),
        )
    commit_unless_nested(raw)
    return final_classification


def invalidate_job_score(conn: Any, dedup_key: str) -> bool:
    """Clear a posting's scoring tuple so it re-enters the scoring cohort.

    Nulls ``classification``, ``sub_scores_json``, ``fit_analysis``,
    ``scoring_model`` atomically -- ``classification`` and ``scoring_model``
    null together in the same statement so m0015's
    ``postings_scoring_model_requires_classification`` CHECK never fires
    against this UPDATE. ``scoring_provider`` is deliberately left intact
    (matches the private original's rationale: it is re-stamped via
    COALESCE on the next ``persist_job_assessment`` call, and this host has
    no I-03-equivalent CHECK requiring it to travel with ``classification``).
    # PORT-SEAM: private additionally nulled ``jd_content_verdict`` /
    # ``jd_content_signal`` / ``jd_adjudicated_version`` /
    # ``classification_rule_version`` / ``sub_score_sum`` /
    # ``classification_rank`` here. The first three are owned by
    # ``_jd_full.py::set_jd_full`` on this host (see module docstring) --
    # touching them here would be a second writer. The last three have no
    # host column (see module docstring). None are referenced.

    Returns:
        True if a row was matched and its scoring tuple cleared; False if
        *dedup_key* matched no row.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    with raw.transaction():
        cursor = raw.execute(
            "UPDATE postings SET "
            "classification = NULL, "
            "sub_scores_json = NULL, "
            "fit_analysis = NULL, "
            "scoring_model = NULL "
            "WHERE dedup_key = %s",
            (dedup_key,),
        )
        rowcount = cursor.rowcount  # PORT-SEAM: condensed invalidate_job_score body -- see this function's own docstring PORT-SEAM note and the module docstring for the dropped jd_content_verdict/rule_version/rank/sum columns
    commit_unless_nested(raw)
    return rowcount > 0
