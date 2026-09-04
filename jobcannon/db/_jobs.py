"""upsert_job — Postgres port of the private repo's single postings writer.

See the docstring rules block in the Wave-1 plan (Task 2 Step 10) for the
verified port-fidelity anchors and the four recorded Wave-1 divergences:
(1) sightings are keyed by source (spec §3.1), not (ats_platform, source_id);
(2) description merge is keep-longer only; (3) salary canonical-pair fill is
fill-if-null only (no trust-ranked reconciler) — the observations LOG itself
is deduped + capped identically to the original (see
``_merge_salary_observations``); (4) a posted-date precision win sets
canonical_changed (kind='updated') even when the calendar date value itself
is unchanged — e.g. the same date re-confirmed at a higher-precision marker
— whereas the original only flags canonical_changed when the win ALSO
changes the stored date value. Deliberate: re-confirmation at higher
precision is treated as a canonical write here (see the inline comment at
the `pd_wins` check). Everything else — kind derivation otherwise, strict
posted-date precedence, secondary (company_id, source_id) match, D-19
non-boolean UpsertResult — matches the original behavior exactly.

``score_breakdown`` and ``config`` are accepted for signature parity with the
frozen ScanServices.upsert_job contract but are UNUSED in Wave 1: ``config``
gates the private repo's auto-reopen logic, which has no hosted counterpart
yet.

Transaction-boundary note (recorded port deviation, matches _companies.py /
_jd_full.py): both the INSERT and UPDATE branches commit via
pool.commit_unless_nested() rather than a bare raw.commit() call, so this
also works when `conn` is already inside an ambient `with
conn.transaction():` block (tests/host/conftest.py's db_conn fixture) —
psycopg3 forbids explicit commit() there. See that helper's docstring for
why a naive `with raw.transaction():` wrapper does not substitute for a real
commit here (it degrades to a savepoint whenever the connection already
carries an open, non-Transaction-managed transaction from the initial bare
`SELECT * FROM postings WHERE dedup_key = ...` lookup a few lines above).

Savepoint note: each write statement (the INSERT and the UPDATE) is
separately wrapped in its own `with raw.transaction():` block purely for
SAVEPOINT-based recovery — if the write raises (e.g. a NOT NULL / CHECK
violation), that block's __exit__ rolls back to the savepoint and
re-raises, leaving the connection usable for the caller's next statement
instead of stuck in Postgres's aborted-transaction state. This does NOT
replace the durable commit: pool.commit_unless_nested() still runs
immediately after the block on every success path, exactly as before.

``unresolved_reasons`` ownership partition (#235, UPDATE branch only):
this column has exactly two production writers — this module and
``_jd_full.py``'s ``set_jd_full`` / ``_record_jd_content_reject`` (grep
confirms no third; ``_unresolved_reasons.py`` is a Python reference no
production path calls, per its own docstring). ``_jd_full.py`` owns
``JD_CONTENT_REASON_CODES`` (``jd_full_offsite`` / ``jd_full_expired`` /
``jd_full_truncated``, defined in ``jd_content_contract.py``) and writes
them via atomic SQL expressions evaluated against the row's LIVE value
(#217/#232) — the authoritative verdict about the row's CURRENTLY STORED
``jd_full``. ``parsed.unresolved_reasons`` can independently re-derive
those SAME codes (I-18 in ``ParsedJob.from_job``) by running
``jd_content_reject`` a second time against THIS ingestion's own
(possibly different, possibly stale-by-the-time-this-UPDATE-runs)
``jd_full`` snippet — a verdict about a different observation of the
row's content, not the live one. Writing that verdict as part of this
module's wholesale ``unresolved_reasons`` replacement could silently
erase a live, still-true ``_jd_full.py`` quarantine flag (the #235 bug).

The fix: this module never asserts an opinion on ``JD_CONTENT_REASON_CODES``.
Before the UPDATE, ``parsed.unresolved_reasons`` is filtered to strip any
``JD_CONTENT_REASON_CODES`` entries (this module's owned codes only —
title/salary/junk-density reasons). The UPDATE's SQL CASE expression (only
evaluated when ``canonical_changed``, same gate as before) then computes
the new value as ``(the row's LIVE ``JD_CONTENT_REASON_CODES``-tagged
entries, whatever they are at UPDATE-execution time) || (this module's
filtered, parser-owned reasons)`` — a live-row read merged with a
Python-known-safe value, both by the SAME statement that writes it, so a
concurrent ``_jd_full.py`` writer's already-committed change can never be
clobbered by this one landing after it (and vice versa: mirrors the
#217/#232 atomic-SQL-expression shape exactly, one ownership partition
instead of one set-difference). Because the two inputs are constructed to
be disjoint by construction (the Python side is pre-filtered to exclude
the codes the SQL side already owns), no de-dup step is needed at
concatenation. The UPDATE additionally gains ``RETURNING unresolved_reasons``
so ``UpsertResult.unresolved_reasons`` reports what this statement actually
persisted rather than the raw (possibly since-filtered) ``parsed`` value —
without this, the returned value would drift from the DB the same way the
bug this fix closes did.

Lost-update fix (#245, advisory lock): ``canonical_changed`` (computed below
from the ``existing`` row fetched by the initial SELECT) is itself derived
from a snapshot that can go stale mid-transaction. Two concurrent
``upsert_job`` calls for the SAME row can both SELECT before either commits;
the second caller's ``canonical_changed`` computation then runs against
pre-first-commit data, falsely concludes it is making a genuine canonical
change, and blindly overwrites the parser-owned slice of
``unresolved_reasons`` with its own Python-computed literal (unlike the
JD-owned slice above, ``parser_reasons`` has no live-row SQL read to protect
it). The fix is `pg_advisory_xact_lock(hashtext(...))`, acquired before any
SELECT, transaction-scoped so it releases automatically at
``commit_unless_nested`` / rollback with no extra cleanup code. Under the
lock, a second caller's SELECT is serialized after the first's commit, sees
live data, and correctly evaluates ``canonical_changed = False`` when its
own payload no longer represents new information -- the CASE's ELSE branch
then preserves the first caller's reasons untouched. Locking is keyed on
``matched_dedup_key``, not unconditionally on ``parsed.dedup_key``: the
(company_id, source_id) fallback match below can resolve to an existing row
under a DIFFERENT dedup_key than ``parsed.dedup_key``, so the lock is
acquired once on ``parsed.dedup_key`` before the primary SELECT and, only
when the fallback resolves to a different key, a second time on that
resolved key -- with ``existing`` re-read under the second lock, since the
fallback SELECT that discovered it ran before that lock was held. SQL-level
union and CAS/retry were considered and rejected: union is disqualified by
``test_control_parser_codes_still_fully_replace_stale_parser_codes``
(sequential calls must fully replace, not union) and reintroduces a
can-never-shrink trap; CAS/retry doesn't help because ``parser_reasons`` is
computed purely from ``parsed.unresolved_reasons``, never from ``existing``,
so retrying against fresher data changes nothing about what gets written.
"""

from __future__ import annotations

import logging  # PORT-SEAM: added for L-0070's set_source_id_if_free (private used the same module-level logger for this warning path)
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from psycopg.types.json import Jsonb

from jobcannon.db.pool import commit_unless_nested
from jobcannon.engine.jd_content_contract import JD_CONTENT_REASON_CODES
from jobcannon.engine.parsed_job import ParsedJob, UnresolvedParsedJob

_logger = logging.getLogger(__name__)

_PRECISION_RANK = {"exact": 3, "approximate": 2, "proxy": 1}

# Maximum number of salary observations retained per row. Bounds the growth
# of the append-only salary_observations JSON array (mirrors the private
# original's cap; oldest entries are dropped first once the cap is hit).
_MAX_SALARY_OBSERVATIONS = 20


def _precision_rank(precision: str | None) -> int:
    return _PRECISION_RANK.get(precision or "", 0)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _observation_dedup_key(obs: dict) -> tuple:
    """Identity tuple for salary-observation dedup: (provenance, raw_text, min, max)."""
    return (
        obs.get("provenance"),
        obs.get("raw_text"),
        obs.get("min_value"),
        obs.get("max_value"),
    )


def _merge_salary_observations(stored: list[dict], incoming: list[dict]) -> list[dict]:
    """Append incoming salary observations onto the stored log.

    Dedupes by ``_observation_dedup_key`` so a re-sighting of the identical
    assertion does not grow the array, and caps the result at
    ``_MAX_SALARY_OBSERVATIONS`` entries, dropping the oldest first.
    """
    if not incoming:
        return list(stored)
    seen = {_observation_dedup_key(o) for o in stored}
    merged = list(stored)
    for obs in incoming:
        key = _observation_dedup_key(obs)
        if key in seen:
            continue
        seen.add(key)
        merged.append(obs)
    if len(merged) > _MAX_SALARY_OBSERVATIONS:
        merged = merged[-_MAX_SALARY_OBSERVATIONS:]
    return merged


@dataclass(frozen=True)
class UpsertResult:
    kind: Literal["inserted", "updated", "unchanged", "touched"]
    dedup_key: str
    unresolved_reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # D-19: never boolean-test an UpsertResult
        raise TypeError(
            "UpsertResult is not bool-testable. Use result.kind: "
            "'inserted', 'updated', 'touched', or 'unchanged'."
        )


def upsert_job(
    conn: Any,
    parsed: ParsedJob | UnresolvedParsedJob,
    *,
    company_id: int | None = None,
    score_breakdown: dict | None = None,
    ats_platform: str | None = None,
    config: dict | None = None,
) -> UpsertResult:
    if not isinstance(parsed, (ParsedJob, UnresolvedParsedJob)):
        raise TypeError(
            f"upsert_job requires ParsedJob | UnresolvedParsedJob, got {type(parsed)!r}"
        )
    raw = conn.raw if hasattr(conn, "raw") else conn

    # #245: serialize concurrent upsert_job calls on the same row so the
    # canonical-change snapshot below can never go stale mid-flight. See the
    # module docstring for why this closes the unresolved_reasons lost
    # update and why the lock is keyed on matched_dedup_key, not
    # unconditionally on parsed.dedup_key.
    raw.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (parsed.dedup_key,))

    existing = raw.execute(
        "SELECT * FROM postings WHERE dedup_key = %s", (parsed.dedup_key,)
    ).fetchone()
    matched_dedup_key = parsed.dedup_key
    if existing is None and parsed.source_id and company_id is not None:
        existing = raw.execute(
            "SELECT * FROM postings WHERE company_id = %s AND source_id = %s",
            (company_id, parsed.source_id),
        ).fetchone()
        if existing is not None:
            matched_dedup_key = existing["dedup_key"]
            if matched_dedup_key != parsed.dedup_key:
                # The fallback SELECT above ran before this second lock was
                # held, so it may itself be stale relative to a concurrent
                # writer already serialized on matched_dedup_key -- re-read
                # under the lock rather than trusting that snapshot.
                raw.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (matched_dedup_key,))
                existing = raw.execute(
                    "SELECT * FROM postings WHERE dedup_key = %s", (matched_dedup_key,)
                ).fetchone()

    pd_date = parsed.posted_date.date() if parsed.posted_date else None
    pd_precision = (parsed.posted_date_precision or "proxy") if pd_date else None
    now_iso = _utc_now_iso()

    if existing is None:
        sightings = [
            {
                "source": src,
                "source_url": (parsed.source_urls[i] if i < len(parsed.source_urls) else None),
                "first_seen": now_iso,
                "last_seen": now_iso,
            }
            for i, src in enumerate(parsed.sources)
        ]
        with raw.transaction():
            raw.execute(
                """
                INSERT INTO postings (
                    dedup_key, company_id, title, company, location, locations_raw,
                    locations_structured, workplace_type, primary_country_code,
                    sources, source_urls, source_id, sightings, description,
                    salary_min, salary_max, salary_currency, salary_period,
                    salary_observations, posted_date, posted_date_precision,
                    direct_url, ats_platform, employment_type, is_remote,
                    unresolved_reasons
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    parsed.dedup_key,
                    company_id,
                    parsed.title,
                    parsed.company,
                    parsed.location or None,
                    Jsonb(parsed.locations_raw),
                    Jsonb([_loc_dict(loc) for loc in parsed.locations_structured])
                    if parsed.locations_structured
                    else None,
                    parsed.workplace_type if parsed.workplace_type != "UNSPECIFIED" else None,
                    parsed.primary_country_code,
                    Jsonb(list(parsed.sources)),
                    Jsonb(list(parsed.source_urls)),
                    parsed.source_id,
                    Jsonb(sightings),
                    parsed.description,
                    parsed.salary_min,
                    parsed.salary_max,
                    parsed.salary_currency,
                    parsed.salary_period,
                    Jsonb(list(parsed.salary_observations)),
                    pd_date,
                    pd_precision,
                    None,
                    ats_platform,
                    None,
                    None,
                    Jsonb(list(parsed.unresolved_reasons)),
                ),
            )
        commit_unless_nested(raw)
        return UpsertResult("inserted", parsed.dedup_key, list(parsed.unresolved_reasons))

    # ---- UPDATE branch ----
    canonical_changed = False
    source_merged = False

    sources = list(existing["sources"] or [])
    for src in parsed.sources:
        if src not in sources:
            sources.append(src)
            source_merged = True
    source_urls = list(existing["source_urls"] or [])
    for url in parsed.source_urls:
        if url not in source_urls:
            source_urls.append(url)
            source_merged = True

    sightings = list(existing["sightings"] or [])
    for i, src in enumerate(parsed.sources):
        url = parsed.source_urls[i] if i < len(parsed.source_urls) else None
        for entry in sightings:
            if entry.get("source") == src:
                entry["last_seen"] = now_iso
                if url:
                    entry["source_url"] = url
                break
        else:
            sightings.append(
                {"source": src, "source_url": url, "first_seen": now_iso, "last_seen": now_iso}
            )

    existing_pd_rank = _precision_rank(
        existing["posted_date_precision"] or ("proxy" if existing["posted_date"] else None)
    )
    pd_wins = pd_date is not None and _precision_rank(pd_precision) > existing_pd_rank
    if pd_wins:
        # A win means the incoming marker outranks what is stored, whether or
        # not the calendar date value itself also changed (e.g. the same date
        # re-confirmed with a higher-precision marker) — still a canonical
        # write per the port rule ("a win sets canonical_changed").
        canonical_changed = True
    new_pd = pd_date if pd_wins else existing["posted_date"]
    new_pd_precision = pd_precision if pd_wins else existing["posted_date_precision"]

    description = existing["description"]
    if parsed.description and len(parsed.description) > len(description or ""):
        description = parsed.description
        canonical_changed = True

    salary_min, salary_max = existing["salary_min"], existing["salary_max"]
    salary_currency, salary_period = existing["salary_currency"], existing["salary_period"]
    if (
        salary_min is None
        and salary_max is None
        and (parsed.salary_min is not None or parsed.salary_max is not None)
    ):
        salary_min, salary_max = parsed.salary_min, parsed.salary_max
        salary_currency, salary_period = parsed.salary_currency, parsed.salary_period
        canonical_changed = True
    salary_observations = _merge_salary_observations(
        existing["salary_observations"] or [], list(parsed.salary_observations)
    )

    locations_raw = list(existing["locations_raw"] or [])
    for loc in parsed.locations_raw:
        if loc not in locations_raw:
            locations_raw.append(loc)
            canonical_changed = True
    location = existing["location"] or (parsed.location or None)
    if location != existing["location"]:
        canonical_changed = True
    locations_structured = existing["locations_structured"]
    if locations_structured is None and parsed.locations_structured:
        locations_structured = [_loc_dict(loc) for loc in parsed.locations_structured]
        canonical_changed = True

    def _fill(col: str, incoming):
        nonlocal canonical_changed
        if existing[col] is None and incoming is not None:
            canonical_changed = True
            return incoming
        return existing[col]

    workplace_type = _fill(
        "workplace_type",
        parsed.workplace_type if parsed.workplace_type != "UNSPECIFIED" else None,
    )
    primary_country_code = _fill("primary_country_code", parsed.primary_country_code)
    ats_platform_col = _fill("ats_platform", ats_platform)
    source_id_col = _fill("source_id", parsed.source_id)

    # Preserve unresolved_reasons unless a canonical field changed. A touch
    # / re-sighting (or a no-op re-ingest) must not clobber an /admin/review
    # triage decision — only a genuine canonical update re-applies the
    # parser contract's reason codes. #235: this module owns every reason
    # code EXCEPT JD_CONTENT_REASON_CODES (_jd_full.py's vocabulary) — strip
    # those from the parser's list here so the write below never carries an
    # opinion on them; the SQL CASE preserves _jd_full.py's live-row value
    # for those codes untouched instead. See the module docstring.
    parser_reasons = [
        reason for reason in parsed.unresolved_reasons if reason not in JD_CONTENT_REASON_CODES
    ]

    with raw.transaction():
        cur = raw.execute(
            """
            UPDATE postings SET
                sources = %s, source_urls = %s, sightings = %s,
                posted_date = %s, posted_date_precision = %s,
                description = %s,
                salary_min = %s, salary_max = %s, salary_currency = %s,
                salary_period = %s, salary_observations = %s,
                location = %s, locations_raw = %s, locations_structured = %s,
                workplace_type = %s, primary_country_code = %s,
                ats_platform = %s, source_id = %s,
                unresolved_reasons = CASE WHEN %s::boolean THEN
                    COALESCE(
                        (SELECT jsonb_agg(elem ORDER BY ord)
                         FROM jsonb_array_elements_text(
                             CASE WHEN jsonb_typeof(unresolved_reasons) = 'array'
                             THEN unresolved_reasons ELSE '[]'::jsonb END
                         ) WITH ORDINALITY AS t(elem, ord)
                         WHERE elem = ANY(%s)
                        ), '[]'::jsonb
                    ) || %s
                ELSE unresolved_reasons END,
                last_seen = now()
            WHERE dedup_key = %s
            RETURNING unresolved_reasons
            """,
            (
                Jsonb(sources),
                Jsonb(source_urls),
                Jsonb(sightings),
                new_pd,
                new_pd_precision,
                description,
                salary_min,
                salary_max,
                salary_currency,
                salary_period,
                Jsonb(salary_observations),
                location,
                Jsonb(locations_raw),
                Jsonb(locations_structured)
                if isinstance(locations_structured, list)
                else locations_structured,
                workplace_type,
                primary_country_code,
                ats_platform_col,
                source_id_col,
                canonical_changed,
                list(JD_CONTENT_REASON_CODES),
                Jsonb(parser_reasons),
                matched_dedup_key,
            ),
        )
    commit_unless_nested(raw)
    # RETURNING captures what THIS statement actually persisted — honest
    # even under the #235 race, unlike re-using the (possibly by-now-stale)
    # `existing` SELECT or the raw `parsed.unresolved_reasons` value.
    returned = cur.fetchone()
    persisted_unresolved_reasons = list(returned["unresolved_reasons"]) if returned else []

    if canonical_changed:
        kind = "updated"
    elif source_merged:
        kind = "touched"
    else:
        kind = "unchanged"
    return UpsertResult(kind, matched_dedup_key, persisted_unresolved_reasons)


def _loc_dict(loc: Any) -> dict:
    from dataclasses import asdict, is_dataclass

    return asdict(loc) if is_dataclass(loc) else dict(loc)


# PORTED from job_finder/db/_jobs.py @ b1f69f3e10a452cc498527f830959b852108f5e9
# (private job-cannon). Ledger L-0070 -- the 5 auxiliary write/read helpers
# named in that row's seam. Of those, set_source_id_if_free / get_job /
# load_job_context land below; set_postings and set_location_policy_columns
# are deferred (not dropped) -- see notes at the bottom of this block.
#
# PORT-SEAM: set_source_id_if_free's IntegrityError backstop has no schema
# analog here. Private's UPDATE relies on the I-11 partial UNIQUE index on
# (company_id, source_id) to make a lost-race double-write raise; this host's
# only index on that pair (m0001, idx_postings_company_source) is a plain
# non-unique btree, so a race that slips past the pre-write SELECT commits
# silently instead of raising. The pre-write SELECT check and warning-log
# path are ported as-is (still closes the common case); the except clause is
# kept as defensive dead code documenting the divergence rather than removed,
# since a future unique index would reactivate it for free.
def set_source_id_if_free(
    conn: Any,  # PORT-SEAM: sqlite3.Connection -> Any (raw psycopg or EngineCompatConnection, matches upsert_job's signature)
    dedup_key: str,
    company_id: int | None,
    source_id: str | None,
) -> bool:
    """Write ``source_id`` when the row has none and the (company_id, source_id)
    pair is free.

    Sanctioned single-writer for ``postings.source_id`` outside ingestion --
    the ``upsert_job`` UPDATE branch deliberately never touches source_id
    (see that function's docstring), so a strict-matched primary posting
    (primary_source_merge, when ported) routes its platform-stable posting
    id through here.
    # PORT-SEAM: jobs.source_id -> postings.source_id; primary_source_merge
    # is not yet ported on this host.

    Returns False without writing when source_id/company_id is missing, the
    row is absent or already carries a source_id, or another row already
    holds (company_id, source_id) -- that twin means the ATS scanner already
    ingested the same posting under a drifted title; it is logged as a
    retroactive-dedup candidate, never raised.
    # PORT-SEAM: docstring adapted for postings.source_id / no I-11 partial
    # unique index on this host -- see the pre-write-check note below.
    """
    raw = (
        conn.raw if hasattr(conn, "raw") else conn
    )  # PORT-SEAM: EngineCompatConnection unwrap, matches upsert_job
    if not source_id or company_id is None or not dedup_key:
        return False
    source_id = str(source_id)

    row = raw.execute(  # PORT-SEAM: sqlite3 `?` placeholders -> psycopg `%s`; jobs -> postings
        "SELECT source_id FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    if (
        row is None or row["source_id"]
    ):  # PORT-SEAM: row[0] (sqlite3 positional) -> row["source_id"] (psycopg dict_row)
        return False

    holder = raw.execute(  # PORT-SEAM: jobs -> postings; `?` -> `%s`
        "SELECT dedup_key FROM postings WHERE company_id = %s AND source_id = %s "
        "AND dedup_key != %s",
        (company_id, source_id, dedup_key),
    ).fetchone()
    if holder is not None:
        _logger.warning(
            "source_id %s (company_id=%s) already held by %s -- same posting "  # PORT-SEAM: em dash -> ASCII double-hyphen (log text only, matches this file's existing style)
            "under a drifted title; skipping (retroactive-dedup candidate)",
            source_id,
            company_id,
            holder["dedup_key"],  # PORT-SEAM: holder[0] -> holder["dedup_key"] (psycopg dict_row)
        )
        return False

    # PORT-SEAM: no schema-level backstop for this UPDATE -- see the module
    # note above this function for why the except clause below is kept as
    # documented dead code rather than removed.
    try:
        with raw.transaction():  # PORT-SEAM: sqlite3 conn.execute/commit -> raw.transaction()+commit_unless_nested (matches upsert_job's own transaction pattern)
            raw.execute(
                "UPDATE postings SET source_id = %s WHERE dedup_key = %s",
                (source_id, dedup_key),
            )
        commit_unless_nested(raw)
    except Exception as exc:  # pragma: no cover -- PORT-SEAM: sqlite3.IntegrityError -> Exception (no schema backstop, see note above)
        _logger.warning("source_id write rejected for %s: %s", dedup_key, exc)
        return False
    return True


# PORT-SEAM: set_postings (private's sanctioned single-writer for the
# jobs.postings JSON rollup column, previously here between
# set_source_id_if_free and get_job) is deferred, not ported -- and, per
# L-0075's ledger `seam` ruling below, has no separate target to land: this
# host's `postings` table is a flat one-row-per-job-entity model (see m0001)
# with no per-source descriptor sub-entity list, so there is no JSON rollup
# column for set_postings / upsert_posting / build_posting_descriptor /
# _find_descriptor_index to write -- the flat row already *is* the posting,
# and this file's own module docstring ("single postings writer") is that
# architecture ruling. L-0075's one remaining live surface,
# annotate_posting_apply_url, lands below (re-adapted to key on dedup_key
# alone -- see that function's own PORT-SEAM block).

# PORT-SEAM: set_location_policy_columns is deferred, not ported. It writes
# location_policy_version / _input_fingerprint / _verdict / _sort_order /
# _rank / _eligible -- six columns this host does not have. This PR's own
# _assessment_writer.py and _feed.py already scope location_policy_* out
# (see their PORT-SEAM notes and m0015_postings_scoring_tuple.py's
# docstring); adding six new columns for a single writer function here,
# inconsistent with those already-landed decisions, is out of scope for this
# port group. Revisit alongside a location-policy migration.


# PORTED from job_finder/db/_postings.py @ 175d0e1024eee45a279522868798fb7b4777a952
# (private job-cannon). Ledger L-0075 -- flat re-adaptation of
# annotate_posting_apply_url, the one live surface of that private module
# (upsert_posting / build_posting_descriptor / _find_descriptor_index are
# subsumed, not ported -- see the PORT-SEAM block above this function).
#
# PORT-SEAM: private keyed the write on (ats_platform, source_id) to find
# the ONE descriptor to annotate inside the jobs.postings JSON array
# (multiple descriptors could share a row). This host's postings table has
# no descriptor sub-entity -- dedup_key alone already identifies the single
# target row -- so those two args are dropped from the signature entirely
# (arity reduction, not a default-filled shim).
#
# PORT-SEAM: private opened its own IMMEDIATE transaction and re-read the
# current postings JSON inside it before merging, specifically so a
# concurrent upsert_posting write to a DIFFERENT descriptor in the same
# array would survive this write untouched. aggregator_apply_url is a
# scalar column on this host (m0019) -- there is no sibling descriptor for
# a concurrent writer to clobber, so a single UPDATE is already atomic and
# the re-read-then-merge dance has no target to port.
#
# PORT-SEAM: docstring rewritten for the flat-column target -- private's
# said "attach to the posting descriptor keyed (ats_platform,
# source_id) on jobs.postings"; there is no descriptor here to key.
def annotate_posting_apply_url(
    conn: Any,  # PORT-SEAM: no sqlite3 dialect on this host (psycopg only)
    dedup_key: str,
    # PORT-SEAM: ats_platform/source_id dropped here (arity reduction, see
    # block comment above) -- dedup_key alone identifies the flat row.
    aggregator_apply_url: str,
) -> bool:
    """Sanctioned single writer for ``postings.aggregator_apply_url`` (m0019).

    Attaches an aggregator-sourced apply link to the posting row -- a
    distinct provenance from ``direct_url``/``direct_url_confidence``
    (m0017, ``_direct_link.py``'s no-downgrade company-site writer; not
    overloaded here, see m0019's migration docstring).

    Returns True if a row was matched and written, False if *dedup_key* /
    *aggregator_apply_url* is falsy or no row matches.

    Args:
        conn: pooled connection (``.raw`` unwrapped) or a bare psycopg
            connection, matching ``set_direct_url``'s dispatch.
        dedup_key: The posting's natural key.
        aggregator_apply_url: The aggregator-sourced apply link to attach.
    """
    # PORT-SEAM: private only guarded dedup_key falsiness; the
    # aggregator_apply_url guard is added below since this host's arity-
    # reduced signature has no descriptor lookup to no-op on instead.
    if (
        not dedup_key or not aggregator_apply_url
    ):  # PORT-SEAM: added aggregator_apply_url guard, see comment above
        return False

    raw = (
        conn.raw if hasattr(conn, "raw") else conn
    )  # PORT-SEAM: EngineCompatConnection unwrap, matches set_source_id_if_free

    with raw.transaction():
        cursor = raw.execute(
            "UPDATE postings SET aggregator_apply_url = %s WHERE dedup_key = %s",  # PORT-SEAM: jobs.postings JSON merge -> scalar-column UPDATE; ? -> %s
            (aggregator_apply_url, dedup_key),
        )
        rowcount = cursor.rowcount
    commit_unless_nested(
        raw
    )  # PORT-SEAM: replaces private's conn.execute("COMMIT") inside its own BEGIN IMMEDIATE block
    return rowcount > 0


def get_job(conn: Any, dedup_key: str) -> dict | None:
    """Return a single job (posting) by dedup_key, or None if not found.

    Args:
        conn: Open connection (raw psycopg or EngineCompatConnection).
        # PORT-SEAM: sqlite3.Connection -> Any (this host's connection duck type)
        dedup_key: The job's primary key.

    Returns:
        Job as dict with all columns, or None if not found.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn  # PORT-SEAM: EngineCompatConnection unwrap
    row = raw.execute(
        "SELECT * FROM postings WHERE dedup_key = %s",  # PORT-SEAM: JOBS_ALL_COLUMNS projection -> SELECT *; jobs -> postings; `?` -> `%s`
        (dedup_key,),
    ).fetchone()
    return dict(row) if row is not None else None


def load_job_context(
    conn: Any, dedup_key: str
) -> dict | None:  # PORT-SEAM: sqlite3.Connection -> Any
    """Load the standard job context bundle.

    Shared helper for expand, rescore, paste_jd, and save_jd routes.

    Args:
        conn: Open connection (raw psycopg or EngineCompatConnection).
        # PORT-SEAM: sqlite3.Connection -> Any (see get_job above)
        dedup_key: The job's primary key.

    Returns:
        Dict with key 'job', or None if job not found.
    """
    job = get_job(conn, dedup_key)
    if job is None:
        return None

    return {"job": job}
