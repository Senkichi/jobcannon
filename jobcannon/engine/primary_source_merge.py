# PORTED from job_finder/web/primary_source_merge.py @ 099531b7ede7c2a86c148b00c35d7dabf0a879d1 (private job-cannon). Ledger L-0228.
"""Merge authoritative fields from a strict-matched primary posting.

merge_primary_posting_fields is called when resolve_primary_posting produced a
STRICT match — the posting is the same real-world job as the stored row, so
its structured fields (salary metadata, posted date, locations, the ATS URL
itself) are authoritative and worth folding in.

The merge is routed through upsert_job (D-15: source_urls is a parser-owned
column) so every field follows the canonical merge rules — set-union
sources/source_urls, keep-longer description, Remote/Hybrid-first locations,
COALESCE fills — instead of ad-hoc UPDATE bypasses.

Identity is pinned to the EXISTING row (dedup_key/title/company): the ATS
title may normalize to a different dedup_key than the aggregator title did,
and an unpinned upsert would mint a duplicate row for the same job.

Non-destructive by design:
  - salary_min/max: first-seen wins (sent only when the row has neither),
    mirroring ats_scanner._upsert_one_ats_api_job; currency/period ride along
    only when a genuine new salary lands (enforced by the upsert SQL CASE).
  - posted_date: fills a NULL slot only.
  - score / score_breakdown: re-sent from the row — the upsert UPDATE branch
    overwrites them, so omitting them would zero the heuristic score.
  - source_id: separate guarded write — only when the row has none AND no
    other row holds (company_id, source_id) (I-11 partial unique index). A
    conflict means the ATS scanner already ingested this posting under a
    drifted title; it is logged as a retroactive-dedup candidate, never raised.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from jobcannon.engine.location_canonical import from_list as _locations_from_list

logger = logging.getLogger(__name__)


def _parse_posted_date(value: Any) -> datetime | None:
    """Parse a posting's posted_date (ISO string or datetime) — None on failure."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _safe_json_list(raw: Any) -> list:
    """Parse a JSON-array column value, tolerating NULL / junk."""
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _safe_json_dict(raw: Any) -> dict:
    """Parse a JSON-object column value, tolerating NULL / junk."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def merge_primary_posting_fields(
    conn: sqlite3.Connection,
    job_row: dict,
    posting: dict,
    *,
    source_tag: str | None = None,
    config: dict | None = None,
) -> bool:
    """Fold a strict-matched primary posting's fields into the existing row.

    Returns True when the row gained data (upsert kind 'updated' or 'touched'),
    False on a no-op or any failure. Never raises — enrichment must not abort
    because the bonus merge failed.

    source_tag, when given, rides along in the set-union ``sources`` list
    next to the posting's platform label — the resolver passes
    'primary_source_llm' for tie-breaker-upgraded merges so they remain
    auditable in the row itself (pitfall P13).

    config, when given, is forwarded to ``upsert_job`` so a salary pair that
    now clears the configured floor re-evaluates a stale ``salary_floor``
    dismissal (T4.2, D20) — this is the ATS-structured salary reconciliation
    path (rank 4), one of the sources that can raise a dismissed job's
    salary_max above the floor.
    """
    dedup_key = job_row.get("dedup_key")
    if not dedup_key or not posting:
        return False

    row = conn.execute(
        "SELECT title, company, company_id, location, salary_min, salary_max, "
        "posted_date, source_id, score_breakdown, unresolved_reasons "
        "FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    if row is None:
        return False
    row = dict(row)

    # P1.5 (D-4): the mirrored "first-seen salary wins" suppression is DELETED.
    # The primary posting is a strict-matched ATS source (provenance
    # 'ats_structured', rank 4); trust-ranked reconciliation in upsert_job now
    # decides whether its pair overwrites the stored one. Offer the posting's
    # salary unconditionally and let the reconciler rank it.
    salary_min = posting.get("salary_min")
    salary_max = posting.get("salary_max")

    # posted_date: offered unconditionally as 'exact' (#363) — the upsert's
    # precision precedence decides; an ATS first-posted timestamp may correct
    # a stored email-proxy date but never churns an equally-exact one.
    posted_date = _parse_posted_date(posting.get("posted_date"))

    posting_url = posting.get("source_url") or posting.get("url")
    source_label = posting.get("company_source")

    # Reconstruct real JobLocation instances in case this posting round-tripped
    # through JSON (e.g. the ATS scan cache) and arrived as plain dicts.
    # Without this, upsert_job's asdict() calls for the locations_structured
    # column and the postings descriptor raise TypeError.
    locations_structured = _locations_from_list(posting.get("locations_structured") or [])

    # The primary posting is a direct ATS sighting. Pass platform/source_id
    # to upsert_job so the postings sub-entity descriptor is created/updated,
    # but only when the source_id is or can become this row's (I-11 guard).
    ats_platform = posting.get("ats_platform")
    posting_source_id = posting.get("source_id")
    descriptor_source_id: str | None = None
    if ats_platform and posting_source_id and row["company_id"]:
        new_sid = str(posting_source_id)
        current_sid = row.get("source_id") or ""
        if current_sid == new_sid:
            descriptor_source_id = new_sid
        elif not current_sid:
            conflict = conn.execute(
                "SELECT 1 FROM jobs WHERE company_id = ? AND source_id = ? AND dedup_key != ? LIMIT 1",
                (row["company_id"], new_sid, dedup_key),
            ).fetchone()
            descriptor_source_id = new_sid if conflict is None else None

    try:
        from dataclasses import asdict

        from jobcannon.engine.services import get_services  # PORT-SEAM: host upsert_job
        from jobcannon.engine.parsed_job import ParsedJob
        from jobcannon.engine.salary_normalizer import SalaryObservation

        svc = get_services()  # PORT-SEAM: host upsert_job

        # P1.5 (D-1/D-4): a strict-matched primary posting is an ATS structured
        # source. Tag provenance + seed the lossless observation log when it
        # carries a pay range so the reconciler can rank it (rank 4) and the
        # evidence survives a later quarantine/overwrite.
        salary_provenance: str | None = None
        salary_observations: list[dict] = []
        if salary_min is not None or salary_max is not None:
            salary_provenance = "ats_structured"
            salary_observations = [
                asdict(
                    SalaryObservation(
                        min_value=salary_min,
                        max_value=salary_max,
                        period=posting.get("salary_period") or "unknown",
                        currency=posting.get("salary_currency") or "USD",
                        provenance="ats_structured",
                        raw_text=posting.get("comp_json") or posting.get("salary_raw"),
                    )
                )
            ]

        parsed = ParsedJob(
            title=row["title"],
            company=row["company"],
            dedup_key=dedup_key,
            # Fall back to the row's location so an empty incoming location
            # cannot regress the structured-locations derivation in upsert.
            location=posting.get("location") or row["location"] or "",
            locations_structured=locations_structured,
            source_id=descriptor_source_id,
            sources=[s for s in (source_label, source_tag) if s],
            source_urls=[posting_url] if posting_url else [],
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=posting.get("salary_currency") or "USD",
            salary_period=posting.get("salary_period") or "unknown",
            salary_provenance=salary_provenance,
            salary_observations=salary_observations,
            description=posting.get("description") or None,
            posted_date=posted_date,
            posted_date_precision="exact" if posted_date else None,
            # A canonical change re-applies unresolved_reasons from the parsed
            # object; carry the row's existing flags through so this merge
            # cannot clear a pending /admin/review item.
            unresolved_reasons=_safe_json_list(row["unresolved_reasons"]),
        )
        result = svc.upsert_job(  # PORT-SEAM: host upsert_job
            conn,
            parsed,
            company_id=row["company_id"],
            score_breakdown=_safe_json_dict(row["score_breakdown"]),
            ats_platform=ats_platform if descriptor_source_id else None,
            config=config,
        )
    except Exception as exc:
        logger.warning("primary-posting merge failed for %s: %s", dedup_key, exc)
        return False

    # source_id rides separately — the upsert UPDATE branch never touches it.
    # set_source_id_if_free is the sanctioned single-writer (I-11 guarded).
    # PORT-SEAM: host set_source_id_if_free; None => skip the backfill.
    if svc.set_source_id_if_free is not None:
        svc.set_source_id_if_free(conn, dedup_key, row["company_id"], posting.get("source_id"))
    return result.kind in ("updated", "touched")
