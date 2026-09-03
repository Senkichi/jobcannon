# PORTED from job_finder/web/ats_platforms/__init__.py @ abedc58ab57db586c74bf22c8a80cb40399deb8b (private job-cannon). Ledger L-0014.
"""ATS platform job scanning — public surface, registry, and shared utilities.

The 16 ``scan_*`` functions defined here delegate to the
``PlatformScanner`` registry in ``_registry``. Per-platform HTTP /
pagination / response parsing lives in ``_platforms_*`` submodules; the
shared title-normalization machinery lives in ``_title_match``; and the
Workday / SmartRecruiters per-job description fetchers live in
``_detail_fetchers``.

This ``__init__`` re-exports every legacy public name so callers that
import from ``jobcannon.engine.ats_platforms`` (the historical flat module)
keep working unchanged.

All HTTP calls use a shared ``requests.Session`` with connection pooling
(via ``_http_session.get_session()``) to avoid repeated TCP+TLS handshakes.
``get_session`` is imported into each consuming submodule's own namespace, so
tests must patch that submodule's local binding — e.g.
``jobcannon.engine.ats_platforms._registry.get_session`` or
``jobcannon.engine.ats_platforms._platforms_workday.get_session`` — not
``_http_session.get_session`` on the origin module, which has no effect once
a consumer already holds its own reference to the original function.

History: extracted from ``ats_scanner.py`` (Plan 02 split). The registry
split landed in polish-review F1 (2026-05-26). The package promotion —
flat ``ats_platforms.py`` + sibling ``ats_platforms_internal`` package
folded into a single ``ats_platforms/`` package — landed in H3
(2026-05-28).
"""

from jobcannon.engine.ats_platforms._detail_fetchers import (  # noqa: F401
    _fetch_smartrecruiters_description,
    _fetch_workday_description,
)
from jobcannon.engine.ats_platforms._platforms_adp import SCANNER as _ADP_SCANNER
from jobcannon.engine.ats_platforms._platforms_amazon import SCANNER as _AMAZON_SCANNER
from jobcannon.engine.ats_platforms._platforms_ashby import SCANNER as _ASHBY_SCANNER
from jobcannon.engine.ats_platforms._platforms_bamboohr import SCANNER as _BAMBOOHR_SCANNER
from jobcannon.engine.ats_platforms._platforms_breezy import SCANNER as _BREEZY_SCANNER
from jobcannon.engine.ats_platforms._platforms_eightfold import SCANNER as _EIGHTFOLD_SCANNER
from jobcannon.engine.ats_platforms._platforms_google import SCANNER as _GOOGLE_SCANNER
from jobcannon.engine.ats_platforms._platforms_greenhouse import SCANNER as _GREENHOUSE_SCANNER
from jobcannon.engine.ats_platforms._platforms_ibm import SCANNER as _IBM_SCANNER
from jobcannon.engine.ats_platforms._platforms_icims import SCANNER as _ICIMS_SCANNER
from jobcannon.engine.ats_platforms._platforms_icims import PlaywrightPlatformScanner
from jobcannon.engine.ats_platforms._platforms_jazzhr import SCANNER as _JAZZHR_SCANNER
from jobcannon.engine.ats_platforms._platforms_jobvite import SCANNER as _JOBVITE_SCANNER
from jobcannon.engine.ats_platforms._platforms_lever import SCANNER as _LEVER_SCANNER
from jobcannon.engine.ats_platforms._platforms_microsoft import SCANNER as _MICROSOFT_SCANNER
from jobcannon.engine.ats_platforms._platforms_oracle_cloud import SCANNER as _ORACLE_CLOUD_SCANNER
from jobcannon.engine.ats_platforms._platforms_paylocity import SCANNER as _PAYLOCITY_SCANNER
from jobcannon.engine.ats_platforms._platforms_personio import SCANNER as _PERSONIO_SCANNER
from jobcannon.engine.ats_platforms._platforms_phenom import SCANNER as _PHENOM_SCANNER
from jobcannon.engine.ats_platforms._platforms_pinpoint import SCANNER as _PINPOINT_SCANNER
from jobcannon.engine.ats_platforms._platforms_recruitee import SCANNER as _RECRUITEE_SCANNER
from jobcannon.engine.ats_platforms._platforms_rippling import SCANNER as _RIPPLING_SCANNER
from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
    SCANNER as _SMARTRECRUITERS_SCANNER,
)
from jobcannon.engine.ats_platforms._platforms_successfactors import (
    SCANNER as _SUCCESSFACTORS_SCANNER,
)
from jobcannon.engine.ats_platforms._platforms_teamtailor import SCANNER as _TEAMTAILOR_SCANNER
from jobcannon.engine.ats_platforms._platforms_tesla import SCANNER as _TESLA_SCANNER
from jobcannon.engine.ats_platforms._platforms_ultipro import SCANNER as _ULTIPRO_SCANNER
from jobcannon.engine.ats_platforms._platforms_workable import SCANNER as _WORKABLE_SCANNER
from jobcannon.engine.ats_platforms._platforms_workday import SCANNER as _WORKDAY_SCANNER
from jobcannon.engine.ats_platforms._registry import PlatformScanner, run_platform_scan
from jobcannon.engine.ats_platforms._title_match import (  # noqa: F401
    _PUNCT_RUN,
    _TITLE_EXPANSIONS,
    _WS_RUN,
    TITLE_MATCH_VERSION,
    _compile_word_boundary,
    _normalize_title,
    _title_matches,
    _token_set_match,
)

# NON_SCANNABLE_PLATFORMS now lives in jobcannon.engine.ats_registry, derived from
# each PlatformSpec's `non_scannable` flag (single source of truth). It used to be
# a hand-maintained frozenset here; importers now take it from the registry.

SCANNERS_BY_NAME: dict[str, PlatformScanner] = {
    s.name: s
    for s in (
        _ADP_SCANNER,
        _AMAZON_SCANNER,
        _ASHBY_SCANNER,
        _BAMBOOHR_SCANNER,
        _BREEZY_SCANNER,
        _EIGHTFOLD_SCANNER,
        _GOOGLE_SCANNER,
        _GREENHOUSE_SCANNER,
        _IBM_SCANNER,
        _JAZZHR_SCANNER,
        _JOBVITE_SCANNER,
        _LEVER_SCANNER,
        _MICROSOFT_SCANNER,
        _ORACLE_CLOUD_SCANNER,
        _PAYLOCITY_SCANNER,
        _PERSONIO_SCANNER,
        _PINPOINT_SCANNER,
        _RECRUITEE_SCANNER,
        _RIPPLING_SCANNER,
        _SMARTRECRUITERS_SCANNER,
        _SUCCESSFACTORS_SCANNER,
        _TEAMTAILOR_SCANNER,
        _ULTIPRO_SCANNER,
        _WORKABLE_SCANNER,
        _WORKDAY_SCANNER,
    )
}

# Playwright scanners are registered separately in ats_registry.py
PLAYWRIGHT_SCANNERS: dict[str, PlaywrightPlatformScanner] = {
    "icims": _ICIMS_SCANNER,
    "phenom": _PHENOM_SCANNER,
    "tesla": _TESLA_SCANNER,
}


# ---------------------------------------------------------------------------
# Platform scanners — thin delegations to the PlatformScanner registry.
#
# The 16 ``scan_*`` functions below are explicit one-liners (rather than a
# factory) so the public signatures, docstrings, and parameter names
# travel with each platform's contract — the registry handles the title
# gate + result-count log line.
# ---------------------------------------------------------------------------


def scan_lever(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Lever API for keyword-matched job postings.

    API: GET https://api.lever.co/v0/postings/{slug}?mode=json

    Returns:
        List of job dicts with keys: title, company_source, location,
        description, source_url, salary_min, salary_max, comp_json.
        Empty list on error or no matches.
    """
    jobs, _ = run_platform_scan(_LEVER_SCANNER, slug, target_titles, exclusions)
    return jobs
    return jobs


def scan_greenhouse(
    board_token: str, target_titles: list[str], exclusions: list[str]
) -> list[dict]:
    """Scan Greenhouse API for keyword-matched job postings.

    Fetches all active jobs with content and pay transparency data.
    CRITICAL: pay_input_ranges values are in cents — divide by 100 for dollars
    (Research Pitfall 7).

    API: GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true&pay_transparency=true
    """
    jobs, _ = run_platform_scan(_GREENHOUSE_SCANNER, board_token, target_titles, exclusions)
    return jobs
    return jobs


def scan_ashby(job_board_name: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Ashby API for keyword-matched job postings.

    Preserves exact slug casing — Ashby slugs are case-sensitive
    (Research Pitfall 3: jobs.ashbyhq.com/OpenAI != jobs.ashbyhq.com/openai).
    Single retry on transient timeout (Ashby intermittency, see registry).

    API: GET https://api.ashbyhq.com/posting-api/job-board/{job_board_name}?includeCompensation=true
    """
    jobs, _ = run_platform_scan(_ASHBY_SCANNER, job_board_name, target_titles, exclusions)
    return jobs
    return jobs


def scan_workday(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Workday CXS API for keyword-matched job postings.

    Workday exposes a standardized POST JSON API across all tenants.
    Slug format: "{subdomain}/{board}" (e.g. "walmart.wd5/WalmartExternal").
    Per-job description is fetched via ``_fetch_workday_description``.
    """
    jobs, _ = run_platform_scan(_WORKDAY_SCANNER, slug, target_titles, exclusions)
    return jobs
    return jobs


def scan_smartrecruiters(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan SmartRecruiters Posting API for keyword-matched job postings.

    SmartRecruiters exposes a public REST API (no auth required) that returns
    JSON job listings with offset-based pagination.

    API: GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?offset={N}&limit=100
    Per-job description is fetched via ``_fetch_smartrecruiters_description``.
    """
    jobs, _ = run_platform_scan(_SMARTRECRUITERS_SCANNER, slug, target_titles, exclusions)
    return jobs
    return jobs


def scan_recruitee(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Recruitee public offers API for keyword-matched job postings.

    API: GET https://{slug}.recruitee.com/api/offers/ → {"offers": [...]}.
    No auth required. The list response includes title, locations, description,
    and careers_url; no detail-page fetch is needed for jd_full.
    """
    jobs, _ = run_platform_scan(_RECRUITEE_SCANNER, slug, target_titles, exclusions)
    return jobs
    return jobs


def scan_breezy(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Breezy HR public JSON feed for keyword-matched job postings.

    API: GET https://{slug}.breezy.hr/json → flat list of position objects.
    No auth required. The list endpoint omits the full description (only a
    short summary is exposed); jd_full enrichment happens later via the
    description URL.
    """
    jobs, _ = run_platform_scan(_BREEZY_SCANNER, slug, target_titles, exclusions)
    return jobs
    return jobs


def scan_jazzhr(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan JazzHR public feed for keyword-matched job postings.

    API: GET https://{slug}.applytojob.com/apply/jobs/feed?json=1
        → {"jobs": [...]} OR a bare list, depending on tenant config.
    """
    jobs, _ = run_platform_scan(_JAZZHR_SCANNER, slug, target_titles, exclusions)
    return jobs
    return jobs


def scan_pinpoint(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Pinpoint public postings JSON for keyword-matched job postings.

    API: GET https://{slug}.pinpointhq.com/postings.json → {"data": [...]}.
    No auth. Single-shot — Pinpoint returns every active posting in one
    response with no pagination.
    """
    jobs, _ = run_platform_scan(_PINPOINT_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_personio(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Personio public XML feed for keyword-matched job postings.

    API: GET https://{slug}.jobs.personio.{de,com}/xml → workzag-jobs
    document with <position> children. Tries .de first, falls back to .com
    on 404 (some tenants migrated TLDs).
    """
    jobs, _ = run_platform_scan(_PERSONIO_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_bamboohr(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan BambooHR public careers widget HTML for keyword-matched postings.

    API: GET https://{slug}.bamboohr.com/jobs/embed2.php → HTML widget.
    The historical /careers/list JSON endpoint was deprecated in 2024 — the
    widget HTML is now the only public source. Listing does NOT include job
    descriptions (deferred to enrichment).
    """
    jobs, _ = run_platform_scan(_BAMBOOHR_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_teamtailor(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Teamtailor public jobs API for keyword-matched postings.

    API: GET https://{slug}.teamtailor.com/api/jobs → JSON:API document
    {"data": [{"attributes": {...}, "links": {...}}, ...]}. Per-tenant
    public unkeyed feed (not the keyed api.teamtailor.com path).
    """
    jobs, _ = run_platform_scan(_TEAMTAILOR_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_workable(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Workable public widget endpoint for keyword-matched postings.

    API: GET https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true
    → {"name": ..., "jobs": [...]}. Empty-jobs path is a clean miss.
    """
    jobs, _ = run_platform_scan(_WORKABLE_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_jobvite(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Jobvite — stub. Always returns [].

    Jobvite hosted career pages have no public unauthenticated JSON API
    and frequently redirect to tenant-custom domains. A real scraper
    requires per-tenant HTML parsing; deferred. The stub exists so the
    platform is registered for URL-evidence promotion via B2 fast-path.
    See _platforms_jobvite.py for the full rationale.
    """
    jobs, _ = run_platform_scan(_JOBVITE_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_paylocity(guid: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Paylocity public v2 job feed for keyword-matched postings.

    API: GET https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{guid}
    → {"organization": ..., "jobs": [...]}. Each job includes summary,
    requirements, benefits as separate sections (stitched into description).
    The "slug" is the tenant GUID extracted from the careers URL path.
    """
    jobs, _ = run_platform_scan(_PAYLOCITY_SCANNER, guid, target_titles, exclusions)
    return jobs


def scan_rippling(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Rippling public board API for keyword-matched postings.

    API: GET https://ats.rippling.com/api/v2/board/{slug}/jobs → paginated
    {"items": [...], "page": ..., "totalPages": ...}. Description is NOT
    in the list endpoint; enrichment fills jd_full asynchronously.
    """
    jobs, _ = run_platform_scan(_RIPPLING_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_microsoft(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Microsoft Careers (Phenom pcsx) for keyword-matched postings.

    API: GET https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com&start={N}
    Offset pagination, page size fixed at 10. Slug is the ``domain`` param
    (defaults to "microsoft.com"). Description is NOT in the list endpoint;
    enrichment fills jd_full from the position_details endpoint.
    """
    jobs, _ = run_platform_scan(_MICROSOFT_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_eightfold(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan an Eightfold (SmartApply) tenant for keyword-matched postings.

    API: GET https://{host}/api/apply/v2/jobs?domain={domain}&start={N}
    Slug encodes "host|domain" (e.g. "explore.jobs.netflix.net|netflix.com").
    Offset pagination, page size fixed at 10. One adapter serves every
    Eightfold tenant; description is filled by enrichment.
    """
    jobs, _ = run_platform_scan(_EIGHTFOLD_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_ultipro(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan a UKG Pro Recruiting (UltiPro) job board for matched postings.

    API: POST https://{host}/{tenant}/JobBoard/{board}/JobBoardView/LoadSearchResults
    Slug packs "{host}/{tenant}/{board}" (e.g.
    "recruiting2.ultipro.com/JAN1000JANI/693b35f4-..."). Top/Skip pagination up
    to totalCount. Description is the short blurb; jd_full is filled by enrichment.
    """
    jobs, _ = run_platform_scan(_ULTIPRO_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_oracle_cloud(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan an Oracle Recruiting Cloud (Fusion CE) site for matched postings.

    API: GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?finder=findReqs;siteNumber={site},limit=50,offset={N},sortBy=POSTING_DATES_DESC
    Slug packs "{host}|{site}" (e.g. "ibtcjb.fa.ocs.oraclecloud.com|CX_1"); a
    missing site defaults to CX_1. Offset pagination up to TotalJobsCount.
    Description is the short blurb; jd_full is filled by enrichment.
    """
    jobs, _ = run_platform_scan(_ORACLE_CLOUD_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_successfactors(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan a SuccessFactors job board for matched postings.

    API: GET https://{host}/career?company={company_id}&career_ns=job_listing_summary&resultType=XML
    Slug packs "{host}|{company_id}" (e.g. "career2.successfactors.eu|SwissRe").
    Single-shot XML feed (no pagination). Facets are tenant-variable — resolved
    by label text, not element index. Description is raw HTML (kept for jd_full).
    """
    jobs, _ = run_platform_scan(_SUCCESSFACTORS_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_ibm(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan IBM careers API for matched postings.

    API: POST https://www-api.ibm.com/search/api/v2
    Slug is a constant ("ibm") — IBM is single-tenant. Offset pagination up to
    _MAX_RESULTS. Description is the short blurb; jd_full is filled by enrichment.
    """
    jobs, _ = run_platform_scan(_IBM_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_adp(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan ADP Workforce Now for matched postings.

    API: GET https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions
        ?cid={slug}&ccId=19000101_000001&lang=en_US&locale=en_US&$top=100&$skip={N}
    Slug is the client ID UUID (e.g. "a6717ebc-f6a8-4a51-856b-f7ebd573645e").
    OData-style pagination with $top/$skip. Description is NOT in the list endpoint;
    jd_full is filled by enrichment.
    """
    jobs, _ = run_platform_scan(_ADP_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_phenom(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Phenom careers site for matched postings.

    Phenom does not expose a public JSON API. Job URLs are discovered via
    sitemaps (sitemap_index.xml → sitemapN.xml → job URLs). Individual job
    detail pages are fetched via Playwright to extract full job data.
    Slug is the careers host (e.g. "careers.conduent.com").

    Note: This is a Playwright-class scanner. The public function exists for
    live_sanity.json validation, but production scans use the Playwright
    registry path in ats_scanner/_run_playwright.py.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            postings = _PHENOM_SCANNER.fetch_postings(browser, slug)
            # Apply title gate manually for the public function
            from jobcannon.engine.ats_platforms import _title_matches

            results = []
            for posting in postings:
                title = _PHENOM_SCANNER.title_of(posting)
                if _title_matches(title, target_titles, exclusions):
                    job_dict = _PHENOM_SCANNER.posting_to_job(posting, slug)
                    if job_dict is not None:
                        results.append(job_dict)
            return results
        finally:
            browser.close()


def scan_amazon(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Amazon Jobs (single global board) for keyword-matched postings.

    API: GET https://www.amazon.jobs/en/search.json?base_query={slug}&sort=recent
    Slug is the ``base_query`` keyword (empty → most-recent across all of
    Amazon). Recent-sorted and capped; the driver's title gate filters to
    target roles. Description is included inline on the list endpoint.
    """
    jobs, _ = run_platform_scan(_AMAZON_SCANNER, slug, target_titles, exclusions)
    return jobs


def scan_google(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Google Careers — stub. Always returns [].

    Google has no public unauthenticated JSON board (the live careers path is
    a JS-only ``batchexecute`` SPA). Registered as a non-scannable platform so
    a company can be classified ``ats_platform='google'`` and badged "no public
    API" instead of "0 jobs". See _platforms_google.py.
    """
    jobs, _ = run_platform_scan(_GOOGLE_SCANNER, slug, target_titles, exclusions)
    return jobs


__all__ = [
    "SCANNERS_BY_NAME",
    "PlatformScanner",
    "run_platform_scan",
    "scan_adp",
    "scan_amazon",
    "scan_ashby",
    "scan_bamboohr",
    "scan_breezy",
    "scan_eightfold",
    "scan_google",
    "scan_greenhouse",
    "scan_ibm",
    "scan_jazzhr",
    "scan_jobvite",
    "scan_lever",
    "scan_microsoft",
    "scan_oracle_cloud",
    "scan_paylocity",
    "scan_personio",
    "scan_phenom",
    "scan_pinpoint",
    "scan_recruitee",
    "scan_rippling",
    "scan_smartrecruiters",
    "scan_successfactors",
    "scan_teamtailor",
    "scan_ultipro",
    "scan_workable",
    "scan_workday",
]
