"""PlatformScanner registry + shared scan driver.

A ``PlatformScanner`` value object captures everything per-platform that
changes between Lever / Greenhouse / Ashby / etc.: how to fetch the
posting list, how to extract the title, and how to turn one raw posting
into the canonical job dict. The driver (``run_platform_scan``) owns the
title-match gate and the final result-count log line that every
historical ``scan_*`` function used to emit.

The shared HTTP helper ``_http_get_json`` consolidates the
GET → status-200 → JSON-parse spine that every simple-shape scanner
duplicates. It supports a single timeout retry (used by Ashby) and
optional ``params`` / ``headers``. Platforms with shapes the helper
cannot express (Workday POST + pagination, Personio XML + multi-TLD,
BambooHR HTML) own their own HTTP inside ``fetch_postings``.

All HTTP calls use a shared ``requests.Session`` with connection pooling
(via ``_http_session.get_session()``) to avoid repeated TCP+TLS handshakes.
``get_session`` is imported into this module's own namespace (see below), so
tests must patch ``jobcannon.engine.ats_platforms._registry.get_session`` (the
local binding actually called by ``_http_get_json``) — patching
``_http_session.get_session`` on the origin module has no effect, since this
module already holds its own reference to the original function.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import requests

from jobcannon.engine import extraction_health
from jobcannon.engine.ats_platforms._http_session import get_session
from jobcannon.engine.ats_prober import _PROBE_TIMEOUT
from jobcannon.engine.runtime_config import get_runtime_config

logger = logging.getLogger(__name__)


# HTTP statuses that mean an ATS board/slug no longer resolves (permanent),
# as opposed to transient 5xx / rate-limit 403 / network blips.
BOARD_GONE_STATUSES = frozenset({404, 410})

# Short-TTL memo of RAW (pre-title-filter) postings per (scanner.name, slug, max_pages),
# so N queued listings at the same company within a burst share one fetch
# instead of re-hitting a paginated board (e.g. Workday) once per listing.
# Keyed on monotonic time — this is a pure in-memory cache, never persisted.
_scan_memo: dict[tuple[str, str, int | None], tuple[list[dict], float]] = {}
_scan_memo_lock = threading.Lock()
# Per-key in-flight lock so concurrent callers for the same (scanner, slug,
# max_pages) serialize on the first fetch instead of launching N live fetches.
# Guarded by _scan_memo_inflight_lock; see run_platform_scan non-force path.
_scan_memo_inflight: dict[tuple[str, str, int | None], threading.Lock] = {}
_scan_memo_inflight_lock = threading.Lock()
# Default fallback TTL for the scan memo, overridden by config.ats.scan_memo_ttl_s.
# Default 28,800 s = 8 h, so the read-only consumers (reconciler, resolver,
# enrichment) can reuse the same full-board fetch for the same day after ats_scan.
_SCAN_MEMO_TTL_SECONDS = 28800


def _auth_block_statuses() -> frozenset[int]:
    """Return the set of HTTP statuses treated as auth/anti-bot walls.

    Reads from ``health.auth_block_statuses`` via the host's injected runtime-
    config provider when one is registered. Falls back to the default
    ``{401, 403, 429}`` when no provider is registered or the key is absent.

    Returns:
        Frozenset of HTTP status codes that should log at WARNING instead of
        DEBUG when encountered by a scanner.
    """
    try:
        return frozenset(
            get_runtime_config().get("health", {}).get("auth_block_statuses", [401, 403, 429])
        )
    except (RuntimeError, AttributeError):
        # No provider registered (e.g. tests) or key missing: use default
        return frozenset({401, 403, 429})


def _get_scan_memo_ttl_seconds() -> int:
    """Return the scan memo TTL in seconds.

    Reads from ``config.ats.scan_memo_ttl_s`` via the host's injected runtime-
    config provider when one is registered, otherwise falls back to
    ``_SCAN_MEMO_TTL_SECONDS``. Invalid values are ignored and the default is
    used.
    """
    try:
        return int(
            get_runtime_config().get("ats", {}).get("scan_memo_ttl_s", _SCAN_MEMO_TTL_SECONDS)
        )
    except (RuntimeError, AttributeError, TypeError, ValueError):
        # No provider registered, missing key, or non-integer value: use default
        return _SCAN_MEMO_TTL_SECONDS


class BoardGoneError(Exception):
    """A previously-discovered ATS board no longer exists.

    Raised by a platform's completeness-fetch when the FIRST page of the board
    returns a definitively-gone status (404 Not Found / 410 Gone) — the
    tenant/slug stopped resolving, not a transient 5xx/403 and not an
    empty-but-live board (HTTP 200, zero postings, which must stay a hit).

    The scan driver lets this propagate so ``_scan_one_company_via_ats_api`` can
    demote a stale ``hit`` to ``miss/platform_slug_gone`` (clearing
    ``scan_enabled``) instead of logging "0 fetched" against a dead board every
    run forever (e.g. Walmart's 410'd Workday slug). The ATS reconciler and the
    enrichment ATS-query path catch it and degrade to "no data" (no expiry, no
    enrichment) rather than crashing.
    """

    def __init__(self, status: int, slug: str):
        self.status = status
        self.slug = slug
        super().__init__(f"ATS board gone (HTTP {status}): {slug}")


# ── Structured-field CAPTURE helpers (#451) ──────────────────────────────────
# Shared raw-as-provided extraction for the is_remote / employment_type /
# department capture columns. Both helpers are pure and return None when the
# value is absent — capture never synthesizes a value the payload does not
# carry (epic #393, CAPTURE stage).


def coerce_remote_bool(value: Any) -> bool | None:
    """Coerce a provider ``isRemote`` / ``remote`` value to a tri-state bool.

    ``None`` (field absent) stays ``None`` — distinct from an explicit
    ``False`` — so the NULL-is-unknown semantics of the ``is_remote`` column
    survive. Any present value is coerced with ``bool()``.
    """
    if value is None:
        return None
    return bool(value)


def label_or_str(value: Any) -> str | None:
    """Extract a raw string from a provider field that may be an object.

    SmartRecruiters emits ``typeOfEmployment`` / ``department`` as
    ``{"id": ..., "label": ...}`` objects; Ashby / Lever emit plain strings.
    Returns the ``label`` for dict inputs, the string itself for str inputs,
    and ``None`` for anything empty or absent.
    """
    if isinstance(value, dict):
        label = value.get("label")
        return label or None
    if isinstance(value, str):
        return value or None
    return None


@dataclass(frozen=True, slots=True)
class PlatformScanner:
    """Per-platform contract for the shared scan driver.

    Attributes:
        name: Lowercase platform key matching ``companies.ats_platform``
            (e.g. ``"lever"``, ``"greenhouse"``). Used in log messages.
        company_source: Display-cased platform name written into the
            ``company_source`` field of each job dict (e.g. ``"Lever"``).
        fetch_postings: ``(slug, max_pages) -> list[dict]``. Owns all HTTP +
            pagination + response-format-specific parsing. Must catch its own
            exceptions and return ``[]`` on any error so one platform's outage
            cannot crash a whole multi-company scan. The ``max_pages`` parameter
            is optional for non-paginated platforms.
        title_of: ``posting -> str``. Pulls the title string out of one
            raw posting dict for the title-match gate.
        posting_to_job: ``(posting, slug) -> dict | None``. Builds the
            canonical job dict ``{title, company_source, location,
            description, source_url, salary_min, salary_max, comp_json}``
            for one posting. Returning ``None`` skips the posting (e.g.
            BambooHR's "anchor missing" case).
        detail_fetch: Optional ``posting -> dict`` callable that fetches
            per-posting detail data (e.g. full description) for platforms
            that require a secondary GET. When provided, the scan driver
            parallelizes detail fetches via a bounded ThreadPoolExecutor
            before calling ``posting_to_job``. Platforms with single-request
            list endpoints (Greenhouse/Lever/Ashby) leave this ``None``.
        fetch_postings_with_completeness: Optional ``(slug, max_pages) -> (list[dict], bool)``
            callable that fetches postings with a completeness flag, so a
            caller can determine if a board was fully fetched. This field is
            forward-wiring for the reconciler chain (issues #1030-1033) and
            currently has no callers — ``ats_reconciler.py`` imports the
            Workday and SmartRecruiters completeness functions directly by
            private name instead of reading this field off the scanner, and
            Microsoft/Eightfold aren't wired into the reconciler at all yet.
            Platforms with paginated list endpoints (Workday, SmartRecruiters,
            Microsoft, Eightfold, Oracle Cloud) should provide this.
    """

    name: str
    company_source: str
    fetch_postings: Callable[[str, int | None], list[dict]]
    title_of: Callable[[dict], str]
    posting_to_job: Callable[[dict, str], dict | None]
    detail_fetch: Callable[[dict], dict] | None = None
    fetch_postings_with_completeness: (
        Callable[[str, int | None], tuple[list[dict], bool]] | None
    ) = None


def _get_cached_postings(scanner_name: str, slug: str, max_pages: int | None) -> list[dict] | None:
    """Return cached postings if present and within TTL, else None."""
    ttl = _get_scan_memo_ttl_seconds()
    with _scan_memo_lock:
        entry = _scan_memo.get((scanner_name, slug, max_pages))
        if entry is None:
            return None
        postings, cached_at = entry
        if time.monotonic() - cached_at > ttl:
            return None
        return list(postings)


def _store_cached_postings(
    scanner_name: str, slug: str, max_pages: int | None, postings: list[dict]
) -> None:
    with _scan_memo_lock:
        _scan_memo[(scanner_name, slug, max_pages)] = (postings, time.monotonic())


def run_platform_scan(
    scanner: PlatformScanner,
    slug: str,
    target_titles: list[str],
    exclusions: list[str],
    *,
    max_pages: int | None = None,
    force_fresh: bool = False,
    conn: sqlite3.Connection | None = None,
    return_raw: bool = False,
) -> tuple[list[dict], int] | tuple[list[dict], int, list[dict]]:
    """Run one platform scan: fetch → raw capture → title gate → normalize → log.

    The behavior matches the historical per-platform ``scan_*`` body:
    every raw posting that ``_title_matches`` accepts is normalized via
    ``scanner.posting_to_job`` and appended to the result list. The
    debug-level count log fires once at the end with the same shape the
    Lever / Greenhouse / Ashby / Pinpoint scanners already used.

    The raw (pre-filter) postings list is memoized per
    ``(scanner.name, slug, max_pages)`` for ``config.ats.scan_memo_ttl_s`` so
    N queued listings at the same company within a burst share one
    ``fetch_postings`` call. The title gate below always re-runs against this
    call's own ``target_titles``/``exclusions``, so a cache hit never returns
    another call's filtered results.

    When the scanner provides a ``detail_fetch`` callable (Workday /
    SmartRecruiters), matched postings' details are fetched in parallel via
    a bounded ThreadPoolExecutor before the build step. The concurrency
    bound is read from ``config.ats.detail_fetch_concurrency`` (default 4).

    Args:
        scanner: The platform's ``PlatformScanner`` value object.
        slug: Per-company platform identifier (e.g. Lever's
            ``"stripe"``, Workday's ``"walmart.wd5/WalmartExternal"``).
        target_titles: Title-match keywords for inclusion. Empty list
            allows all titles through (the config layer is expected to
            forbid this; the gate respects it for completeness).
        exclusions: Title-match keywords for exclusion. AND-NOT semantics.
        max_pages: Optional page budget for paginated platforms. Passed
            through to the platform's fetch_postings implementation.
        force_fresh: When True, skip the memo read and always call the
            scanner's ``fetch_postings``. The fresh result is still written
            into the memo so downstream consumers can share it. This is the
            discovery-freshness invariant used by ``ats_scan``.
        conn: Optional DB connection.  When provided the raw pre-filter
            API response is recorded via ``extraction_health.record(...)``
            with ``detect=True`` so an empty response on a previously-productive
            platform is detected as a true break.  The ~19 callers in
            ``ats_platforms/__init__.py`` and ``ats_reconciler.py`` omit
            this argument (``conn=None``) and are unaffected.
        return_raw: When True, also return the full pre-title-gate list of
            job dicts so callers can cache the entire board.  This keeps the
            default 2-tuple return unchanged for the ~19 existing callers.

    Returns:
        Tuple of (matched_job_dicts, skipped_count) by default.  When
        ``return_raw=True``, a 3-tuple of
        (matched_job_dicts, skipped_count, raw_job_dicts) where
        ``raw_job_dicts`` is the full board before title filtering.
        Empty list on fetch error or no matches.
    """
    # Lazy import — once ats_platforms.py's scan_X bodies delegate to this
    # driver (F1 Commit 2), the import graph becomes
    # ats_platforms -> _registry -> ats_platforms. A module-level
    # ``from ats_platforms import _title_matches`` would race that cycle;
    # the function-local import resolves only after ats_platforms is
    # fully loaded and is cheap because Python caches the module lookup.
    from jobcannon.engine.ats_platforms import _title_matches

    if force_fresh:
        # Discovery consumers must always see the live board. Always fetch,
        # but still write the result so read-only consumers (reconciler,
        # resolver, enrichment) can share the same payload.
        # BoardGoneError is intentionally not cached: a gone board must keep
        # raising so the caller can demote the stale hit.
        postings = list(scanner.fetch_postings(slug, max_pages=max_pages))
        _store_cached_postings(scanner.name, slug, max_pages, postings)
    else:
        key = (scanner.name, slug, max_pages)
        postings = _get_cached_postings(*key)
        if postings is None:
            # Serialize same-key concurrent callers so only one thread fetches
            # the full board while the others block and reuse the cached result.
            with _scan_memo_inflight_lock:
                key_lock = _scan_memo_inflight.get(key)
                if key_lock is None:
                    key_lock = threading.Lock()
                    _scan_memo_inflight[key] = key_lock
            with key_lock:
                # Re-check inside the lock in case the first fetcher stored
                # the result while this thread was waiting for the lock.
                postings = _get_cached_postings(*key)
                if postings is None:
                    # BoardGoneError must propagate uncached — a gone board needs to keep
                    # failing on every call so the caller can demote the stale hit; caching
                    # the exception would hide the failure for the rest of the TTL window.
                    postings = list(scanner.fetch_postings(slug, max_pages=max_pages))
                    _store_cached_postings(*key, postings)

    # --- Autoheal Phase B: capture raw pre-filter API response ---
    # Runs on every call, cache hit or miss — extraction_health.record()
    # forwards to a host-registered recorder (e.g. the private repo's
    # health-monitor break-detection baseline), which needs a sample per
    # scan, not just per network fetch. No-op when no recorder is registered.
    # detect=True is honest here: len(postings)==0 on a platform that
    # previously returned jobs is a genuine API break (shape changed,
    # auth expired, …), not a post-filter false-alarm.
    if conn is not None:
        try:
            raw = json.dumps(postings)[:50000]
            # The health-monitor's min-meaningful-length gate was designed for
            # email bodies where a very short body is a meta/empty email.
            # For ATS, any API response — including [] — is a meaningful
            # result.  Pad to the threshold so genuine empty-API breaks on
            # a previously-productive platform actually fire the break counter.
            if len(raw) < extraction_health.min_meaningful_len():
                raw = raw.ljust(extraction_health.min_meaningful_len())

            extraction_health.record(
                conn=conn,
                source=f"ats:{scanner.name}",
                surface="ats",
                payload=raw,
                job_count=len(postings),
                detect=True,
            )
        except Exception:
            pass  # observability must never break ingestion

    # Title-match gate: collect matched indices (still against raw postings so
    # the skipped_count is exact and the title-gate contract is unchanged).
    matched_indices: list[int] = []
    skipped_count = 0
    for i, posting in enumerate(postings):
        title = scanner.title_of(posting)
        if not _title_matches(title, target_titles, exclusions):
            skipped_count += 1
            continue
        matched_indices.append(i)
    matched_set = set(matched_indices)

    # Parallel detail fetch for platforms that require it (Workday / SmartRecruiters)
    details_by_index: dict[int, dict] = {}
    if scanner.detail_fetch is not None and matched_indices:
        matched_postings = [postings[i] for i in matched_indices]

        # Read concurrency bound from config (default 4, range 1-6)
        try:
            concurrency = get_runtime_config().get("ats", {}).get("detail_fetch_concurrency", 4)
            # Clamp to sane range (floor 1, not 4 — operators must be able to
            # throttle to 1 during vendor rate-limit incidents)
            concurrency = max(1, min(6, int(concurrency)))
        except (RuntimeError, AttributeError, TypeError, ValueError):
            # No provider registered or invalid config: use default
            concurrency = 4

        # Fetch details in parallel, preserving input order
        def _fetch_one(posting: dict, index: int) -> tuple[int, dict]:
            """Fetch detail for one posting, returning (local_index, detail_dict)."""
            detail = scanner.detail_fetch(posting)  # type: ignore[arg-type]
            return index, detail

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(_fetch_one, p, i): p for i, p in enumerate(matched_postings)}
            for future in as_completed(futures):
                try:
                    local_index, detail = future.result()
                    original_index = matched_indices[local_index]
                    # Merge fetched details into a copy of the posting dict so
                    # the cached raw postings in _scan_memo stay untouched.
                    merged = dict(matched_postings[local_index])
                    merged.update(detail)
                    details_by_index[original_index] = merged
                except Exception as exc:
                    # One posting's failed detail fetch degrades that posting
                    # exactly as the serial path does (same fallback fields/logging).
                    # The detail dict remains absent; posting_to_job handles it.
                    logger.debug(
                        "detail_fetch failed for posting: %s",
                        exc,
                    )

    # Build job dicts for the full board, using the detail-augmented posting for
    # matched entries and a no-network copy for non-matched entries.  The latter
    # sets __fetched_description to a sentinel empty string so that scanners with
    # lazy per-job detail fetches (Workday, SmartRecruiters) never issue network
    # requests for postings the caller has already filtered out.
    job_dicts_by_index: list[dict | None] = [None] * len(postings)
    for i, posting in enumerate(postings):
        if i in details_by_index:
            posting_for_job = details_by_index[i]
        elif i in matched_set:
            posting_for_job = posting
        else:
            posting_for_job = dict(posting)
            posting_for_job.setdefault("__fetched_description", "")

        job_dict = scanner.posting_to_job(posting_for_job, slug)
        job_dicts_by_index[i] = job_dict

    raw_job_dicts = [job for job in job_dicts_by_index if job is not None]
    matched_job_dicts = [
        job_dicts_by_index[i] for i in matched_indices if job_dicts_by_index[i] is not None
    ]

    # INFO (not DEBUG): per-company fetched-vs-matched is the primary live
    # observability signal during a scan — it surfaces title-filter
    # over-restriction (many fetched, 0 matched) and silent board breaks
    # (0 fetched on a previously-productive board) as they stream.
    logger.info(
        "scan_%s('%s'): %d postings fetched, %d matched, %d skipped by title filter",
        scanner.name,
        slug,
        len(postings),
        len(matched_job_dicts),
        skipped_count,
    )
    if return_raw:
        return matched_job_dicts, skipped_count, raw_job_dicts
    return matched_job_dicts, skipped_count


def _http_get_json(
    url: str,
    log_label: str,
    slug: str,
    *,
    retry_on_timeout: bool = False,
    params: dict | None = None,
    headers: dict | None = None,
) -> Any:
    """GET + 200-check + JSON-parse, with optional single timeout retry.

    Replaces the GET → status check → ``resp.json()`` try/except spine
    that every simple-shape scanner duplicates. Returns the parsed JSON
    on success, ``None`` on any failure (connection error, timeout,
    non-200, JSON parse error). Callers turn ``None`` into ``[]``.

    The ``retry_on_timeout`` knob exists for Ashby: a 2026-05-26 incident
    showed Ashby returning Read timeouts for ~20 tenants in sequence over
    a 9-minute window. A fresh attempt 2s later typically succeeds. One
    retry is enough; more would double the run time of a sustained
    outage with no benefit.

    Args:
        url: Target URL.
        log_label: Per-scanner label for warning/debug log lines
            (e.g. ``"scan_lever"``).
        slug: Per-company identifier; included in log lines.
        retry_on_timeout: When True, swallow a single
            ``requests.exceptions.Timeout`` and retry once after 2 s.
        params: Optional query parameters passed to the GET request.
        headers: Optional request headers passed to the GET request.

    Returns:
        Parsed JSON value (dict, list, etc.) on success; ``None`` on any
        failure path.
    """
    resp = None
    for attempt in (1, 2):
        try:
            resp = get_session().get(url, params=params, headers=headers, timeout=_PROBE_TIMEOUT)
            break
        except requests.exceptions.Timeout as exc:
            if retry_on_timeout and attempt == 1:
                logger.debug("%s('%s') timed out attempt 1, retrying in 2s", log_label, slug)
                time.sleep(2)
                continue
            logger.warning("%s('%s') timed out: %s", log_label, slug, exc)
            return None
        except Exception as exc:
            logger.warning("%s('%s') request failed: %s", log_label, slug, exc)
            return None

    if resp is None:
        return None

    if resp.status_code != 200:
        if resp.status_code in _auth_block_statuses():
            logger.warning(
                "%s('%s') possible auth/anti-bot wall: HTTP %d",
                log_label,
                slug,
                resp.status_code,
            )
        else:
            logger.debug("%s('%s') returned HTTP %d", log_label, slug, resp.status_code)
        return None

    try:
        return resp.json()
    except Exception as exc:
        logger.warning("%s('%s') JSON parse error: %s", log_label, slug, exc)
        return None
