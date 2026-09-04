# PORTED from job_finder/web/careers_crawler/_cohort_legitimacy.py @ 1af9001b887b04aca0701614a0a208ee4aa02238 (private job-cannon). Ledger L-0464.
"""Cohort-legitimacy gate for the sitemap crawler tier.

WHY THIS EXISTS
----------------
On 2026-07-11, role.com — an AI job-search aggregator, not a single employer
— was crawled by the sitemap tier (`_sitemap_tier._try_sitemap_extract`) as
if it were one company's own careers page. The tier has no concept of "is
this actually one company's postings": it harvests every `/jobs/...` URL a
sitemap exposes, derives a title from the URL slug, and bulk-imports the
lot. 437 postings belonging to hundreds of unrelated real employers (e.g. a
CoStar Group "Lead Product Analyst, LoopNet" posting) were attributed to a
phantom company row, "Role, Inc." That incident was fixed reactively — a
one-name denylist entry (#1144) — which is the *correct* response for a
single confirmed offender (mirrors #213's aggregator-denylist precedent;
see `job_finder.config._RAW_COMPANY_DENYLIST` and `m103`/`m205994845`) but
does nothing for the next aggregator domain the crawler stumbles into.

This module is that proactive check. It runs at TWO call sites:
  1. Inside the sitemap tier (`_sitemap_tier._try_sitemap_extract`) — the
     tier with zero per-posting content validation, which was the original
     role.com vector. The sitemap tier short-circuits the remaining
     escalation chain via its `flag_sink` handoff.
  2. At the orchestrator level (`careers_crawler._crawl_worker`) — on the
     assembled per-company cohort AFTER any other tier (api_cached, static,
     url_param, embedded_json, playwright, ai_nav) returns postings and
     BEFORE import. This closes the 2026-08-23 Next Frontier Capital gap
     (#1921): a multi-employer aggregator board minted JPMorganChase
     requisitions under an unrelated company row through the playwright/
     ai_nav tiers, which parse per-posting HTML (well-formed postings) but
     never check whether the postings belong to the crawled employer. The
     "comparatively self-limiting" premise that originally scoped the gate
     to the sitemap tier alone does not hold for an aggregator whose per-
     posting pages are themselves well-formed — the postings are real, they
     just belong to a different employer.

A free, in-memory title-template clustering signal runs on every
cohort regardless of size. The bounded sample-fetch network cross-check runs
only when the cohort is at or above `legitimacy_large_cohort_threshold`
(default 10 — just enough to make the capped `legitimacy_sample_size` (8)
spread meaningful; the sample cost is capped, so the threshold guards sample
quality, not network spend).

DESIGN: COHORT-TARGETING, NOT A BLANKET SWEEP
-----------------------------------------------
This project's standing policy (see `brand_blocklist.py`'s module
docstring, and the ATS slug-challenge/demotion precedent in
`ats_slug_challenge.py`) is that heuristics prone to false positives must
never silently reject on a single weak signal — they must require
corroborating, positive evidence, and when in doubt must FLAG for human
review rather than act unilaterally. This gate follows the same shape:

- It never flags on the cohort SIZE alone (a large real employer — Amazon,
  a big bank — can legitimately expose hundreds of postings). Size only
  decides whether the bounded sample-fetch is worth running; the free title
  signal already covers smaller cohorts.
- It never flags on a SINGLE sampled posting's mismatch (`_extract_jsonld_
  postings` finding `hiringOrganization` absent, or a page failing to
  fetch, is treated as "no data," never as "evidence of an aggregator" —
  many legitimate careers pages simply don't emit `hiringOrganization` in
  their JSON-LD). It requires at least `_MIN_POSITIVE_SAMPLES` (2)
  INDEPENDENT samples carrying positive off-brand evidence — either a
  different employer's name, or aggregator sidebar chrome bled into a
  structured field — before flagging. One bad sample could be a data
  glitch; two independent ones on a bounded, spread-out sample is a
  pattern.
- On a positive trip it FLAGS (`companies.careers_crawl_flag_reason`) and
  withholds the cohort from import — it does not silently drop the
  company, blocklist it, or delete anything. A human reviews the flagged
  row and either denylists the domain (the role.com playbook) or clears
  the flag if it was a false positive.

SIGNALS
-------
Three signals. The first is free and runs on every cohort; the other two
are cheap-per-sample checks against a spread sample of the cohort's job-detail
URLs (not just the head — an aggregator can group same-employer postings
contiguously in sitemap order, e.g. per upstream feed, so sampling only the
first K URLs risks drawing all K from one real employer inside an otherwise-
mixed cohort):

1. Templated-title clustering (free, in-memory): normalize each title
   (lowercase, remove punctuation, keep digits), cluster by token-level
   longest-common-subsequence similarity, and flag when a large fraction of
   titles collapse onto a small number of base templates. A low distinct-title
   ratio plus at least `_MIN_POSITIVE_SAMPLES` postings in the dominant base
   template is the positive signal. This catches aggregators that embed an
   unrelated employer name as a constant prefix/suffix regardless of the
   exact phrasing convention, without hand-rolling a "<Role> at <Company>"
   regex.

2. Hiring-organization variance: fetch each sampled URL, parse its
   schema.org `JobPosting` JSON-LD (reusing `_static_tier`'s existing
   walker — the same extraction the static tier already trusts), and
   token-compare `hiringOrganization.name` against the crawled company's
   `name_raw` via `ats_slug_challenge.name_slug_affinity` (already the
   project's name-vs-identity comparator, reused rather than
   reimplemented). The positive signal is >= `_MIN_POSITIVE_SAMPLES`
   independent sampled postings resolving to a `hiringOrganization`
   name that is NOT affine to the crawled company. This covers BOTH
   the multi-distinct-employer case (>= 2 distinct off-brand names —
   direct evidence of multiple real employers inside one "company"'s
   cohort) AND the single-consistent-off-brand case (one wrong org
   name, e.g. "Aumni" vs "Next Frontier Capital", repeated across >= 2
   independent samples — the 2026-08-24 #1930 blind spot (b): the
   original #1144 gate required >= 2 DISTINCT names, so a cohort
   consistently labeled with ONE wrong employer never fired; the
   independent-sample count is the correct independence proxy, not the
   distinct-name count).

3. Location-field chrome bleed: the tell that actually surfaced the
   role.com incident during triage — aggregator sidebar/rail text ("Popular
   Jobs", "View all jobs", ...) bleeding into a structured `jobLocation`
   field because the aggregator's own page furniture sits next to the
   JSON-LD island. Reuses `_static_tier._location_from_jsonld` for the
   extraction; matches a small, high-precision substring list (same
   philosophy as `_title_contract.py`'s hand-curated CTA-phrase set — kept
   deliberately narrow so it stays high-precision).

4. Portfolio-board URL taxonomy (free, in-memory): detect a
   `/companies/<slug>/` path segment shared across the cohort's job
   URLs — the path convention used by white-labeled multi-employer
   portfolio boards (Getro-powered VC portfolio boards et al.), where
   `<slug>` is one of many portfolio companies, not the crawled
   employer. Closes the 2026-08-24 Next Frontier Capital gap (#1930):
   85/86 jobs came from `/companies/aumni/` (Aumni was acquired by
   JPMorganChase, so every posting was a JPMC req laundered through a
   VC firm's Getro board). The slug must NOT be name-affine to the
   crawled company (a real company whose own careers site nests under
   `/companies/<its-own-slug>/` is exempt), and >= `_MIN_POSITIVE_SAMPLES`
   postings must share the dominant slug — high-precision by design,
   matching the project's "corroborating evidence, flag not reject"
   posture.

Each signal independently requires >= `_MIN_POSITIVE_SAMPLES` positive
samples before flagging; any one is sufficient on its own.

# PORT-SEAM: `name_slug_affinity` (+ `_tokens`/`_compressed`/`_GENERIC_TOKENS`/
# `_NOISE_TOKEN_RE`) is ported inline below as engine code, not a seam --
# verified pure per the wave-3 crawler-cascade design note.
# `record_legitimacy_flag`'s DB write uses the existing (required)
# `svc.connection_factory()` seam, same as careers_crawler/_persistence.py
# (L-0465) -- no new ScanServices field needed.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from jobcannon.engine._http_constants import _HEADERS, _TIMEOUT

# PORT-SEAM: name_slug_affinity ported inline below, not a seam (L-0464)
from jobcannon.engine.careers_crawler._static_tier import (
    _extract_jsonld_postings,
    _location_from_jsonld,
)

# PORT-SEAM: standalone_connection -> svc.connection_factory() below (L-0464)
from jobcannon.engine.http_fetch import fetch_with_deadline
from jobcannon.engine.identity_evidence import _hiring_org_name
from jobcannon.engine.services import get_services  # PORT-SEAM: seam import (L-0464)

logger = logging.getLogger(__name__)

# PORT-SEAM: web/ats_slug_challenge.py's own module-level constants and
# helpers, ported inline as engine code (not a seam) -- name_slug_affinity
# is verified pure per the wave-3 crawler-cascade design note (L-0464).
#
# Web/ATS-infrastructure words that appear in board slugs and site paths with no
# company-identity signal ("careers.homedepot.com", "External_Career_Site").
# Linguistic constants, not app state — shared by name and slug tokenization so
# a generic word can never create affinity on its own.
_GENERIC_TOKENS = frozenset(
    {
        "and",
        "apply",
        "career",
        "careers",
        "com",
        "company",
        "corp",
        "external",
        "global",
        "group",
        "holdings",
        "inc",
        "internal",
        "job",
        "jobs",
        "llc",
        "ltd",
        "net",
        "org",
        "portal",
        "recruiting",
        "search",
        "site",
        "the",
        "www",
    }
)

# Workday shard tokens ("wd1", "wd503") and pure numbers carry no identity.
_NOISE_TOKEN_RE = re.compile(r"^(wd\d+|\d+)$")


def _tokens(value: str) -> set[str]:
    """Identity-bearing tokens: alnum runs, >= 3 chars, minus generic/noise."""
    out = set()
    for tok in re.findall(r"[a-z0-9]+", (value or "").lower()):
        if len(tok) < 3 or tok in _GENERIC_TOKENS or _NOISE_TOKEN_RE.match(tok):
            continue
        out.add(tok)
    return out


def _compressed(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def name_slug_affinity(name: str, slug: str) -> bool:
    """True when ``name`` plausibly identifies the company behind ``slug``.

    Three signals, any of which passes: shared identity token ("Cigna Health"
    vs "cigna.wd5/cignacareers"), token substring containment >= 4 chars
    ("Home Depot" vs "careers.homedepot.com"), or whole-string compressed
    containment ("Gong.IO" vs "gongio"). Opaque slugs (ADP UUIDs, Oracle Cloud
    tenant hashes) match nothing — by design, so they can neither be
    challenged into nor demoted out of.
    """
    name_toks = _tokens(name)
    slug_toks = _tokens(slug)
    if not name_toks or not slug_toks:
        return False
    if name_toks & slug_toks:
        return True
    for a in name_toks:
        for b in slug_toks:
            if (len(a) >= 4 and a in b) or (len(b) >= 4 and b in a):
                return True
    cn, cs = _compressed(name), _compressed(slug)
    return len(cn) >= 4 and len(cs) >= 4 and (cn in cs or cs in cn)


# Sitemap cohorts at or above this size trigger the sample-fetch cross-check.
# The sample size is bounded/capped, so the cost does not scale with cohort
# size; this threshold only ensures a meaningful spread sample is available
# (it is set slightly above the sample size so tiny cohorts are skipped).
_DEFAULT_LARGE_COHORT_THRESHOLD = 10

# Job-detail pages sampled from the cohort when the size trip-wire fires.
_DEFAULT_SAMPLE_SIZE = 8

# Near-duplicate titles are considered clustered when their token-level
# longest-common-subsequence ratio meets this threshold. 0.7 keeps role
# permutations and adjective insertions together while separating genuinely
# distinct short titles that only share one word (e.g. "Engineer 0" vs
# "Engineer 99" share only "engineer" and score 0.5).
_TITLE_CLUSTER_SIMILARITY = 0.7

# Minimum distinct-title ratio a cohort must stay strictly above to avoid
# being treated as templated. A ratio at or below this value is treated as
# templated. A low ratio means most titles collapse onto a small number of
# base templates. Tunable via config.
_DEFAULT_MIN_DISTINCT_TITLE_RATIO = 0.4

# Independent positive-evidence samples required before flagging. Kept at 2
# deliberately: a single sample (fetch glitch, one mis-tagged posting) must
# never be enough to flag a legitimate company — see module docstring.
_MIN_POSITIVE_SAMPLES = 2

# Sidebar / nav chrome substrings that indicate a location field was
# contaminated with page furniture rather than an actual place name.
# Lowercase; matched as a substring against a lowercased location string.
# Kept small and high-precision on purpose (mirrors _title_contract.py's
# hand-curated CTA-phrase set) — a real location never contains these.
_LOCATION_CHROME_SUBSTRINGS: tuple[str, ...] = (
    "popular job",
    "trending job",
    "view all job",
    "browse job",
    "related job",
    "similar job",
    "recommended job",
    "more job",
    "apply now",
    "see more",
)

# Path taxonomy tell for white-labeled multi-employer portfolio boards
# (Getro-powered VC portfolio boards et al.). A real single-employer
# careers site rarely nests its own postings under /companies/<slug>/;
# that path shape is a portfolio/aggregator-board convention where
# <slug> is one of many portfolio companies. The slug is captured up to
# the next path separator.
_COMPANIES_SLUG_PATH_RE = re.compile(r"/companies/([^/]+)/", re.IGNORECASE)


@dataclass(frozen=True)
class CohortVerdict:
    """Result of evaluating a sitemap-discovered job cohort.

    `flagged=False` covers both "gate didn't trip" and "gate didn't run"
    (cohort too small, gate disabled, no company name) — callers only need
    the boolean to decide whether to withhold import.
    """

    flagged: bool
    reason: str | None
    sampled: int
    positive_signals: int


def _location_is_chrome(location: str) -> bool:
    loc = (location or "").lower()
    return any(s in loc for s in _LOCATION_CHROME_SUBSTRINGS)


def _companies_slug_signal(candidate_jobs: list[dict], company_name: str) -> tuple[str, int, int]:
    """Detect the ``/companies/<slug>/`` portfolio-board path taxonomy.

    Returns ``(dominant_slug, dominant_count, total_matched)``. Free and
    in-memory — no network fetch. ``dominant_slug`` is the slug shared by
    the most postings; ``dominant_count`` is that count; ``total_matched``
    is how many postings carry any ``/companies/<slug>/`` path.

    A signal is positive (``dominant_slug`` non-empty) only when the
    dominant slug is NOT name-affine to the crawled company — a real
    company whose own careers site nests under
    ``/companies/<its-own-slug>/`` is exempt (the slug matches the
    company). When the dominant slug IS affine, returns
    ``("", 0, total_matched)`` so callers can still observe the path
    shape without treating it as an aggregator signal.
    """
    slug_counts: dict[str, int] = {}
    for j in candidate_jobs:
        url = j.get("url") or ""
        m = _COMPANIES_SLUG_PATH_RE.search(url)
        if not m:
            continue
        slug = m.group(1).lower()
        slug_counts[slug] = slug_counts.get(slug, 0) + 1
    if not slug_counts:
        return "", 0, 0
    dominant_slug, dominant_count = max(slug_counts.items(), key=lambda kv: kv[1])
    total = sum(slug_counts.values())
    if name_slug_affinity(company_name, dominant_slug):
        return "", 0, total
    return dominant_slug, dominant_count, total


def _sample_urls(urls: list[str], sample_size: int) -> list[str]:
    """Spread a bounded sample across the full cohort, not just the head.

    An aggregator can group one real employer's postings contiguously in
    sitemap order (e.g. bulk-imported per upstream feed) — sampling only
    the first K URLs risks drawing all K from a single employer inside an
    otherwise-mixed cohort and missing the variance entirely.
    """
    n = len(urls)
    if n <= sample_size or sample_size <= 0:
        return list(urls)
    step = n / sample_size
    return [urls[int(i * step)] for i in range(sample_size)]


def _normalize_title(title: str | None) -> list[str]:
    """Normalize a job title for near-duplicate clustering.

    * lowercases
    * removes punctuation (so slash-numbered IDs like "1/4/5/6/7/8" become
      a single non-matching numeric token)
    * keeps digits in place (a real short title + numeric ID such as
      "Engineer 123" must not collapse with "Engineer 456" just because the
      IDs differ; the LCS similarity threshold handles templated long suffixes
      without conflating short role+ID postings)
    * collapses whitespace
    """
    if not title:
        return []
    text = re.sub(r"[^a-z0-9\s]", " ", str(title).lower())
    tokens = [t for t in text.split() if t]
    return tokens


def _token_lcs_similarity(a: list[str], b: list[str]) -> float:
    """Token-level longest-common-subsequence similarity, range [0, 1]."""
    if not a or not b:
        return 0.0
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        curr = [0] * (n + 1)
        ai = a[i - 1]
        for j in range(1, n + 1):
            if ai == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(curr[j - 1], prev[j])
        prev = curr
    lcs = prev[n]
    denom = max(m, n)
    return lcs / denom if denom else 0.0


def _cluster_titles(
    titles: list[str], similarity: float = _TITLE_CLUSTER_SIMILARITY
) -> list[list[str]]:
    """Order-independent token-LCS clustering of normalized titles.

    Titles are unioned into connected components when any pair meets the
    similarity threshold, so the result does not depend on the order the
    titles are presented. Titles that normalize to nothing (empty or all
    punctuation) are skipped entirely and do not create orphan clusters.
    """
    normalized = [(t, _normalize_title(t)) for t in titles]
    n = len(normalized)
    parent = list(range(n))
    rank = [0] * n

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra = find(a)
        rb = find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    for i in range(n):
        if not normalized[i][1]:
            continue
        for j in range(i + 1, n):
            if not normalized[j][1]:
                continue
            if _token_lcs_similarity(normalized[i][1], normalized[j][1]) >= similarity:
                union(i, j)

    root_to_cluster: dict[int, list[str]] = {}
    for i in range(n):
        if not normalized[i][1]:
            continue
        root = find(i)
        root_to_cluster.setdefault(root, []).append(normalized[i][0])
    return list(root_to_cluster.values())


def _title_template_ratio(candidate_jobs: list[dict]) -> tuple[float, int, int]:
    """Return (distinct_title_ratio, largest_cluster_size, total_titles).

    The distinct-title ratio is the number of title clusters divided by the
    number of non-empty normalized titles. A low ratio means many postings
    share the same base template, which is a free, in-memory signal for
    aggregator-style templated listings.
    """
    titles = [j.get("title") for j in candidate_jobs if j.get("title")]
    clusters = _cluster_titles(titles)
    total = sum(len(c) for c in clusters)
    if total == 0:
        return 1.0, 0, 0
    distinct_ratio = len(clusters) / total
    largest = max((len(c) for c in clusters), default=0)
    return distinct_ratio, largest, total


def _fetch_posting_signal(url: str) -> dict | None:
    """Fetch one job-detail page and return its JSON-LD JobPosting dict.

    Returns None on any fetch/parse failure or when no JobPosting JSON-LD
    is present — fail-open, matching the tier's existing HTTP-error
    posture (`_sitemap_tier._fetch_xml` swallows the same class of errors).
    """
    try:
        resp = fetch_with_deadline(url, getter=requests.get, timeout=_TIMEOUT, headers=_HEADERS)
        if resp.status_code != 200 or not resp.text:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.debug("cohort_legitimacy: sample fetch failed for %s: %s", url, e)
        return None

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        postings = _extract_jsonld_postings(data)
        if postings:
            return postings[0]
    return None


def evaluate_cohort_legitimacy(
    company_name: str,
    candidate_jobs: list[dict],
    config: dict | None,
) -> CohortVerdict:
    """Decide whether a crawler-discovered job cohort belongs to one employer.

    Called from two sites (see module docstring): the sitemap tier's own
    internal gate, and the orchestrator-level gate that covers every other
    tier that can import a cohort. Cheap size check first (free); only
    escalates to the bounded sample fetch when the cohort is large enough
    to warrant it. See module docstring for the full design rationale.

    Args:
        company_name: The crawled company's `name_raw`.
        candidate_jobs: The tier's filtered `{"title", "url", ...}` dicts,
            before import.
        config: App config dict (reads `careers_crawl.legitimacy_*` keys).

    Returns:
        A CohortVerdict. `flagged=True` means the caller should withhold
        this cohort from import and persist `reason` for human review.
    """
    gate_cfg = (config or {}).get("careers_crawl", {})
    if not gate_cfg.get("legitimacy_gate_enabled", True):
        return CohortVerdict(False, None, 0, 0)

    if not company_name:
        return CohortVerdict(False, None, 0, 0)

    # Free, in-memory title-template clustering signal. Always run regardless
    # of cohort size so smaller aggregators are not missed by the size gate.
    title_ratio, title_largest, title_total = _title_template_ratio(candidate_jobs)
    title_ratio_threshold = gate_cfg.get(
        "legitimacy_min_distinct_title_ratio", _DEFAULT_MIN_DISTINCT_TITLE_RATIO
    )
    title_cluster_positive = (
        title_total > 0
        and title_ratio <= title_ratio_threshold
        and title_largest >= _MIN_POSITIVE_SAMPLES
    )
    if title_cluster_positive:
        return CohortVerdict(
            True,
            f"aggregator_suspected:title_template_collapse_{title_largest}_of_{title_total}",
            0,
            title_largest,
        )

    # Free, in-memory portfolio-board URL-taxonomy signal. Always run
    # regardless of cohort size — a Getro-style /companies/<slug>/ path
    # is a structural tell that does not depend on cohort scale. Closes
    # the 2026-08-24 Next Frontier Capital gap (#1930): 85/86 jobs from
    # /companies/aumni/, all JPMorganChase reqs laundered through a VC
    # firm's Getro portfolio board. The slug-vs-company affinity check
    # keeps a real company's own /companies/<self>/ path out of scope.
    slug, slug_count, _slug_total = _companies_slug_signal(candidate_jobs, company_name)
    if slug and slug_count >= _MIN_POSITIVE_SAMPLES:
        return CohortVerdict(
            True,
            f"aggregator_suspected:portfolio_board_companies_path_{slug}_{slug_count}_postings",
            0,
            slug_count,
        )

    threshold = gate_cfg.get("legitimacy_large_cohort_threshold", _DEFAULT_LARGE_COHORT_THRESHOLD)
    if len(candidate_jobs) < threshold:
        return CohortVerdict(False, None, 0, 0)

    sample_size = gate_cfg.get("legitimacy_sample_size", _DEFAULT_SAMPLE_SIZE)
    urls = [j["url"] for j in candidate_jobs if j.get("url")]
    sample_urls = _sample_urls(urls, sample_size)

    offbrand_names: set[str] = set()
    offbrand_hits = 0
    chrome_hits = 0
    sampled_with_data = 0

    for url in sample_urls:
        posting = _fetch_posting_signal(url)
        if posting is None:
            continue
        sampled_with_data += 1

        org_name = _hiring_org_name(posting)
        if org_name and not name_slug_affinity(company_name, org_name):
            offbrand_names.add(org_name.lower())
            offbrand_hits += 1

        location = _location_from_jsonld(posting)
        if _location_is_chrome(location):
            chrome_hits += 1

    # Off-brand signal: >= _MIN_POSITIVE_SAMPLES independent sampled
    # postings carrying a hiringOrganization name NOT affine to the
    # crawled company. Covers BOTH the multi-distinct-employer case
    # (>= 2 distinct off-brand names) AND the single-consistent-off-brand
    # case (one wrong org name, e.g. "Aumni" vs "Next Frontier Capital",
    # repeated across >= 2 independent samples). The distinct-name count
    # was the #1144 gate's original independence proxy; the independent-
    # sample count is the correct one (#1930 blind spot (b)).
    if offbrand_hits >= _MIN_POSITIVE_SAMPLES:
        if len(offbrand_names) >= _MIN_POSITIVE_SAMPLES:
            reason = (
                f"aggregator_suspected:{len(offbrand_names)}_distinct_employers"
                f"_in_{sampled_with_data}_sampled"
            )
            positive = len(offbrand_names)
        else:
            sole_name = next(iter(offbrand_names))
            reason = (
                f"aggregator_suspected:consistent_offbrand_{sole_name}"
                f"_in_{offbrand_hits}_of_{sampled_with_data}_sampled"
            )
            positive = offbrand_hits
        return CohortVerdict(True, reason, sampled_with_data, positive)
    if chrome_hits >= _MIN_POSITIVE_SAMPLES:
        return CohortVerdict(
            True,
            f"aggregator_suspected:location_chrome_bleed_x{chrome_hits}_in_{sampled_with_data}_sampled",
            sampled_with_data,
            chrome_hits,
        )
    return CohortVerdict(False, None, sampled_with_data, max(offbrand_hits, chrome_hits))


def record_legitimacy_flag(  # PORT-SEAM: db_path param dropped -- svc.connection_factory() is zero-arg (L-0464)
    company_id: int, reason: str
) -> None:
    """Persist a cohort-legitimacy flag on the company row for human review.

    Does not touch `scan_enabled` or delete anything — the crawler's own
    batch-selection query excludes flagged rows going forward (see
    `crawl_careers_batch`'s `careers_crawl_flag_reason IS NULL` guard) until
    a human clears the flag or denylists the domain.
    """
    svc = get_services()  # PORT-SEAM: seam (L-0464)
    with svc.connection_factory() as conn:  # PORT-SEAM: seam (L-0464)
        conn.execute(
            "UPDATE companies SET careers_crawl_flag_reason = ? WHERE id = ?",
            (reason, company_id),
        )
        conn.commit()
