"""upsert_job — Postgres port of the private repo's single postings writer.

See the docstring rules block in the Wave-1 plan (Task 2 Step 10) for the
verified port-fidelity anchors and the three recorded Wave-1 divergences:
(1) sightings are keyed by source (spec §3.1), not (ats_platform, source_id);
(2) description merge is keep-longer only; (3) salary is fill-if-null only
(no observation reconciler). Everything else — kind derivation, strict
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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from psycopg.types.json import Jsonb

from jobcannon.db.pool import commit_unless_nested
from jobcannon.engine.parsed_job import ParsedJob, UnresolvedParsedJob

_PRECISION_RANK = {"exact": 3, "approximate": 2, "proxy": 1}


def _precision_rank(precision: str | None) -> int:
    return _PRECISION_RANK.get(precision or "", 0)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    salary_observations = list(existing["salary_observations"] or []) + list(
        parsed.salary_observations
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

    raw.execute(
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
            last_seen = now()
        WHERE dedup_key = %s
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
            matched_dedup_key,
        ),
    )
    commit_unless_nested(raw)

    if canonical_changed:
        kind = "updated"
    elif source_merged:
        kind = "touched"
    else:
        kind = "unchanged"
    return UpsertResult(kind, matched_dedup_key, list(parsed.unresolved_reasons))


def _loc_dict(loc: Any) -> dict:
    from dataclasses import asdict, is_dataclass

    return asdict(loc) if is_dataclass(loc) else dict(loc)
