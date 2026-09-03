# PORTED from job_finder/web/primary_source_resolver.py @ c6e37b72c6706e6f547c63d0a697b9ef645c2dff (private job-cannon). Ledger L-0229.
"""Company-batched primary-source resolver (scheduled stage, Phase 3).

Every aggregator-discovered job at a company with a verified ATS board gets a
resolution attempt — decoupled from whether enrichment needed to run (closes
the G2 coverage leak), across the full PlatformScanner registry (closes G3's
3-platform limit), at one board fetch per company per run instead of one per
job.

Pipeline per run:
  1. Free promotion — a job whose source_urls already contain an ATS/careers
     link gets it as a strict direct_url. No network, no attempt consumed.
  2. Company-batched board match — candidates (direct_url IS NULL, company
     ats_probe_status='hit', attempts under the cap or past the decay window,
     not expired/closed) are grouped by company; each company's board is
     fetched ONCE via the PlatformScanner registry, and every candidate job
     is matched in memory via resolve_primary_posting. A strict match merges
     authoritative fields (primary_source_merge); a loose match records the
     link only — the contamination invariant from Phase 2.

Attempt semantics (m092 columns):
  - direct_url_checked_at / direct_url_attempts stamp once per board-match
    attempt via db._direct_link.stamp_direct_url_checks (single writer).
  - An empty board fetch counts as an attempt for all of that company's
    candidates: the registry contract returns [] for both "no postings" and
    "fetch failed", so the two are indistinguishable here. The decay window
    below repairs any attempt burned on a transient outage. An uncaught
    scanner exception (as opposed to the registry's own []-on-failure
    contract) is a different anomaly and burns no attempt at all — it is
    treated like an unsupported platform so it cannot abort other
    in-flight companies' results in the parallel fetch pool.
  - Re-eligibility is DECAY-BASED, not transition-hooked: a row past
    max_attempts re-enters candidacy once its checked_at ages past
    recheck_days. The alternative — resetting attempts when ats_probe_status
    flips to 'hit' — would need hooks at ~8 scattered status-write sites
    (ats_prober, ats_scanner._probe, ats_identity_reconcile, _upsert) and
    would still miss slug re-keys/heals on already-hit companies. The decay
    window covers all of those from one place (the candidate SQL) at the
    cost of one bounded re-check per job per window. Note that attempts only
    accrue while a company IS 'hit' (candidacy requires it), so the classic
    deadlock — attempts exhausted before the ATS was even discovered —
    cannot occur.

Company gating is strict (pitfall P2): only ats_probe_status='hit' rows are
consulted; the resolver never probes speculatively, keeping the
speculative-miss cohort's ~29% FP rate quarantined in the probe subsystem.

Runs on its own sqlite3 connection (APScheduler thread; stale_detector
pattern). Careers-page (non-ATS) resolution intentionally stays in the free
enrichment tier: per-job HTML scraping is exactly the N-fetches-per-company
shape this module exists to eliminate.

LLM tie-breaker (Phase 4): a loose/ambiguous heuristic match gets one
quick-tier call_model verdict (primary_source_tiebreak module — $0 on the
Ollama primary). Only a confident, valid verdict upgrades the job to a
strict link + data merge; the merge is tagged with a 'primary_source_llm'
source label for auditability (pitfall P13). The first provider failure
disables tie-breaking for the rest of the run — a dead cascade must not
turn into a per-job timeout storm.

Config (config.yaml > direct_link.resolver, all optional):
  enabled                  gate consulted by the scheduler wrapper (default true)
  max_attempts             skip rows at this many attempts (default 3)
  recheck_days             decay window for re-eligibility (default 30)
  max_companies_per_run    board-fetch cap per run (default 50)
  llm_tiebreak             quick-tier tie-breaker for loose matches (default true)
  llm_tiebreak_max_board   skip the LLM above this many candidate postings (40)
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

# PORT-SEAM: db._direct_link.{set_direct_url,stamp_direct_url_checks} are
# OPTIONAL ScanServices seams (svc.set_direct_url / svc.stamp_direct_url_checks)
# rather than a direct import -- Postgres-native SQL cannot run against the
# bare sqlite3 connections tests/engine/ uses, even though it is safe against
# a real connection_factory connection in production (internal conn.raw unwrap).
from jobcannon.engine.json_utils import utc_now_iso

# PORT-SEAM: db_helpers.standalone_connection (DIES) -> svc.connection_factory()
from jobcannon.engine.direct_link import (
    _posting_link,
    promote_existing_direct_url,
    resolve_primary_posting,
)
from jobcannon.engine.primary_source_merge import merge_primary_posting_fields
from jobcannon.engine.services import get_services

# PORT-SEAM: job_finder.web.db_helpers.standalone_connection is DIES ->
# svc.connection_factory(). job_finder.web.primary_source_tiebreak.
# tiebreak_primary_posting is L-0230 (HOLD) -> svc.tiebreak_primary_posting(...);
# its sibling DEFAULT_MAX_BOARD is a plain int constant, not a callable, and
# is copied verbatim below instead (ats_slug_challenge.TRIGGER_PREFIX_CAREERS_URL
# precedent). job_finder.db._postings.annotate_posting_apply_url is L-0075
# (escalated/unlanded) -> svc.annotate_posting_apply_url(...).
DEFAULT_MAX_BOARD = 40

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_RECHECK_DAYS = 30
_DEFAULT_MAX_COMPANIES_PER_RUN = 50
_DEFAULT_PARALLEL_WORKERS = 4
_MIN_PARALLEL_WORKERS = 1
_MAX_PARALLEL_WORKERS = 6

# Unresolved jobs at probe-verified companies, attempt-gated with decay
# re-eligibility. Closed/expired rows are excluded — resolving a dead
# posting's Apply target is wasted board traffic. ISO-8601 naive-UTC strings
# compare correctly as text.
_CANDIDATE_SQL = """
    SELECT j.dedup_key, j.title, j.location, j.company_id,
           COALESCE(j.description, substr(j.jd_full, 1, 400)) AS snippet,
           c.ats_platform, c.ats_slug
    FROM jobs j
    JOIN companies c ON c.id = j.company_id
    WHERE j.direct_url IS NULL
      AND c.ats_probe_status = 'hit'
      AND c.ats_platform IS NOT NULL
      AND c.ats_slug IS NOT NULL AND c.ats_slug != ''
      AND (COALESCE(j.direct_url_attempts, 0) < ?
           OR COALESCE(j.direct_url_checked_at, '') < ?)
      AND (j.expiry_status IS NULL OR j.expiry_status != 'expired')
      AND (j.pipeline_status IS NULL OR j.pipeline_status NOT IN
           ('archived', 'rejected', 'withdrawn', 'dismissed'))
    ORDER BY j.company_id, j.last_seen DESC
"""


def _resolver_settings(config: dict) -> dict:
    section = (config.get("direct_link") or {}).get("resolver") or {}
    try:
        parallel_workers = int(section.get("parallel_workers", _DEFAULT_PARALLEL_WORKERS))
    except (TypeError, ValueError):
        parallel_workers = _DEFAULT_PARALLEL_WORKERS
    parallel_workers = max(_MIN_PARALLEL_WORKERS, min(_MAX_PARALLEL_WORKERS, parallel_workers))
    return {
        "enabled": bool(section.get("enabled", True)),
        "max_attempts": int(section.get("max_attempts", _DEFAULT_MAX_ATTEMPTS)),
        "recheck_days": int(section.get("recheck_days", _DEFAULT_RECHECK_DAYS)),
        "max_companies_per_run": int(
            section.get("max_companies_per_run", _DEFAULT_MAX_COMPANIES_PER_RUN)
        ),
        "llm_tiebreak": bool(section.get("llm_tiebreak", True)),
        "llm_tiebreak_max_board": int(section.get("llm_tiebreak_max_board", DEFAULT_MAX_BOARD)),
        "parallel_workers": parallel_workers,
    }


def _parse_source_urls(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [u for u in raw if isinstance(u, str)]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [u for u in parsed if isinstance(u, str)] if isinstance(parsed, list) else []


def _parse_postings(raw: Any) -> list[dict]:
    """Parse the jobs.postings JSON column, tolerating NULL / junk."""
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _resolve_company(
    item: tuple[int, dict],
    *,
    run_platform_scan: Any,
    resolve_primary_posting: Any,
    scanners_by_name: dict[str, Any],
    non_scannable: frozenset[str],
    delay_range: tuple[float, float],
) -> dict:
    """Fetch one company's board and match every candidate job in memory.

    This is the pure network + in-memory half of the fetch/diff split: the
    caller (the main thread) holds the single sqlite3 connection and applies
    all DB writes serially. Returning a skipped marker for non-scannable
    platforms keeps the "no attempt burned on skip" invariant.
    """
    company_id, group = item
    platform = group["platform"]
    slug = group["slug"]
    scanner = scanners_by_name.get(platform)
    if scanner is None or platform in non_scannable:
        return {
            "company_id": company_id,
            "platform": platform,
            "postings": [],
            "matches": [],
            "skipped": True,
            "skip_reason": "unsupported",
        }

    # Per-request politeness jitter (not a global sleep); the shared Session
    # in _http_session.py adds per-host concurrency pacing on top of this.
    if delay_range[1] > 0:
        time.sleep(random.uniform(*delay_range))  # noqa: S311

    try:
        postings, _ = run_platform_scan(scanner, slug, [], [])
    except Exception as exc:
        # An uncaught scanner exception is a genuine anomaly, not the
        # registry's own "no postings or fetch failed -> []" contract (which
        # intentionally burns an attempt, repaired by the decay window).
        # Treat it like an unsupported platform — no attempt burned — instead
        # of letting it escape executor.map and abort iteration before sibling
        # in-flight results for other companies get applied.
        logger.warning(
            "Board fetch raised for company_id=%s platform=%s (%s) — skipping, no attempt burned",
            company_id,
            platform,
            exc,
        )
        return {
            "company_id": company_id,
            "platform": platform,
            "postings": [],
            "matches": [],
            "skipped": True,
            "skip_reason": "fetch_error",
        }

    matches = []
    for job in group["jobs"]:
        resolved = resolve_primary_posting(postings, job["title"] or "", job["location"] or "")
        if resolved is None:
            match = {
                "dedup_key": job["dedup_key"],
                "title": job["title"],
                "location": job["location"],
                "snippet": job["snippet"],
                "posting": None,
                "url": None,
                "confidence": None,
            }
        else:
            posting, url, confidence = resolved
            match = {
                "dedup_key": job["dedup_key"],
                "title": job["title"],
                "location": job["location"],
                "snippet": job["snippet"],
                "posting": posting,
                "url": url,
                "confidence": confidence,
            }
        matches.append(match)

    return {
        "company_id": company_id,
        "platform": platform,
        "postings": postings,
        "matches": matches,
        "skipped": False,
        "skip_reason": None,
    }


def _promote_existing(conn: sqlite3.Connection, stats: dict) -> None:
    """Stage 1: promote source_urls already on an ATS/careers host (free)."""
    svc = get_services()  # PORT-SEAM: ScanServices seam (L-0229)
    rows = conn.execute(
        "SELECT dedup_key, source_urls FROM jobs WHERE direct_url IS NULL"
    ).fetchall()
    for row in rows:
        stats["scanned"] += 1
        promoted = promote_existing_direct_url(_parse_source_urls(row["source_urls"]))
        if (
            promoted
            and svc.set_direct_url is not None  # PORT-SEAM: db._direct_link.set_direct_url seam
            and svc.set_direct_url(conn, row["dedup_key"], promoted, "strict")
        ):
            stats["promoted"] += 1
            stats["resolved"] += 1
            stats["strict"] += 1


def resolve_primary_sources(
    conn: sqlite3.Connection,
    config: dict,
    *,
    max_companies: int | None = None,
    delay_range: tuple[float, float] = (1.0, 2.0),
) -> dict:
    """Run one resolution pass. Returns counters for activity logging.

    Keys: scanned (NULL-direct_url rows examined for promotion), promoted,
    companies_scanned, companies_skipped (platform without a public API /
    unknown — no attempt burned), jobs_checked (board-match attempts),
    resolved, strict, loose, merged (strict matches whose fields folded in),
    llm_checked (loose matches sent to the quick-tier tie-breaker),
    llm_upgraded (tie-breaker verdicts that promoted loose -> strict).
    """
    svc = get_services()  # PORT-SEAM: ScanServices seam (L-0229)
    settings = _resolver_settings(config)
    if max_companies is None:
        max_companies = settings["max_companies_per_run"]
    now = utc_now_iso()
    decay_cutoff = (
        datetime.now(UTC).replace(tzinfo=None) - timedelta(days=settings["recheck_days"])
    ).isoformat()

    stats = {
        "scanned": 0,
        "promoted": 0,
        "companies_scanned": 0,
        "companies_skipped": 0,
        "jobs_checked": 0,
        "resolved": 0,
        "strict": 0,
        "loose": 0,
        "merged": 0,
        "llm_checked": 0,
        "llm_upgraded": 0,
    }

    _promote_existing(conn, stats)

    candidates = conn.execute(_CANDIDATE_SQL, (settings["max_attempts"], decay_cutoff)).fetchall()
    by_company: dict[int, dict] = {}
    for row in candidates:
        group = by_company.setdefault(
            row["company_id"],
            {"platform": row["ats_platform"], "slug": row["ats_slug"], "jobs": []},
        )
        group["jobs"].append(row)

    # Deferred import: the platform package pulls in the scanner modules and
    # requests; the scheduler only pays that cost when the job actually runs.
    from jobcannon.engine.ats_platforms import SCANNERS_BY_NAME
    from jobcannon.engine.ats_platforms._registry import run_platform_scan
    from jobcannon.engine.ats_registry import NON_SCANNABLE_PLATFORMS

    # Pre-filter to scannable companies (no scanner, or an explicit
    # non-scannable platform, can never resolve via a board fetch) BEFORE the
    # max_companies_per_run slice, so a non-scannable cohort never consumes a
    # scannable company's cap slot (#1131). companies_skipped reflects the
    # full non-scannable cohort, not just what the cap window would have seen.
    scannable_companies = [
        (cid, group)
        for cid, group in by_company.items()
        if SCANNERS_BY_NAME.get(group["platform"]) is not None
        and group["platform"] not in NON_SCANNABLE_PLATFORMS
    ]
    stats["companies_skipped"] = len(by_company) - len(scannable_companies)

    work_items = scannable_companies[:max_companies]
    tiebreak_enabled = settings["llm_tiebreak"]

    if work_items:
        # A cohort that fits inside a single dispatch batch has nothing to
        # stagger — jitter exists to avoid a thundering herd across
        # sequential batches, which can't happen when every item is already
        # in flight from the first dispatch.
        effective_delay_range = (
            delay_range if len(work_items) > settings["parallel_workers"] else (0.0, 0.0)
        )
        resolve_one = partial(
            _resolve_company,
            run_platform_scan=run_platform_scan,
            resolve_primary_posting=resolve_primary_posting,
            scanners_by_name=SCANNERS_BY_NAME,
            non_scannable=NON_SCANNABLE_PLATFORMS,
            delay_range=effective_delay_range,
        )

        with ThreadPoolExecutor(max_workers=settings["parallel_workers"]) as executor:
            for result in executor.map(resolve_one, work_items):
                if result["skipped"]:
                    stats["companies_skipped"] += 1
                    continue
                stats["companies_scanned"] += 1

                checked: list[str] = []
                for match in result["matches"]:
                    dedup_key = match["dedup_key"]
                    checked.append(dedup_key)
                    stats["jobs_checked"] += 1
                    if not result["postings"]:
                        continue
                    # No heuristic match (resolve_primary_posting returned None
                    # for a no-exact-title-match job, #1932). Only the LLM
                    # tie-breaker can recover a link from the board now; skip
                    # the rest of the loop body when it's disabled so we don't
                    # burn a per-job DB read for nothing.
                    if match["url"] is None and not tiebreak_enabled:
                        continue

                    # Phase 5 (#643): Check if the row has postings sub-entities.
                    # If so, a strict match to a board posting should annotate the
                    # corresponding descriptor instead of the row-level direct_url.
                    row = conn.execute(
                        "SELECT dedup_key, postings, source_urls FROM jobs WHERE dedup_key = ?",
                        (dedup_key,),
                    ).fetchone()
                    descriptors = _parse_postings(row["postings"]) if row else []
                    source_urls = _parse_source_urls(row["source_urls"]) if row else []
                    aggregator_url = source_urls[0] if source_urls else None

                    posting = match["posting"]
                    url = match["url"]
                    confidence = match["confidence"]

                    # Loose/ambiguous heuristic match: one quick-tier verdict can
                    # upgrade it to strict (Phase 4). Only confident=true with a
                    # valid index does — everything else stays loose (P13).
                    source_tag = None
                    if (
                        posting is None
                        and tiebreak_enabled
                        and svc.tiebreak_primary_posting is not None  # PORT-SEAM: L-0230 HOLD
                    ):
                        stats["llm_checked"] += 1
                        try:
                            upgraded = svc.tiebreak_primary_posting(  # PORT-SEAM: L-0230 HOLD
                                result["postings"],
                                match["title"] or "",
                                match["location"] or "",
                                match["snippet"],
                                conn,
                                config,
                                job_id=dedup_key,
                                max_board=settings["llm_tiebreak_max_board"],
                            )
                        except Exception as exc:
                            # Cascade exhausted / providers down: stop tie-breaking
                            # for this run instead of timing out once per loose job.
                            logger.warning(
                                "LLM tie-break unavailable (%s) — disabled for the rest of this run",
                                exc,
                            )
                            tiebreak_enabled = False
                            upgraded = None
                        if upgraded is not None:
                            posting = upgraded
                            url = _posting_link(upgraded) or url
                            confidence = "strict"
                            source_tag = "primary_source_llm"
                            stats["llm_upgraded"] += 1

                    # Phase 5 (#643): If we have a strict match and the row has descriptors,
                    # try to annotate the matching descriptor instead of row-level merge.
                    if posting is not None and confidence == "strict" and descriptors:
                        ats_platform = result["platform"]
                        source_id = posting.get("source_id")
                        if source_id and aggregator_url:
                            # Strict match to a specific descriptor — annotate it via the
                            # atomic keyed-union writer (db._postings.annotate_posting_apply_url).
                            # This re-reads the current postings inside an IMMEDIATE transaction,
                            # so concurrent upsert_posting additions to OTHER descriptors survive.
                            if (
                                svc.annotate_posting_apply_url is not None  # PORT-SEAM: L-0075
                                and svc.annotate_posting_apply_url(
                                    conn, dedup_key, ats_platform, source_id, aggregator_url
                                )
                            ):
                                stats["resolved"] += 1
                                stats["strict"] += 1
                                # Continue to next job — no row-level merge needed.
                                # NOTE: Phase 5 scope deliberately skips merge_primary_posting_fields
                                # on the descriptor-annotation path. Row-level salary/posted_date/location
                                # folding is deferred to a later phase when those fields are added to the
                                # descriptor shape (the descriptor currently has only
                                # locations_structured + workplace_type from Phase 1).
                                continue

                    # No strict descriptor match or no descriptors — fall through to row-level
                    if (
                        svc.set_direct_url is not None
                        and svc.set_direct_url(  # PORT-SEAM: db._direct_link.set_direct_url seam
                            conn, dedup_key, url, confidence
                        )
                    ):
                        stats["resolved"] += 1
                        stats["strict" if confidence == "strict" else "loose"] += 1
                    # Strict match only: fold the posting's authoritative fields in.
                    # merge_primary_posting_fields never raises (logs and returns
                    # False), so one bad posting cannot abort the run.
                    if posting is not None and merge_primary_posting_fields(
                        conn,
                        {"dedup_key": dedup_key},
                        posting,
                        source_tag=source_tag,
                        config=config,
                    ):
                        stats["merged"] += 1

                if (
                    svc.stamp_direct_url_checks is not None
                ):  # PORT-SEAM: db._direct_link.stamp_direct_url_checks seam
                    svc.stamp_direct_url_checks(conn, checked, now)

    logger.info("resolve_primary_sources: %s", stats)
    return stats


def run_primary_source_resolution(db_path: str, config: dict) -> dict:
    """Scheduler entry point — own connection (APScheduler thread safety).

    Uses ``standalone_connection`` so this 5:45 AM job gets WAL +
    busy_timeout=30000 and waits (rather than raising "database is locked")
    when it contends with the 5:00 careers_crawl / company_linkage jobs or
    Flask HTMX polling.
    """
    svc = get_services()  # PORT-SEAM: ScanServices seam (L-0229)
    with svc.connection_factory() as conn:  # PORT-SEAM: ScanServices seam
        return resolve_primary_sources(conn, config)
