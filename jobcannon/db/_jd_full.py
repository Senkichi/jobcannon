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

JD-content verdict persistence (D5 / #152, m0009): after the gates above
pass, this stamps ``postings.jd_content_verdict`` / ``jd_content_signal`` by
running ``jd_content_contract.classify_jd_content`` ONCE against the body
about to be stored — the single point where that cost is paid, mirroring
the private original's ``set_jd_full`` (see ``job_finder/db/_jd_full.py``,
read-only reference) in spirit but NOT in statement shape (see the #184 note
below). ``jobcannon.engine.job_scorer.scoring_precheck`` then reads this
persisted column instead of recomputing per row.

Atomic single-UPDATE write (#184, deliberate deviation from both the
pre-fix hosted code and the private original): ``jd_full``,
``unresolved_reasons``, ``jd_content_verdict``, ``jd_content_signal``, and
``jd_adjudicated_version`` are all set by ONE UPDATE statement, in ONE
transaction. The pre-#184 shape ran the ``jd_full`` write and the verdict
stamp as two separately-committed UPDATEs, CAS-guarded on
``WHERE dedup_key = %s AND jd_full = %s`` so a concurrent writer landing
between them would make the stamp UPDATE's WHERE miss and leave the verdict
untouched. That CAS guard protected the wrong thing: it stopped a
mismatched verdict from being *written*, but a reader polling in the gap
between the two commits could still observe the NEW ``jd_full`` sitting
next to the OLD (already-committed) verdict for however long that gap
lasted — and critically, ``scoring_precheck`` treats a persisted CLEAN
verdict as "no adjudication needed" regardless of ``jd_adjudicated_version``
(see ``job_scorer.py``), so a stale CLEAN reader observation silently
bypassed the D5 gate entirely. Nulling ``jd_adjudicated_version`` in the
FIRST statement alone (the "naive fix") does not close this: the window is
about the ``jd_content_verdict`` column itself disagreeing with the stored
text, not about ``jd_adjudicated_version``.

Folding everything into one UPDATE makes that window unrepresentable rather
than detecting it after the fact: ``classify_jd_content`` is pure Python
(no DB/IO — verified by reading its full call chain), so its result for the
NEW text can be computed BEFORE any write and handed to the same statement
that writes the text. Whether the content actually changed, and therefore
whether to apply the new verdict / null the watermark, is decided INSIDE
the UPDATE via SQL ``CASE ... WHEN jd_full IS DISTINCT FROM %(text)s``,
which Postgres evaluates against the row's live pre-update value at
UPDATE-execution time — not against a value read earlier in a separate
SELECT. That removes the specific TOCTOU the old CAS guard existed to
catch (a stale Python-side ``content_changed`` decision), so there is
nothing left for a WHERE-clause CAS to protect: with one statement, a
reader on any other connection either sees the full pre-image or the full
post-image, never a mix, by Postgres's normal row-visibility rules — no
extra guard required. (Proven by a real two-connection race test —
``tests/host/test_jd_full.py::test_race_two_connections_never_observe_torn_jd_full_and_verdict`` —
which fails against the pre-#184 two-statement shape and passes against
this one.) The write remains unconditional on ``WHERE dedup_key = %s``, same
as the pre-#184 first statement, so under a genuine same-row concurrent
write the later commit wins outright and is fully self-consistent (last-
writer-wins) for ``jd_full`` / ``unresolved_reasons`` / ``jd_content_verdict``
/ ``jd_content_signal`` / ``jd_adjudicated_version`` — all five columns
whose new value is decided by the SET clause itself, against the live
pre-update row. This is stricter than the old fail-open behavior, which on
a CAS miss left the loser's verdict-stamp UPDATE matching 0 rows — the
persisted verdict simply kept whatever value it already had (NULL for a
fresh row; a STALE prior verdict once a future caller re-touches an
already-populated one), rather than being nulled. ``unresolved_reasons``
used to be the one column this guarantee did NOT cover — its new value was
computed from an earlier SELECT rather than a SQL expression against the
live row — but #217 closed that gap: the SET clause now derives it via a
``jsonb_array_elements_text``/filter set-difference (mirroring
``_unresolved_reasons.remove_reasons``'s malformed-value tolerance and
no-op-on-absent-reason semantics) evaluated at UPDATE-execution time, the
same as the other four columns. ``_record_jd_content_reject`` below (the
function this column's other writer races against) received the same
treatment, as a single atomic UPDATE with no preceding SELECT at all.

``classify_jd_content`` is called unconditionally (its result feeds the SQL
CASE, which decides whether to apply it) rather than gated on a
Python-side "did anything change" check — the classifier is a cheap
deterministic regex/heuristic pass, and the earlier per-call skip existed
to avoid recomputing this cost in *readers* (``scoring_precheck`` reads the
persisted column instead of recomputing per row), not to avoid a second
pure-Python call inside this already-paid write chokepoint.

Watermark invalidation (design decision, #152): the private original's
``invalidate_job_score`` nulls ``jd_adjudicated_version`` on a content
change as one of several score-invalidation side effects; this hosted
engine has no ``invalidate_job_score`` counterpart at all (no per-posting
LLM score tuple — see the divergence above), so rather than build one just
to hold this single field, ``jd_adjudicated_version`` is nulled inline here,
in the same atomic UPDATE as the fresh verdict stamp (see the #184 note
above), whenever the stored content actually changed. This re-arms the D5
gate on any
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

from jobcannon.db.pool import commit_unless_nested
from jobcannon.engine.description_formatter import html_to_plain_text
from jobcannon.engine.jd_content_contract import (
    JD_CONTENT_REASON_CODES,
    _is_jd_junk,
    classify_jd_content,
    jd_content_reject,
)

logger = logging.getLogger(__name__)

# HTML-signal regex: detects escaped tags (&lt;), closing tags (</...>), or
# common opening block tags. Ported verbatim from the private original
# (job_finder/db/_jd_full.py's _HTML_SIGNAL_RE, READ-ONLY reference) --
# #216: the prior hosted pattern (`<\s*(p|div|br|li|ul|span|h\d)\b`) missed
# entity-encoded (`&lt;`) and closing-tag-only (`</tag>` with no matching
# earlier opening tag) bodies, so those bypassed normalization here and
# diverged from the private engine on the same input. Plain prose that
# merely contains a stray `<` (e.g. "earn < $100k") is not matched because a
# word char or `/` is required immediately after the `<`.
_HTML_SIGNAL_RE = re.compile(
    r"(&lt;|</([\w]+)>|<p[\s>]|<div|<br|<li|<ul|<h[1-6])",
    re.IGNORECASE,
)


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
    same UPDATE as the ``jd_full`` write; like that UPDATE's other four
    columns, the new ``unresolved_reasons`` value is decided by a SQL
    expression against the row's LIVE value at UPDATE-execution time, not a
    Python value computed from an earlier SELECT (#217 fix), so it shares
    their same-statement self-consistency guarantee -- a concurrent writer
    to that column (e.g. ``_record_jd_content_reject`` below, itself now a
    single atomic UPDATE with no preceding SELECT) can no longer have its
    committed change clobbered by this one landing after it, or vice versa.
    See this module's docstring for the ``enrichment_tier`` reset the private
    original also performs, which has no column to act on in the Wave-1
    hosted schema.

    JD-content verdict persistence (D5 / #152) and jd_adjudicated_version
    invalidation: see this module's docstring (#184) -- all of it, including
    the ``jd_full`` write itself, lands in ONE UPDATE so no reader can ever
    observe the text and its verdict disagreeing about which body they
    describe.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    if not text:
        return False
    if _HTML_SIGNAL_RE.search(text):
        # #216: html_to_plain_text (not strip_html_to_text directly) --
        # entity-encoded input (the `&lt;` signal this regex now also
        # catches) must be unescaped BEFORE the tag-stripping regexes run,
        # or literal `<p>`/`</p>` tags survive decoded-but-unstripped in the
        # output (empirically confirmed: strip_html_to_text("&lt;p&gt;Hello
        # &lt;/p&gt;World") == "<p>Hello</p>World"). html_to_plain_text does
        # `_html.unescape(raw)` first, then delegates to strip_html_to_text
        # -- a no-op reordering for already-plain-tag input, so this is a
        # strict widening, not a behavior change, for the pre-existing
        # opening-tag-shaped bodies. Matches the private original's
        # `normalize_jd`, which calls `html_to_plain_text` for the same
        # reason -- the brief's call-site note assumed the prior
        # `strip_html_to_text` call was already equivalent; it is not, so
        # this call site changes too (see PR body).
        text = html_to_plain_text(text)
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
        "SELECT company FROM postings WHERE dedup_key = %s",
        (dedup_key,),
    ).fetchone()
    if existing is None:
        # No posting row for this dedup_key -- nothing to write. Without
        # this, the UPDATE below would silently affect 0 rows (Postgres
        # raises nothing) and this function would return True -- a false
        # "wrote" signal for a no-op, plus a wasted classify_jd_content call.
        return False
    # Pure Python, no DB access (#184) -- computed for the NEW text before
    # any write, using the row's `company` as read above. `company` is not a
    # decision input for WHETHER this write is applied (the SQL CASE below
    # ignores it entirely) but it IS a grounding input for WHAT verdict gets
    # stamped: classify_jd_content uses it to score title/company overlap, so
    # a concurrent writer changing `company` between this SELECT and the
    # UPDATE below can make the stamped verdict reflect stale grounding. This
    # is a narrower, still-open TOCTOU distinct from #217 (closed below for
    # `unresolved_reasons`): no live caller races `company` writes against
    # this path today, so it is left as a Python value rather than folded
    # into the UPDATE as a live-row SQL input -- revisit if that changes.
    # Called unconditionally; the UPDATE's SQL CASE below decides whether the
    # result is actually applied. See the module docstring for why an
    # external CAS guard is unnecessary once the write is a single statement.
    jd_result = classify_jd_content(text, title, existing["company"], config)
    with raw.transaction():
        cur = raw.execute(
            "UPDATE postings SET "
            "jd_full = %(text)s, "
            # #217 fix: unresolved_reasons is now decided by a SQL
            # expression against the row's LIVE value at UPDATE-execution
            # time, not a Python value computed from an earlier SELECT --
            # mirrors `_unresolved_reasons.remove_reasons`'s set-difference
            # semantics (malformed/non-array values normalized to '[]' via
            # the jsonb_typeof guard; removing an absent reason is a no-op).
            "unresolved_reasons = COALESCE("
            "(SELECT jsonb_agg(elem ORDER BY ord) "
            "FROM jsonb_array_elements_text("
            "CASE WHEN jsonb_typeof(unresolved_reasons) = 'array' "
            "THEN unresolved_reasons ELSE '[]'::jsonb END"
            ") WITH ORDINALITY AS t(elem, ord) "
            "WHERE elem <> ALL(%(remove_codes)s)"
            "), '[]'::jsonb), "
            "jd_content_verdict = CASE WHEN jd_full IS DISTINCT FROM %(text)s "
            "OR jd_content_verdict IS NULL THEN %(verdict)s ELSE jd_content_verdict END, "
            "jd_content_signal = CASE WHEN jd_full IS DISTINCT FROM %(text)s "
            "OR jd_content_verdict IS NULL THEN %(signal)s ELSE jd_content_signal END, "
            # No `OR jd_content_verdict IS NULL` branch here, unlike the two
            # CASEs above: this column tracks whether the CURRENT jd_full has
            # been adjudicated, not whether a verdict string is present, so
            # it must only reset on an actual content change. A NULL verdict
            # with unchanged content is the self-heal branch (pinned by
            # test_unchanged_content_with_no_verdict_stamps_without_nulling_adjudicated_version)
            # and must NOT null an adjudication that already covers this
            # exact text -- don't "normalize" this to match the other two.
            "jd_adjudicated_version = CASE WHEN jd_full IS DISTINCT FROM %(text)s "
            "THEN NULL ELSE jd_adjudicated_version END "
            "WHERE dedup_key = %(dedup_key)s",
            {
                "text": text,
                "remove_codes": list(JD_CONTENT_REASON_CODES),
                "verdict": jd_result.verdict.value,
                "signal": jd_result.signal,
                "dedup_key": dedup_key,
            },
        )
    commit_unless_nested(raw)
    if cur.rowcount == 0:
        # The SELECT above found a row, but a concurrent DELETE / re-upsert
        # removed it before this UPDATE ran -- the write matched nothing.
        # Report the honest "did not write" signal instead of a false True.
        # Defensive: no code path in this repo issues `DELETE FROM postings`
        # today (grep-confirmed), so this branch is not known to be live.
        logger.warning(
            "set_jd_full: UPDATE matched 0 rows [source=%s dedup_key=%s] -- "
            "row deleted between SELECT and UPDATE?",
            source,
            dedup_key,
        )
        return False
    return True


def _record_jd_content_reject(raw: Any, dedup_key: str, reason: str) -> None:
    """Append a jd-content reject reason to ``postings.unresolved_reasons``.

    Mirrors the private original's ``_record_jd_content_reject``, minus the
    ``enrichment_tier`` reset for ``JD_TRUNCATED`` — the hosted schema has no
    such column in Wave 1 (see this module's docstring).

    Atomic single-statement append (#217 fix): the dedupe/malformed-value
    tolerance semantics of ``_unresolved_reasons.append_reason`` are mirrored
    in a SQL expression decided against the row's LIVE ``unresolved_reasons``
    value at UPDATE-execution time, rather than a Python value computed from
    an earlier SELECT — so a concurrent writer to the same column (e.g. a
    same-row ``set_jd_full`` success clearing stale reason codes) can never
    have its already-committed change clobbered by this UPDATE landing
    after it, and vice versa. No pre-read is needed at all: a WHERE-clause
    miss (no row for ``dedup_key``, or one deleted concurrently) just leaves
    the UPDATE matching 0 rows, a silent no-op in Postgres — there is no
    longer a SELECT-then-UPDATE gap here to race.
    """
    if reason not in JD_CONTENT_REASON_CODES:
        return
    with raw.transaction():
        raw.execute(
            "UPDATE postings SET unresolved_reasons = "
            "(CASE WHEN jsonb_typeof(unresolved_reasons) = 'array' "
            "THEN unresolved_reasons ELSE '[]'::jsonb END) "
            "|| (CASE WHEN "
            "(CASE WHEN jsonb_typeof(unresolved_reasons) = 'array' "
            "THEN unresolved_reasons ELSE '[]'::jsonb END) "
            "@> jsonb_build_array(%(reason)s::text) "
            "THEN '[]'::jsonb ELSE jsonb_build_array(%(reason)s::text) END) "
            "WHERE dedup_key = %(dedup_key)s",
            {"reason": reason, "dedup_key": dedup_key},
        )
    commit_unless_nested(raw)
