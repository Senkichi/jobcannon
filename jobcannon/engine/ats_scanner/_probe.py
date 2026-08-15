"""Speculative ATS-API slug probing for companies with pending probe_status.

Extracted from ats_scanner/__init__.py during S7c (portfolio cleanup).
Re-exported from the package for backward compatibility.
"""

import logging
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta

from jobcannon.engine.json_utils import utc_now_iso
from jobcannon.engine.ats_detection import (
    ATS_EXTRACTOR_VERSION,
    derive_slug_candidates,
    extract_ats_from_url_best,
    probe_hit_consistent_or_dead_url,
)
from jobcannon.engine.ats_prober import (
    _probe_ashby,
    _probe_greenhouse,
    _probe_jazzhr,
    _probe_lever,
    _probe_pinpoint,
    _probe_teamtailor,
)
from jobcannon.engine.brand_blocklist import is_blocked_brand
from jobcannon.engine.services import get_services

logger = logging.getLogger(__name__)

# Re-homed from the private repo's ats_slug_challenge.py: a plain string
# constant (not a callable), so it is copied verbatim here rather than routed
# through ScanServices. Owner-anchored trigger prefix for careers_url-derived
# fast-path hits — see the _prober_extensions bundle docstring in
# jobcannon.engine.ats_prober for the sibling careers_link: prefix.
TRIGGER_PREFIX_CAREERS_URL = "careers_url:"

# Platforms excluded from the speculative ladder due to a 100% false-positive
# rate empirically observed in the 2026-05-27 ATS coverage audit. Each of these
# four platforms had every single speculative-probe hit (18 + 6 + 8 + 8 = 40
# rows) come back with `ats_evidence_trigger IS NULL` — i.e. no corroborating
# job-URL evidence. Famous-brand names (Microsoft, Amazon, Meta, YouTube,
# Accenture, EY, Leidos, IQVIA, ...) collide with real SMB tenants that
# registered the same {slug}={normalized_name} on these platforms, and the
# probe returns a true 200 for the wrong company. F8 brand_blocklist catches
# some but not all of the cohort.
#
# These platforms can still be PROMOTED via the evidence-based reconcile path
# (``services.reconcile_company_ats``, see engine/services.py), which requires
# corroborating job-URL evidence before writing `hit`. The per-platform probe
# functions remain available and are used by reconcile's _verify_live step.
_FP_PRONE_PLATFORMS: frozenset[str] = frozenset({"bamboohr", "personio", "recruitee", "breezy"})

# (platform, probe_fn) pairs. Ordering matches the historical ladder:
# original three (Lever / Greenhouse / Ashby) first because they have the
# longest track record; surviving Stage 4 additions follow, with the
# Pinpoint/Teamtailor/JazzHR block ordered fastest-JSON-first so cheap
# probes short-circuit before slower variants pay their cost.
#
# bamboohr / personio / recruitee / breezy are deliberately excluded —
# see _FP_PRONE_PLATFORMS above for the 100% FP rate finding.
_PROBES: list[tuple[str, Callable[[str], bool]]] = [
    ("lever", _probe_lever),
    ("greenhouse", _probe_greenhouse),
    ("ashby", _probe_ashby),
    ("jazzhr", _probe_jazzhr),
    ("pinpoint", _probe_pinpoint),
    ("teamtailor", _probe_teamtailor),
]

# Invariant: speculative ladder must not include any FP-prone platform.
# Tests assert this stays true under future edits.
assert _FP_PRONE_PLATFORMS.isdisjoint({name for name, _ in _PROBES}), (
    "speculative _PROBES ladder must not include any platform in "
    "_FP_PRONE_PLATFORMS; only the evidence-based reconcile path may "
    "promote to these platforms"
)

# B2 fast-path verified platforms. Covers every platform that
# extract_ats_from_url_best can identify, including the FP-prone ones.
# URL-evidence is strictly stronger than {slug}={name} speculation, so the
# fast-path is allowed to assign FP-prone platforms even though the
# speculative ladder cannot.
#
# **jobvite is intentionally NOT in this set** even though the URL regex
# detects it. Jobvite-hosted career sites (jobs.jobvite.com/{slug}) are
# client-side JS apps with no public unauthenticated API; the scanner is a
# stub that returns []. If we promoted these companies to
# ats_probe_status='hit', they'd be excluded from the careers_crawler
# (which filters `ats_probe_status != 'hit'` in __init__.py:226) — the only
# data path that COULD extract their jobs (via the Playwright tier). Leaving
# jobvite out of the fast-path keeps them at status='miss' so careers_crawler
# remains their eligibility owner. Companion: ats_identity_reconcile's
# reconcile path is similarly evidence-gated, and the stub scanner stays
# registered defensively so any pre-existing jobvite-tagged row is a no-op
# rather than an "unknown platform" error.
_URL_FASTPATH_PLATFORMS: frozenset[str] = frozenset(
    {
        "lever",
        "greenhouse",
        "ashby",
        "workday",
        "smartrecruiters",
        "pinpoint",
        "jazzhr",
        "teamtailor",
        "bamboohr",
        "personio",
        "recruitee",
        "breezy",
        # Round 6 -- audit B2-roadmap additions (jobvite intentionally excluded; see comment above):
        "workable",
        "paylocity",
        "rippling",
        # SuccessFactors -- public XML feed
        "successfactors",
        # ADP Workforce Now -- public JSON requisitions feed
        "adp",
    }
)


def _verify_fastpath_live(platform: str, slug: str) -> bool:
    """Liveness gate for the B2 URL-evidence fast-path (the sole caller is the
    promotion write below).

    Delegates to the registry SSOT ``ats_registry.verify_fastpath_live`` so the
    fast-path set AND the probe dispatch both derive from ``PlatformSpec`` — there
    is no hand-maintained if/elif ladder to fall out of sync. The old ladder WAS a
    third un-governed mirror (alongside ``_URL_FASTPATH_PLATFORMS`` and
    ``_RECONCILABLE_PLATFORMS``): ``successfactors`` and ``adp`` were added to
    ``_URL_FASTPATH_PLATFORMS`` (parity-forced) but never got a ladder branch, so
    this returned False for them and their careers-URL fast-path silently never
    promoted. The registry resolves the probe by name from ``ats_prober`` at call
    time, preserving the patch-ability the ladder existed for (tests patch
    ``ats_prober._probe_X`` / ``ats_prober.requests.get``). Lazy import avoids an
    import-time cycle. Pinned by test_ats_registry_completeness.py so the drift
    cannot recur.
    """
    from jobcannon.engine.ats_registry import verify_fastpath_live

    return verify_fastpath_live(platform, slug)


def _resolve_collision(
    conn: sqlite3.Connection, *, platform: str, slug: str, company_id: int, config: dict
) -> dict:
    """Shared collision handling for both write sites below (the B2 fast-path
    and the speculative ladder): consult the slug-ownership challenge
    mechanism instead of unconditionally treating every m076
    UNIQUE(ats_platform, ats_slug) collision as a permanent miss for this
    company. ``resolve_slug_collision`` / ``identity_reconcile_settings`` are
    host-supplied ``ScanServices`` fields (the private source's
    ``ats_slug_challenge`` / ``ats_identity_reconcile`` imports don't port —
    see the Task 3 ScanServices seam). With either hook unset this fails
    closed: a single speculative guess must never evict an incumbent owner
    without the identity-verification machinery to back it.
    """
    svc = get_services()
    if svc.resolve_slug_collision is None or svc.identity_reconcile_settings is None:
        logger.info(
            "_resolve_collision: slug-challenge services not configured — "
            "fail closed (no demotion) for %s/%s",
            platform,
            slug,
        )
        return {
            "demoted": False,
            "challenge": None,
            "existing_owner_id": None,
            "existing_owner_name": None,
        }
    return svc.resolve_slug_collision(
        conn,
        platform=platform,
        slug=slug,
        challenger_id=company_id,
        settings=svc.identity_reconcile_settings(config),
        config=config,
    )


_DEFAULT_COLLISION_RETRY_COOLDOWN_HOURS = 48


def _probe_platforms_concurrently(slug: str) -> list[tuple[str, bool]]:
    """Fire all platform probes for a single slug concurrently.

    Returns results in the original _PROBES order (deterministic precedence).
    An earlier-platform hit beats a later-platform hit even if the later one
    returns first.

    Args:
        slug: The slug candidate to probe across all platforms.

    Returns:
        List of (platform, hit_bool) tuples in _PROBES order.
    """
    results = {}

    def _probe_single(platform: str, probe_fn: Callable[[str], bool]) -> tuple[str, bool]:
        """Execute a single platform probe and return (platform, result)."""
        try:
            return (platform, probe_fn(slug))
        except Exception:
            # Treat any exception as a miss (same behavior as serial loop)
            return (platform, False)

    # Fire all probes concurrently
    with ThreadPoolExecutor(max_workers=len(_PROBES)) as executor:
        future_to_platform = {
            executor.submit(_probe_single, platform, probe_fn): platform
            for platform, probe_fn in _PROBES
        }

        for future in as_completed(future_to_platform):
            platform, result = future.result()
            results[platform] = result

    # Return results in the original _PROBES order for deterministic precedence
    return [(platform, results.get(platform, False)) for platform, _ in _PROBES]


def _slug_probe_settings(config: dict | None) -> dict:
    """Resolve ``config['ats']['slug_probe']`` knobs for the collision-retry sweep."""
    cfg = ((config or {}).get("ats") or {}).get("slug_probe") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    try:
        cooldown = int(
            cfg.get("collision_retry_cooldown_hours", _DEFAULT_COLLISION_RETRY_COOLDOWN_HOURS)
        )
    except (TypeError, ValueError):
        cooldown = _DEFAULT_COLLISION_RETRY_COOLDOWN_HOURS
    # Clamp to [1h, 30d] so a misconfigured value can't disable the sweep
    # (0/negative) or effectively never fire (absurdly large).
    cooldown = max(1, min(cooldown, 24 * 30))
    return {
        "collision_retry_enabled": bool(cfg.get("collision_retry_enabled", True)),
        "collision_retry_cooldown_hours": cooldown,
    }


def _reset_stale_collision_misses(conn: sqlite3.Connection, cooldown_hours: int) -> int:
    """Reset ``miss``/``collision`` companies back to ``pending`` after a cooldown.

    ``probe_ats_slugs`` only ever selects ``ats_probe_status = 'pending'`` (see
    below), so without this a company that lost a slug collision — whether or
    not ``_resolve_collision`` above demoted the prior owner — is frozen
    forever on the batch scheduler path: it can never accumulate the repeated
    challenges ``ats_slug_challenge.process_slug_challenge`` needs to demote a
    poisoned incumbent. Contrast with the manual retry route
    (``ats_prober.probe_single_company``), which has no status gate and so
    already retries a given company's identical collision branch on every
    click, accumulating challenges fine on its own.

    The cutoff is computed in Python in the canonical naive-UTC ISO-8601
    'T'-separator format (matching ``utc_now_iso``), so the comparison below is
    a plain string compare — avoids the SQLite ``datetime('now')`` separator
    pitfall documented in ``stale_detector.run_stale_detection``.

    Only commits when a row actually changed. A no-op commit here would count
    as an extra ``commit()`` call on shared per-run connections, which would
    have tripped the private test suite's ``TestDemotionPromotionAtomicity``
    invariant (exactly one commit per atomic demote+promote unit; that test
    was not carried into this port) even though this sweep touched nothing.
    """
    cutoff = (datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=cooldown_hours)).isoformat()
    cur = conn.execute(
        """UPDATE companies
           SET ats_probe_status = 'pending',
               miss_reason = NULL
           WHERE ats_probe_status = 'miss'
             AND miss_reason = 'collision'
             AND ats_probe_attempted_at IS NOT NULL
             AND ats_probe_attempted_at <= ?""",
        (cutoff,),
    )
    if cur.rowcount > 0:
        conn.commit()
    return cur.rowcount


def probe_ats_slugs(db_path: str, config: dict) -> dict:
    """Probe ATS APIs speculatively for companies with pending probe status.

    Thread-safe: opens own sqlite3 connection (same pattern as stale_detector.py).
    TESTING guard: returns early when config.get('TESTING') is True.

    Before selecting the pending cohort, resets any ``miss``/``collision``
    company whose ``ats_probe_attempted_at`` is older than
    ``ats.slug_probe.collision_retry_cooldown_hours`` (default 48h) back to
    ``pending`` — see ``_reset_stale_collision_misses``. This is what lets a
    company that lost a slug collision get re-probed at all on the batch path.

    For each pending company:
    1. Derive slug candidates from company name
    2. Try Lever, Greenhouse, Ashby, Recruitee, Breezy, JazzHR, Pinpoint,
       Teamtailor, Personio, BambooHR APIs for each candidate (in that order;
       first hit wins). New platforms are appended after the established
       three; fastest probes go earlier within the new block so we
       short-circuit before paying the cost of slower ones.
    3. F6 consistency gate (augmented with liveness check): if a hit's
       platform disagrees with the platform inferred from the company's
       `careers_url` AND that careers_url is still live (not 404/410),
       reject the hit and keep trying. Catches brand-name-collision false
       positives (e.g. 'Shopify' → Pinpoint tenant of a different small
       company) without rejecting legitimate ATS migrations where the old
       careers_url now 404s and the live probe correctly rediscovers the
       new platform.
    4. Set ats_probe_status='hit' when API returns valid postings
    5. Set ats_probe_status='miss' when all APIs fail/return empty
    6. Empty-postings 200 responses stay as 'miss' (never 'hit') per
       Lever Research Pitfall 2 — same dynamic affects every Stage 4 platform

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict. Reads TESTING flag.

    Returns:
        Dict with probed, hits, misses, collision_reset counts.
    """
    # TESTING guard: skip real API calls during tests
    if config.get("TESTING"):
        logger.debug("probe_ats_slugs: TESTING mode — skipping API calls")
        return {"probed": 0, "hits": 0, "misses": 0, "collision_reset": 0}

    summary = {"probed": 0, "hits": 0, "misses": 0, "collision_reset": 0}
    svc = get_services()

    with svc.connection_factory() as conn:
        settings = _slug_probe_settings(config)
        if settings["collision_retry_enabled"]:
            reset_count = _reset_stale_collision_misses(
                conn, settings["collision_retry_cooldown_hours"]
            )
            if reset_count:
                logger.info(
                    "probe_ats_slugs: reset %d stale collision-miss company(ies) back to "
                    "pending (cooldown=%dh)",
                    reset_count,
                    settings["collision_retry_cooldown_hours"],
                )
            summary["collision_reset"] = reset_count

        # Only probe companies with pending status
        pending = conn.execute(
            "SELECT id, name, name_raw, careers_url FROM companies "
            "WHERE ats_probe_status = 'pending'"
        ).fetchall()

        for company in pending:
            company_id = company["id"]
            company_name = company["name_raw"]
            company_name_norm = company["name"] or ""
            careers_url = company["careers_url"]
            now = utc_now_iso()

            # B2 — careers_url hostname fast-path. If careers_url unambiguously
            # identifies an ATS (e.g. https://jobs.ashbyhq.com/{slug},
            # https://{slug}.recruitee.com), skip the speculative ladder and
            # write the hit with URL-evidence attribution. Runs BEFORE the
            # brand blocklist because URL evidence is strictly stronger than
            # name-collision concerns — a famous brand whose own careers page
            # points at an ATS we support is not a collision case.
            #
            # Evidence is recorded via the same ats_evidence_* columns used
            # by the reconcile path, so URL-evidence hits are distinguishable
            # from speculative hits and protected by the same B1 reset filter
            # (status='hit' AND evidence IS NULL → reset). Future audits can
            # tell the three provenance classes apart by ats_evidence_trigger:
            #   - 'careers_url:...'           → B2 fast-path (this branch)
            #   - 'scheduled_promote' (etc.)  → reconcile_company_ats path
            #   - NULL                        → legacy speculative-probe hit
            inferred = extract_ats_from_url_best(careers_url) if careers_url else None
            if inferred is not None:
                fp_platform, fp_slug, _ = inferred
                if fp_platform in _URL_FASTPATH_PLATFORMS and _verify_fastpath_live(
                    fp_platform, fp_slug
                ):
                    trigger = f"{TRIGGER_PREFIX_CAREERS_URL}{careers_url}"[:240]
                    # careers_url: is an owner-anchored trigger prefix (same
                    # family as promote_from_careers_link's careers_link:), so
                    # owner_identity_passes short-circuits to True regardless
                    # of name affinity — this fast-path is never provisional.
                    # Still routed through the shared scoring fn (not
                    # hardcoded 0) so a renamed/removed prefix constant can't
                    # silently diverge from every other write site's logic.
                    is_provisional = (
                        0
                        if (
                            svc.owner_identity_passes is not None
                            and svc.owner_identity_passes(
                                company_name_norm, company_name, trigger, fp_slug
                            )
                        )
                        else 1
                    )
                    fastpath_sql = """UPDATE companies
                               SET ats_platform = ?,
                                   ats_slug = ?,
                                   ats_probe_status = 'hit',
                                   ats_probe_attempted_at = ?,
                                   ats_evidence_trigger = ?,
                                   ats_evidence_extractor_version = ?,
                                   ats_evidence_unique_url_count = ?,
                                   ats_evidence_job_count = ?,
                                   ats_evidence_reconciled_at = ?,
                                   ats_evidence_provisional = ?,
                                   consecutive_empty_scans = 0,
                                   updated_at = ?
                               WHERE id = ?"""
                    fastpath_params = (
                        fp_platform,
                        fp_slug,
                        now,
                        trigger,
                        ATS_EXTRACTOR_VERSION,
                        1,
                        0,
                        now,
                        is_provisional,
                        now,
                        company_id,
                    )
                    try:
                        conn.execute(fastpath_sql, fastpath_params)
                        conn.commit()
                    except sqlite3.IntegrityError as exc:
                        # m076's UNIQUE(ats_platform, ats_slug) gate. Another
                        # company already owns (fp_platform, fp_slug) —
                        # re-verify ITS identity via the shared challenge
                        # mechanism rather than unconditionally leaving this
                        # company at miss forever (a poisoned owner sitting
                        # on this slug would otherwise block every future
                        # careers_url fast-path hit for the rightful company).
                        collision = _resolve_collision(
                            conn,
                            platform=fp_platform,
                            slug=fp_slug,
                            company_id=company_id,
                            config=config,
                        )
                        if collision["demoted"]:
                            try:
                                conn.execute(fastpath_sql, fastpath_params)
                            except sqlite3.IntegrityError:
                                # Unreachable by construction (this
                                # transaction just cleared the owner's claim)
                                # unless another writer raced in between;
                                # don't ride an unrelated commit on this
                                # uncommitted demotion.
                                conn.rollback()
                                collision["demoted"] = False
                            if collision["demoted"]:
                                conn.commit()
                                logger.info(
                                    "probe_ats_slugs: %s (id=%d) -> hit %s/%s via "
                                    "careers_url fast-path (demoted prior owner id=%s)",
                                    company_name,
                                    company_id,
                                    fp_platform,
                                    fp_slug,
                                    collision["existing_owner_id"],
                                )
                                summary["hits"] += 1
                                summary["probed"] += 1
                                continue
                        if collision["challenge"] and collision["challenge"]["recorded"]:
                            # Persist challenge bookkeeping even though promotion is refused.
                            conn.commit()
                        owner_id = collision["existing_owner_id"]
                        owner_name = collision["existing_owner_name"]
                        logger.warning(
                            "probe_ats_slugs: fast-path collision for %s "
                            "(id=%d) on %s/%s — already owned by id=%s (%r); "
                            "marking miss with reason='collision'. exc=%s",
                            company_name,
                            company_id,
                            fp_platform,
                            fp_slug,
                            owner_id,
                            owner_name,
                            exc,
                        )
                        conn.execute(
                            """UPDATE companies
                               SET ats_probe_status = 'miss',
                                   miss_reason = 'collision',
                                   ats_probe_attempted_at = ?,
                                   updated_at = ?
                               WHERE id = ?""",
                            (now, now, company_id),
                        )
                        conn.commit()
                        summary["misses"] += 1
                        summary["probed"] += 1
                        continue
                    logger.info(
                        "probe_ats_slugs: %s (id=%d) -> hit %s/%s via careers_url fast-path",
                        company_name,
                        company_id,
                        fp_platform,
                        fp_slug,
                    )
                    summary["hits"] += 1
                    summary["probed"] += 1
                    continue

            # F8 — brand blocklist gate. Famous-brand names (Shopify, Walmart,
            # Canva, ...) produce a high-rate collision with small companies
            # that have registered the same slug on a small ATS (BambooHR,
            # Recruitee, Pinpoint, ...). Empirically the tenants self-identify
            # with the same name, so name-matching can't disambiguate; only
            # a curated blocklist works for this cohort. See
            # job_finder/web/brand_blocklist.py for the rationale and seed list.
            if is_blocked_brand(company_name):
                logger.info(
                    "probe_ats_slugs: skipped %s (id=%d) — blocked brand",
                    company_name,
                    company_id,
                )
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           miss_reason = 'blocked_brand',
                           ats_probe_attempted_at = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (now, now, company_id),
                )
                conn.commit()
                summary["misses"] += 1
                summary["probed"] += 1
                continue

            candidates = derive_slug_candidates(company_name)
            hit_platform = None
            hit_slug = None
            # B4: track whether the speculative loop rejected ANY hit via
            # the consistency gate, so misses can be categorized into
            # `speculative_rejected` (had a hit but it was blocked) vs
            # `speculative_exhausted` (no probe even returned True).
            any_hit_consistency_rejected = False

            for slug in candidates:
                # Fire all platform probes for this slug concurrently
                platform_results = _probe_platforms_concurrently(slug)

                # Pick the winner by _PROBES order (deterministic precedence)
                for platform, hit in platform_results:
                    if not hit:
                        continue
                    if not probe_hit_consistent_or_dead_url(platform, careers_url):
                        any_hit_consistency_rejected = True
                        logger.info(
                            "probe_ats_slugs: rejected %s/%s for company %s — "
                            "careers_url %s infers a different platform and is live",
                            platform,
                            slug,
                            company_name,
                            careers_url,
                        )
                        continue
                    hit_platform = platform
                    hit_slug = slug
                    break
                if hit_platform:
                    break

            # Update company record based on probe result
            if hit_platform:
                # hit_platform/hit_slug are always assigned together above.
                assert hit_slug is not None
                # Every ats_evidence_* column is nulled explicitly (not just
                # omitted) so a company row that once held an evidence-based
                # promotion — then got its slug cleared by a path that
                # doesn't touch these columns (companies.py's update-slug
                # route, company_dedup's heal) — can't re-enter the
                # speculative ladder and keep a stale non-NULL
                # ats_evidence_trigger. The NULL-means-speculative invariant
                # (relied on by m064_reset_fp_prone_speculative_hits) must
                # hold regardless of the row's prior state.
                # Speculative ladder has no evidence trigger (always NULL) —
                # score falls through to name-vs-slug affinity, same as
                # ats_prober's sibling speculative writer. The candidate was
                # itself derived from this company's own name
                # (derive_slug_candidates), so this is non-provisional in the
                # common case; only a genuine name/slug divergence marks it.
                is_provisional = (
                    0
                    if (
                        svc.owner_identity_passes is not None
                        and svc.owner_identity_passes(
                            company_name_norm, company_name, None, hit_slug
                        )
                    )
                    else 1
                )
                speculative_sql = """UPDATE companies
                           SET ats_platform = ?,
                               ats_slug = ?,
                               ats_probe_status = 'hit',
                               ats_probe_attempted_at = ?,
                               ats_evidence_trigger = NULL,
                               ats_evidence_extractor_version = NULL,
                               ats_evidence_unique_url_count = NULL,
                               ats_evidence_job_count = NULL,
                               ats_evidence_reconciled_at = NULL,
                               ats_evidence_provisional = ?,
                               consecutive_empty_scans = 0,
                               updated_at = ?
                           WHERE id = ?"""
                speculative_params = (
                    hit_platform,
                    hit_slug,
                    now,
                    is_provisional,
                    now,
                    company_id,
                )
                try:
                    conn.execute(speculative_sql, speculative_params)
                    summary["hits"] += 1
                except sqlite3.IntegrityError as exc:
                    # m076's UNIQUE(ats_platform, ats_slug) gate. The
                    # speculative ladder produced a slug that's already owned
                    # by another company — re-verify ITS identity via the
                    # shared challenge mechanism (a single lower-confidence
                    # name-derived guess must not evict an incumbent outright,
                    # but a poisoned owner failing repeated re-verification
                    # against a name-affine challenger still gets demoted).
                    collision = _resolve_collision(
                        conn,
                        platform=hit_platform,
                        slug=hit_slug,
                        company_id=company_id,
                        config=config,
                    )
                    if collision["demoted"]:
                        try:
                            conn.execute(speculative_sql, speculative_params)
                        except sqlite3.IntegrityError:
                            # Unreachable by construction (this transaction
                            # just cleared the owner's claim) unless another
                            # writer raced in between; don't ride an
                            # unrelated commit on this uncommitted demotion.
                            conn.rollback()
                            collision["demoted"] = False
                        if collision["demoted"]:
                            summary["hits"] += 1
                            logger.info(
                                "probe_ats_slugs: %s (id=%d) -> hit %s/%s via speculative "
                                "ladder (demoted prior owner id=%s)",
                                company_name,
                                company_id,
                                hit_platform,
                                hit_slug,
                                collision["existing_owner_id"],
                            )
                    if not collision["demoted"]:
                        owner_id = collision["existing_owner_id"]
                        owner_name = collision["existing_owner_name"]
                        logger.warning(
                            "probe_ats_slugs: speculative collision for %s "
                            "(id=%d) on %s/%s — already owned by id=%s (%r); "
                            "marking miss with reason='collision'. exc=%s",
                            company_name,
                            company_id,
                            hit_platform,
                            hit_slug,
                            owner_id,
                            owner_name,
                            exc,
                        )
                        conn.execute(
                            """UPDATE companies
                               SET ats_probe_status = 'miss',
                                   miss_reason = 'collision',
                                   ats_probe_attempted_at = ?,
                                   updated_at = ?
                               WHERE id = ?""",
                            (now, now, company_id),
                        )
                        summary["misses"] += 1
            else:
                # B4: categorical miss_reason so the next audit can tell
                # speculative-exhausted misses apart from gate-rejected ones.
                # Legacy NULL miss_reason rows (pre-B4) are not retroactively
                # backfilled — they stay NULL until the company is re-probed.
                miss_reason = (
                    "speculative_rejected"
                    if any_hit_consistency_rejected
                    else "speculative_exhausted"
                )
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           miss_reason = ?,
                           ats_probe_attempted_at = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (miss_reason, now, now, company_id),
                )
                summary["misses"] += 1

            conn.commit()
            summary["probed"] += 1

            # Polite delay between companies (0.5s per Research Open Question 2)
            time.sleep(0.5)

    logger.info(
        "probe_ats_slugs: probed=%d, hits=%d, misses=%d, collision_reset=%d",
        summary["probed"],
        summary["hits"],
        summary["misses"],
        summary["collision_reset"],
    )
    return summary
