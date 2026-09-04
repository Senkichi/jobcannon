# PORTED from job_finder/web/careers_crawler/_autoheal_seam.py @ 6fc7fb1b50d3ff1fe880614471cda8303a68be35 (private job-cannon). Ledger L-0469.
"""Careers-crawler autoheal seam — override-first extraction + capture (Phase D / D4).

Shared by the static and Playwright tiers so the override/shadow/capture
logic is identical at every extraction site. Both functions are fail-open:
an override or capture error must never break crawling.

Override-first with generic shadow: when a per-company override recipe
exists and yields title-matched jobs, its results are used and the GENERIC
structural count rides along as ``legacy_count`` — D2's shadow machinery
then retires a stale override that the generic extractor structurally
outperforms ``SHADOW_ROLLBACK_WINS`` times consecutively (costing nothing:
the soup the generic count comes from is already parsed at every site).

# PORT-SEAM: the private autoheal subsystem (override_loader,
# recipe_extractor, health_monitor.record_extraction) has no public
# counterpart yet -- three flat optional ScanServices fields
# (`load_careers_override`, `extract_careers_recipe`,
# `record_careers_extraction`) stand in per the wave-3 crawler-cascade
# design note, which supersedes this row's own ledger `seam` text ("an
# autoheal service handle"). `careers_source_key` is a pure key-builder,
# ported inline below as engine code rather than a fourth seam field.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse  # PORT-SEAM: for careers_source_key, inlined below (L-0469)

from jobcannon.engine.ats_platforms import _title_matches
from jobcannon.engine.services import get_services  # PORT-SEAM: seam import (L-0469)

logger = logging.getLogger(__name__)


def careers_source_key(url: str) -> str:
    """Per-company careers source key: ``careers:{hostname}``.

    Hostname only — lowercase, port stripped (invariant I5: ``:`` is illegal
    in NTFS filenames, and the key doubles as the override file key in D4).
    Falls back to ``careers:unknown`` for garbage/empty URLs.
    """
    # PORT-SEAM: pure key-builder ported inline as engine code, not a seam
    # (job_finder.web.autoheal.careers_source_key) (L-0469)
    host = (urlparse(url or "").hostname or "").lower()
    return f"careers:{host}" if host else "careers:unknown"


def try_careers_override(
    html: str,
    url: str,
    target_titles: list[str],
    exclusions: list[str],
) -> tuple[list[dict], int | None]:
    """Apply the per-company careers override to *html*, when one exists.

    Returns ``(filtered_jobs, structural_count)`` — ``([], None)`` when no
    override file exists for the company or anything fails. *structural_count*
    is the override's pre-title-filter yield (I4: the detection signal).
    """
    svc = get_services()  # PORT-SEAM: seam (L-0469)
    if svc.load_careers_override is None or svc.extract_careers_recipe is None:
        # PORT-SEAM: optional seam -- host didn't wire autoheal overrides (L-0469)
        return [], None
    try:
        recipe = svc.load_careers_override(careers_source_key(url))  # PORT-SEAM: seam call (L-0469)
        if recipe is None:
            return [], None
        raw = svc.extract_careers_recipe(recipe, html, url)  # PORT-SEAM: seam call (L-0469)
        matched = [d for d in raw if _title_matches(d["title"], target_titles, exclusions)]
        return matched, len(raw)
    except Exception:
        logger.exception("careers override failed for '%s'; using generic path", url)
        return [], None


def record_careers_capture(
    # PORT-SEAM: db_path param dropped -- svc.connection_factory() is zero-arg (L-0469)
    url: str,
    html: str,
    *,
    generic_structural: int,
    override_structural: int | None,
    used_override: bool,
    filtered_count: int,
) -> None:
    """Record the per-company corpus sample + break-counter update. Never raises.

    Structural counts only (I4): "your roles were filled" must not look like
    "the page broke". When the override produced the returned jobs, the
    generic structural count is passed as ``legacy_count`` (shadow guard).
    """
    svc = get_services()  # PORT-SEAM: seam (L-0469)
    if svc.record_careers_extraction is None:
        # PORT-SEAM: optional seam -- host didn't wire autoheal capture (L-0469)
        return
    try:
        with svc.connection_factory() as conn:  # PORT-SEAM: seam (L-0469)
            svc.record_careers_extraction(  # PORT-SEAM: seam call (L-0469)
                conn,
                careers_source_key(url),
                "careers",
                html[:50000],
                job_count=override_structural if used_override else generic_structural,
                detect=True,
                legacy_count=generic_structural if used_override else None,
                extractor="override" if used_override else "generic",
                filtered_count=filtered_count,
            )
            conn.commit()
    except Exception:
        pass  # observability must never break ingestion
