"""set_jd_full — Postgres port of the sole sanctioned jd_full writer.

Same 4-layer gate chain as the private original, with the two content gates
imported straight from the ENGINE (they were ported in Phase 1A):
  1. empty-text short-circuit
  2. HTML normalization (strip to text only when an HTML signal is present)
  3. I-13 density gate: jobcannon.engine.jd_content_contract._is_jd_junk
  4. I-17 content contract: jobcannon.engine.jd_content_contract.jd_content_reject
Returns True on write, False on any gate hit (no write).

Divergence from the private original, deliberate and Wave-1-scoped: no
score-invalidation side effect on content change — the hosted schema has no
per-posting LLM score tuple yet (structural axes are Wave-2 work and are
recomputed at ingest, not invalidated here). Revisit when owner-fit scoring
lands (Phase 2).

JD-content verdict persistence (D5 / #152, m0009): after a successful write,
this stamps ``postings.jd_content_verdict`` / ``jd_content_signal`` by
running ``jd_content_contract.classify_jd_content`` ONCE against the stored
body — the single point where that cost is paid, mirroring the private
original's ``set_jd_full`` (see ``job_finder/db/_jd_full.py``, read-only
reference). ``jobcannon.engine.job_scorer.scoring_precheck`` then reads this
persisted column instead of recomputing per row. Stamped whenever the text
changed, or whenever no verdict is on record yet (self-healing a legacy row
the moment it happens to be re-touched). CAS-guarded on
``WHERE dedup_key = %s AND jd_full = %s`` (mirroring the private original)
because the jd_full write and the verdict stamp are two separate statements:
a concurrent writer could interleave a second ``set_jd_full`` call between
them and overwrite ``jd_full`` again before this UPDATE lands. A guard miss
leaves the verdict untouched (fail-open, per ``scoring_precheck``'s existing
semantics) rather than stamping a mismatched one.

Watermark invalidation (design decision, #152): the private original's
``invalidate_job_score`` nulls ``jd_adjudicated_version`` on a content
change as one of several score-invalidation side effects; this hosted
engine has no ``invalidate_job_score`` counterpart at all (no per-posting
LLM score tuple — see the divergence above), so rather than build one just
to hold this single field, ``jd_adjudicated_version`` is nulled inline here,
in the same CAS-guarded UPDATE as the fresh verdict stamp, whenever the
stored content actually changed. This re-arms the D5 gate on any
content-changing re-fetch without inventing score-invalidation machinery
this Wave-1 schema has no other use for.

Row-projection / SQL-mirror decision (#152): hosts call
``jobcannon.engine.job_scorer.scoring_precheck`` directly against a full
``SELECT * FROM postings ...`` row dict — this repo has no
``JOBS_ALL_COLUMNS``-style explicit projection to update (grep confirms the
only column-enumerated postings reads are single-purpose, e.g. this
module's own ``unresolved_reasons`` lookups) and ships no
``count_scorable``/``exclusion_filter``-style SQL mirror of the gate's
conditions, matching the current documented position in
``job_scorer.py``'s own docstring. Should a host later need a fast
SQL-only "N unscored" count without loading full rows, mirror
``scoring_precheck``'s exact conditions in one place, next to that host's
own query, rather than duplicating the logic ad hoc.

A second Wave-1 divergence, same shape: the private original also resets a
terminal ``enrichment_tier`` to NULL when a truncated body is rejected, so
the row re-enters its multi-tier resumable enrichment pipeline. The hosted
schema has no ``enrichment_tier`` column and no resumable-tier pipeline
writing back to ``postings`` in Wave 1 (``jobcannon.engine.enrichment_states``
is ported for its tier vocabulary only, consumed as a plain parameter by
``classification.py``, never persisted) — so only the reason-code append
below is ported; the tier reset has no column to act on.

Transaction-boundary note (recorded port deviation, matches _companies.py /
_jobs.py): the write commits via pool.commit_unless_nested() rather than a
bare raw.commit() call, so this also works when `conn` is already inside an
ambient `with conn.transaction():` block (tests/host/conftest.py's db_conn
fixture) — psycopg3 forbids explicit commit() there. See that helper's
docstring for why a naive `with raw.transaction():` wrapper does NOT
substitute for a real commit here (verified empirically: it degrades to a
savepoint whenever the connection already carries an open, non-Transaction-
managed transaction from an earlier bare statement — the common case, since
this is called right after the engine's own bare
`SELECT jd_full FROM jobs ...` read in _run.py).

The UPDATE itself is additionally wrapped in its own `with raw.transaction():`
block purely for SAVEPOINT-based recovery (matches _jobs.py / _companies.py):
if the write raises, that block's __exit__ rolls back to the savepoint and
re-raises, leaving the connection usable for the caller's next statement
instead of stuck in Postgres's aborted-transaction state. commit_unless_nested()
still runs immediately after the block, unchanged.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from psycopg.types.json import Jsonb

from jobcannon.db._unresolved_reasons import append_reason, remove_reasons
from jobcannon.db.pool import commit_unless_nested
from jobcannon.engine.description_formatter import strip_html_to_text
from jobcannon.engine.jd_content_contract import (
    JD_CONTENT_REASON_CODES,
    _is_jd_junk,
    classify_jd_content,
    jd_content_reject,
)

logger = logging.getLogger(__name__)

_HTML_SIGNAL_RE = re.compile(r"<\s*(p|div|br|li|ul|span|h\d)\b", re.IGNORECASE)


def set_jd_full(
    conn: Any,
    dedup_key: str,
    text: str | None,
    *,
    source: str,
    title: str | None = None,
    config: dict | None = None,
) -> bool:
    """Store a JD body on the posting, gated by the junk and content contracts.

    `config` (keyword-only) pins the private chokepoint's full signature and
    is threaded into the content-gate call, so ``enrichment.jd_full``
    thresholds govern the truncation check for any caller that supplies
    config. The scan-path caller (``ats_scanner/_run.py``) does not supply it
    — faithful to the private call site — so that path runs on the engine
    defaults today.

    Content-contract side effects (parity with the private original's I-18
    fix): a body rejected by ``jd_content_reject`` has its reason code
    (``jd_full_offsite`` / ``jd_full_expired`` / ``jd_full_truncated``)
    appended to ``unresolved_reasons`` so the row is flagged for review
    instead of silently staying ``jd_full IS NULL``. A successful write
    clears any stale I-18 reason codes from ``unresolved_reasons`` in the
    same UPDATE as the ``jd_full`` write. See this module's docstring for the
    ``enrichment_tier`` reset the private original also performs, which has
    no column to act on in the Wave-1 hosted schema.

    JD-content verdict persistence (D5 / #152) and jd_adjudicated_version
    invalidation: see this module's docstring. Both happen after the
    ``jd_full`` UPDATE has committed, in a second, CAS-guarded UPDATE.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    if not text:
        return False
    if _HTML_SIGNAL_RE.search(text):
        text = strip_html_to_text(text)
    if _is_jd_junk(text):
        logger.warning("set_jd_full: junk-gated [source=%s] prefix=%r", source, text.strip()[:60])
        return False
    rejection = jd_content_reject(text, title, config)
    if rejection is not None:
        reason, signal = rejection
        logger.warning(
            "set_jd_full: content-gated [source=%s] reason=%s signal=%s prefix=%r",
            source,
            reason,
            signal,
            text.strip()[:60],
        )
        _record_jd_content_reject(raw, dedup_key, reason)
        return False
    existing = raw.execute(
        "SELECT jd_full, unresolved_reasons, company, jd_content_verdict "
        "FROM postings WHERE dedup_key = %s",
        (dedup_key,),
    ).fetchone()
    existing_jd = existing["jd_full"] if existing is not None else None
    existing_company = existing["company"] if existing is not None else None
    existing_verdict = existing["jd_content_verdict"] if existing is not None else None
    content_changed = text != existing_jd
    new_reasons = remove_reasons(
        existing["unresolved_reasons"] if existing is not None else None,
        list(JD_CONTENT_REASON_CODES),
    )
    with raw.transaction():
        raw.execute(
            "UPDATE postings SET jd_full = %s, unresolved_reasons = %s WHERE dedup_key = %s",
            (text, Jsonb(new_reasons), dedup_key),
        )
    commit_unless_nested(raw)

    # Persist the jd-content verdict at this single write chokepoint (D5 /
    # #152) — see the module docstring. Must run AFTER the jd_full UPDATE
    # above (never before): the verdict describes the body just written, not
    # whatever was there before.
    if content_changed or existing_verdict is None:
        jd_result = classify_jd_content(text, title, existing_company, config)
        if content_changed:
            # A changed body invalidates any prior adjudication — re-arm the
            # D5 gate so scoring_precheck cannot keep coasting on an
            # adjudicated_version stamped against the OLD body.
            stamp_sql = (
                "UPDATE postings SET jd_content_verdict = %s, jd_content_signal = %s, "
                "jd_adjudicated_version = NULL WHERE dedup_key = %s AND jd_full = %s"
            )
        else:
            stamp_sql = (
                "UPDATE postings SET jd_content_verdict = %s, jd_content_signal = %s "
                "WHERE dedup_key = %s AND jd_full = %s"
            )
        with raw.transaction():
            raw.execute(
                stamp_sql,
                (jd_result.verdict.value, jd_result.signal, dedup_key, text),
            )
        commit_unless_nested(raw)
    return True


def _record_jd_content_reject(raw: Any, dedup_key: str, reason: str) -> None:
    """Append a jd-content reject reason to ``postings.unresolved_reasons``.

    Mirrors the private original's ``_record_jd_content_reject``, minus the
    ``enrichment_tier`` reset for ``JD_TRUNCATED`` — the hosted schema has no
    such column in Wave 1 (see this module's docstring).
    """
    if reason not in JD_CONTENT_REASON_CODES:
        return
    existing = raw.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    if existing is None:
        return
    new_reasons = append_reason(existing["unresolved_reasons"], reason)
    with raw.transaction():
        raw.execute(
            "UPDATE postings SET unresolved_reasons = %s WHERE dedup_key = %s",
            (Jsonb(new_reasons), dedup_key),
        )
    commit_unless_nested(raw)
