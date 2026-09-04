# PORTED from tests/test_cohort_legitimacy.py @ a7f0f38a85dfa0af4d305c04da833785f723d649 (private job-cannon). Ledger L-0580.
"""Tests for the sitemap-tier cohort-legitimacy gate (#1144 follow-up).

Covers the pure scoring logic in isolation from the sitemap tier's own
wiring (that side is covered in
tests/test_careers_crawler_sitemap.py::TestCohortLegitimacyWiring).

# PORT-SEAM: (L-0464, already-landed public module) tests/test_careers_crawler_sitemap.py's
# TestCohortLegitimacyWiring is a private test, not ported here -- the sitemap tier's own
# wiring is a separate ledger row's scope.
#
# - _hiring_org_name is no longer defined in _cohort_legitimacy.py -- the
#   public port relocated it to jobcannon.engine.identity_evidence (shared
#   by more than one caller). Imported from there below.
# - record_legitimacy_flag dropped its db_path positional arg in favor of
#   get_services().connection_factory() (the ScanServices seam) --
#   TestRecordLegitimacyFlag is adapted accordingly, using the shared
#   tests/engine/helpers/ats_scan_services.py connection-factory builder
#   instead of a bare sqlite3.connect(db_path).
#
# Every other pure function (_cluster_titles, _companies_slug_signal,
# _location_is_chrome, _sample_urls, _title_template_ratio,
# evaluate_cohort_legitimacy, CohortVerdict) carries with an unchanged
# signature -- this file is otherwise a straight port, no tests dropped.
"""

from __future__ import annotations

# PORT-SEAM: os/sqlite3/tempfile imports dropped -- TestRecordLegitimacyFlag's fixture now
# uses tmp_path + the shared ats_scan_services helpers instead (see notes above)

from unittest.mock import patch

import pytest

from jobcannon.engine import services
from jobcannon.engine.careers_crawler._cohort_legitimacy import (
    CohortVerdict,
    _cluster_titles,
    _companies_slug_signal,
    # PORT-SEAM: _hiring_org_name relocated to jobcannon.engine.identity_evidence (L-0464)
    _location_is_chrome,
    _sample_urls,
    _title_template_ratio,
    evaluate_cohort_legitimacy,
    record_legitimacy_flag,
)
from jobcannon.engine.identity_evidence import (  # PORT-SEAM: relocated (L-0464)
    _hiring_org_name,
)

from tests.engine.helpers.ats_scan_services import make_scan_services, open_connection

_GATE_PATCH_TARGET = "jobcannon.engine.careers_crawler._cohort_legitimacy._fetch_posting_signal"


def _jobs(n: int) -> list[dict]:
    return [
        {"title": f"Engineer {i}", "url": f"https://co.com/jobs/engineer-{i}", "description": ""}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# evaluate_cohort_legitimacy — trip-wire / no-op conditions (no network)
# ---------------------------------------------------------------------------


class TestGateNoOpConditions:
    def test_below_threshold_never_flags_and_never_fetches(self):
        with patch(_GATE_PATCH_TARGET) as mock_fetch:
            verdict = evaluate_cohort_legitimacy("Acme Corp", _jobs(7), {})
        assert verdict == CohortVerdict(False, None, 0, 0)
        mock_fetch.assert_not_called()

    def test_empty_company_name_never_flags_and_never_fetches(self):
        with patch(_GATE_PATCH_TARGET) as mock_fetch:
            verdict = evaluate_cohort_legitimacy("", _jobs(200), {})
        assert verdict.flagged is False
        mock_fetch.assert_not_called()

    def test_gate_disabled_via_config_never_flags_and_never_fetches(self):
        config = {"careers_crawl": {"legitimacy_gate_enabled": False}}
        with patch(_GATE_PATCH_TARGET) as mock_fetch:
            verdict = evaluate_cohort_legitimacy("Acme Corp", _jobs(200), config)
        assert verdict.flagged is False
        mock_fetch.assert_not_called()

    def test_none_config_uses_defaults(self):
        """A None config (careers_crawl key absent entirely) must not raise."""
        with patch(_GATE_PATCH_TARGET) as mock_fetch:
            verdict = evaluate_cohort_legitimacy("Acme Corp", _jobs(7), None)
        assert verdict.flagged is False
        mock_fetch.assert_not_called()

    def test_custom_threshold_from_config(self):
        """A lowered threshold trips the network fetch on a mid-size cohort
        that would otherwise be below the default threshold."""
        config = {"careers_crawl": {"legitimacy_large_cohort_threshold": 5}}
        with patch(_GATE_PATCH_TARGET, return_value=None) as mock_fetch:
            evaluate_cohort_legitimacy("Acme Corp", _jobs(10), config)
        assert mock_fetch.called

    def test_default_threshold_triggers_network_fetch_for_mid_size_cohort(self):
        """With the lowered default threshold, a 15-job cohort now exercises
        the bounded sample-fetch path (previously always below 80)."""
        with patch(_GATE_PATCH_TARGET, return_value=None) as mock_fetch:
            evaluate_cohort_legitimacy("Acme Corp", _jobs(15), {})
        assert mock_fetch.called


# ---------------------------------------------------------------------------
# evaluate_cohort_legitimacy — title-template clustering signal
# ---------------------------------------------------------------------------


def _umemployed_jobs(n: int = 44) -> list[dict]:
    """Build a cohort shaped like the 2026-07-13 UmEmployed aggregator run.

    Most titles are variants of "<Data Scientist ...> At General Tech Jobs",
    with adjective permutations, numbered duplicates, one literal template
    leak, and one non-matching outlier.
    """
    adjectives = [
        "",
        "Senior",
        "Remote",
        "Senior Remote",
        "Remote Senior",
        "Principal",
        "Principal Remote",
        "Lead",
        "Staff",
        "Junior",
        "Mid Level",
        "Experienced",
    ]
    jobs: list[dict] = []
    i = 0
    for adj in adjectives:
        for variant in ("Data Scientist", "Data Scientist"):
            if i >= n:
                break
            parts = [p for p in (adj, variant, "At General Tech Jobs") if p]
            jobs.append(
                {
                    "title": " ".join(parts),
                    "url": f"https://umemployed.com/jobs/{i}",
                    "description": "",
                }
            )
            i += 1
    # Numbered near-duplicate and literal template leak.
    extras = [
        "Data Scientist At General Tech Jobs 1/4/5/6/7/8",
        "Data Scientist At I12company",
        "Data Scientist Mit Fuhrungsverantwortung In Teilzeit Remote Oder Karlsruhe",
    ]
    for extra in extras:
        if i >= n:
            break
        jobs.append({"title": extra, "url": f"https://umemployed.com/jobs/{i}", "description": ""})
        i += 1
    # If still short, add more suffix variants.
    while i < n:
        jobs.append(
            {
                "title": f"Remote Data Scientist At General Tech Jobs {i}",
                "url": f"https://umemployed.com/jobs/{i}",
                "description": "",
            }
        )
        i += 1
    return jobs


def _distinct_jobs(n: int = 44) -> list[dict]:
    """A genuinely varied small-employer cohort with no templated suffix."""
    roles = [
        "Software Engineer",
        "Data Scientist",
        "Product Manager",
        "UX Designer",
        "DevOps Engineer",
        "Sales Representative",
        "Customer Success Manager",
        "Marketing Manager",
        "Account Executive",
        "Machine Learning Engineer",
        "Backend Engineer",
        "Frontend Engineer",
        "QA Engineer",
        "Security Engineer",
        "Site Reliability Engineer",
        "Data Engineer",
        "Analytics Engineer",
        "Research Scientist",
        "Product Designer",
        "Engineering Manager",
        "Tech Lead",
        "Solutions Architect",
        "Support Engineer",
        "Growth Manager",
        "Operations Manager",
        "Finance Analyst",
        "Legal Counsel",
        "HR Business Partner",
        "Recruiter",
        "Office Manager",
        "Content Writer",
        "Community Manager",
        "Partnerships Manager",
        "Business Development",
        "Strategy Analyst",
        "Supply Chain Manager",
        "Logistics Coordinator",
        "Event Coordinator",
        "Product Marketing",
        "Brand Designer",
        "Systems Administrator",
        "Network Engineer",
        "Database Administrator",
        "Release Manager",
        "Technical Writer",
        "Program Manager",
    ]
    return [
        {"title": roles[i % len(roles)], "url": f"https://realco.com/jobs/{i}", "description": ""}
        for i in range(n)
    ]


class TestTitleTemplateSignal:
    def test_umemployed_like_cohort_flagged_by_title_cluster(self):
        """The 44-posting UmEmployed cohort is flagged by the free title
        signal even though it is well below the old 80 threshold."""
        with patch(_GATE_PATCH_TARGET) as mock_fetch:
            verdict = evaluate_cohort_legitimacy("UmEmployed", _umemployed_jobs(44), {})
        assert verdict.flagged is True
        assert "title_template" in verdict.reason
        assert verdict.sampled == 0
        mock_fetch.assert_not_called()

    def test_distinct_titles_not_flagged(self):
        """A real small employer with 44 genuinely distinct titles is not
        flagged, even though the lowered default threshold now runs the
        network sample-fetch cross-check on a cohort this size."""
        clean = {"hiringOrganization": {"name": "RealCo"}}
        with patch(_GATE_PATCH_TARGET, return_value=clean) as mock_fetch:
            verdict = evaluate_cohort_legitimacy("RealCo", _distinct_jobs(44), {})
        assert verdict.flagged is False
        assert mock_fetch.called

    def test_title_cluster_runs_regardless_of_size_floor(self):
        """A tiny templated cohort is caught by the free title signal
        without ever reaching the network sample-fetch size gate."""
        jobs = [
            {
                "title": f"{adj} Data Scientist At General Tech Jobs".strip(),
                "url": f"https://agg.com/jobs/{i}",
                "description": "",
            }
            for i, adj in enumerate(["", "Senior", "Remote", "Principal", "Lead"])
        ]
        with patch(_GATE_PATCH_TARGET) as mock_fetch:
            verdict = evaluate_cohort_legitimacy("AggCo", jobs, {})
        assert verdict.flagged is True
        assert "title_template" in verdict.reason
        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# evaluate_cohort_legitimacy — Next Frontier Capital fixture (#1921)
#
# Pinned evidence packet:
#   logs/nightly_monitor/2026-08-23/checkpoint_Careers-crawl_17920_1787486400.json
#   — companies_crawled: 15, jobs_found: 91, jobs_new: 85,
#     legitimacy_flagged: 0, sitemap_hits: 1, playwright_rendered: 12,
#     api_cached: 1, ai_replayed: 1. The crawl imported 85 new jobs across
#     15 companies; a large share were minted under "Next Frontier Capital"
#     with dedup keys of the shape ``next frontier capital|<8-digit id>
#     <title>``. The bodies are JPMorganChase postings — a multi-employer
#     aggregator board attributed to an unrelated company row.
# ---------------------------------------------------------------------------


_NFC_REQ_IDS = [
    "90704021",
    "90703912",
    "90394878",
    "90394854",
    "90394584",
    "88086354",
    "87961311",
    "86240066",
    "85308871",
    "83824821",
    "85025486",
    "85623509",
    "84128022",
    "87291540",
    "86406764",
]

_NFC_ROLE_TITLES = [
    "Data Scientist Senior Associate",
    "Quant Analyst Senior Associate",
    "Card Analytics Data Scientist Lead Vice President",
    "Marketing Data Scientist Senior Associate Consumer Bank",
    "AWM Product Owner Business Analyst",
    "Quant Analytics Manager Deposit Pricing and Analytics",
    "Data Engineer Senior Associate",
    "Software Engineer Vice President",
    "Risk Analyst Associate",
    "Business Analyst Vice President",
    "Data Scientist Lead Vice President",
    "Quant Developer Senior Associate",
    "Analytics Manager Vice President",
    "Product Manager Associate",
    "Credit Risk Data Scientist Vice President",
]


def _nfc_fixture_jobs() -> list[dict]:
    """Cohort shaped like the 2026-08-23 Next Frontier Capital leak.

    Company ``Next Frontier Capital`` (a venture firm); each posting's
    title carries a bare 8-digit JPMorganChase requisition id prefix.
    The titles are distinct JPMorganChase lines of business (Card,
    Consumer Bank, AWM, Quant) so the free title-template clustering
    signal does NOT collapse them — the hiring-org variance signal is
    the one that fires.
    """
    jobs: list[dict] = []
    for i, req_id in enumerate(_NFC_REQ_IDS):
        title = f"{req_id} {_NFC_ROLE_TITLES[i % len(_NFC_ROLE_TITLES)]}"
        jobs.append(
            {
                "title": title,
                "url": f"https://nextfrontiercapital.com/careers/{req_id}",
                "description": "",
            }
        )
    return jobs


def _nfc_fetch(url: str) -> dict | None:
    """Mock `_fetch_posting_signal` for the NFC fixture.

    Most postings resolve to JPMorgan Chase; a minority resolve to Morgan
    Stanley — the board is a multi-employer aggregator, predominantly
    JPMorganChase. The gate's ``_MIN_POSITIVE_SAMPLES >= 2`` distinct-
    employer requirement fires on the two independent off-brand names.
    """
    if "85025486" in url or "87291540" in url:
        return {"hiringOrganization": {"name": "Morgan Stanley"}}
    return {"hiringOrganization": {"name": "JPMorgan Chase"}}


class TestNextFrontierCapitalFixture:
    """The observed Next Frontier Capital fixture is FLAGGED by the gate's
    hiring-org variance signal — two independent off-brand employers
    (JPMorgan Chase + Morgan Stanley) in the bounded sample."""

    def test_nfc_cohort_flagged_by_hiring_org_variance(self):
        """The 15-posting NFC cohort (>= the default large-cohort threshold
        of 10) triggers the bounded sample-fetch, which finds >= 2 distinct
        off-brand employers — FLAGGED."""
        with patch(_GATE_PATCH_TARGET, side_effect=_nfc_fetch):
            verdict = evaluate_cohort_legitimacy("Next Frontier Capital", _nfc_fixture_jobs(), {})
        assert verdict.flagged is True
        assert verdict.reason is not None
        assert verdict.reason.startswith("aggregator_suspected:")
        assert "distinct_employers" in verdict.reason
        assert verdict.positive_signals >= 2

    def test_nfc_titles_do_not_collapse_onto_template(self):
        """The NFC titles are distinct JPMorganChase lines of business with
        unique 8-digit requisition id prefixes — the free title-template
        clustering signal must NOT fire (the distinct-title ratio is high).
        This confirms the hiring-org variance signal is the one that flags,
        not the title-template signal."""
        # PORT-SEAM: redundant local re-import of _title_template_ratio dropped --
        # already imported at module level above.
        ratio, largest, total = _title_template_ratio(_nfc_fixture_jobs())
        # 15 distinct titles → ratio close to 1.0, well above the 0.4
        # templated threshold. The title-template signal does not fire.
        assert ratio > 0.4
        assert largest < 2  # no cluster has >= _MIN_POSITIVE_SAMPLES


# ---------------------------------------------------------------------------
# evaluate_cohort_legitimacy — positive-evidence floor (never on 1 sample)
# ---------------------------------------------------------------------------


class TestPositiveEvidenceFloor:
    def test_single_offbrand_sample_does_not_flag(self):
        """One mismatched sample must never be enough — could be a fetch
        glitch or one mis-tagged posting, not proof of an aggregator."""

        def fake_fetch(url):
            if url.endswith("engineer-0"):
                return {"hiringOrganization": {"name": "Totally Unrelated Corp"}}
            return {"hiringOrganization": {"name": "Acme Corp"}}

        with patch(_GATE_PATCH_TARGET, side_effect=fake_fetch):
            verdict = evaluate_cohort_legitimacy("Acme Corp", _jobs(100), {})
        assert verdict.flagged is False

    def test_two_distinct_offbrand_employers_flags(self):
        def fake_fetch(url):
            if url.endswith("engineer-0"):
                return {"hiringOrganization": {"name": "Totally Unrelated Corp"}}
            if url.endswith("engineer-25"):
                return {"hiringOrganization": {"name": "Another Random Employer"}}
            return {"hiringOrganization": {"name": "Acme Corp"}}

        with patch(_GATE_PATCH_TARGET, side_effect=fake_fetch):
            verdict = evaluate_cohort_legitimacy("Acme Corp", _jobs(100), {})
        assert verdict.flagged is True
        assert verdict.reason is not None
        assert verdict.reason.startswith("aggregator_suspected:")
        assert verdict.positive_signals == 2

    def test_all_samples_matching_company_never_flags(self):
        def fake_fetch(_url):
            return {"hiringOrganization": {"name": "Acme Corp"}}

        with patch(_GATE_PATCH_TARGET, side_effect=fake_fetch):
            verdict = evaluate_cohort_legitimacy("Acme Corp", _jobs(100), {})
        assert verdict.flagged is False

    def test_no_structured_data_available_never_flags(self):
        """Fail-open: absence of JSON-LD data anywhere in the sample is
        never treated as evidence of anything (many legitimate careers
        pages simply don't emit hiringOrganization)."""
        with patch(_GATE_PATCH_TARGET, return_value=None):
            verdict = evaluate_cohort_legitimacy("Acme Corp", _jobs(100), {})
        assert verdict.flagged is False
        assert verdict.sampled == 0

    def test_missing_url_field_never_flags(self):
        """Candidate jobs missing a 'url' key are skipped without error."""
        jobs = [{"title": f"Engineer {i}", "description": ""} for i in range(100)]
        with patch(_GATE_PATCH_TARGET) as mock_fetch:
            verdict = evaluate_cohort_legitimacy("Acme Corp", jobs, {})
        assert verdict.flagged is False
        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# evaluate_cohort_legitimacy — single consistent off-brand org name (#1930)
#
# The #1144 gate's original off-brand signal required >= 2 DISTINCT
# hiringOrganization names. The 2026-08-24 Next Frontier Capital
# recurrence (#1930) is a cohort consistently labeled with ONE wrong
# employer ("Aumni" vs "Next Frontier Capital") — the distinct-name
# count never reached 2, so the gate never fired. The fix: count
# independent off-brand SAMPLES, not distinct names.
# ---------------------------------------------------------------------------


class TestSingleConsistentOffbrandName:
    def test_single_consistent_offbrand_name_flags(self):
        """#1930 blind spot (b): a cohort where every sampled posting
        resolves to the SAME single off-brand org name (not affine to
        the crawled company) must flag. The original gate required >= 2
        DISTINCT names and missed this — the independent-sample count is
        the correct independence proxy."""

        def fake_fetch(_url):
            return {"hiringOrganization": {"name": "Aumni"}}

        with patch(_GATE_PATCH_TARGET, side_effect=fake_fetch):
            verdict = evaluate_cohort_legitimacy("Next Frontier Capital", _jobs(100), {})
        assert verdict.flagged is True
        assert verdict.reason is not None
        assert verdict.reason.startswith("aggregator_suspected:")
        assert "consistent_offbrand_aumni" in verdict.reason
        assert verdict.positive_signals >= 2

    def test_single_offbrand_sample_still_does_not_flag(self):
        """The positive-evidence floor is preserved: a single off-brand
        sample (the rest matching the company) is still insufficient,
        even with the new sample-count semantics."""

        def fake_fetch(url):
            if url.endswith("engineer-0"):
                return {"hiringOrganization": {"name": "Aumni"}}
            return {"hiringOrganization": {"name": "Acme Corp"}}

        with patch(_GATE_PATCH_TARGET, side_effect=fake_fetch):
            verdict = evaluate_cohort_legitimacy("Acme Corp", _jobs(100), {})
        assert verdict.flagged is False

    def test_single_consistent_name_affine_to_company_does_not_flag(self):
        """If the one consistent org name IS affine to the crawled
        company, it is the company's own cohort — never flagged."""

        def fake_fetch(_url):
            return {"hiringOrganization": {"name": "Acme Corp"}}

        with patch(_GATE_PATCH_TARGET, side_effect=fake_fetch):
            verdict = evaluate_cohort_legitimacy("Acme Corp", _jobs(100), {})
        assert verdict.flagged is False


# ---------------------------------------------------------------------------
# evaluate_cohort_legitimacy — portfolio-board URL-taxonomy signal (#1930)
#
# A /companies/<slug>/ path shared across the cohort's job URLs is the
# structural tell of a white-labeled multi-employer portfolio board
# (Getro-powered VC portfolio boards et al.). Free, in-memory — no
# network fetch. The 2026-08-24 Next Frontier Capital case: 85/86 jobs
# from /companies/aumni/.
# ---------------------------------------------------------------------------


def _getro_cohort(n: int = 12, slug: str = "aumni") -> list[dict]:
    """Cohort shaped like a Getro portfolio board: every job URL nests
    under /companies/<slug>/, where <slug> is a portfolio company, not
    the crawled employer."""
    return [
        {
            "title": f"Engineer {i}",
            "url": f"https://nextfrontiercapital.com/companies/{slug}/jobs/{i}",
            "description": "",
        }
        for i in range(n)
    ]


class TestCompaniesSlugSignal:
    def test_getro_portfolio_board_path_flags_without_fetch(self):
        """A cohort whose job URLs share a /companies/<slug>/ path
        (slug NOT affine to the crawled company) is flagged by the free
        URL-shape signal — no network fetch required."""
        with patch(_GATE_PATCH_TARGET) as mock_fetch:
            verdict = evaluate_cohort_legitimacy(
                "Next Frontier Capital", _getro_cohort(12, "aumni"), {}
            )
        assert verdict.flagged is True
        assert verdict.reason is not None
        assert "portfolio_board_companies_path_aumni" in verdict.reason
        assert verdict.sampled == 0
        mock_fetch.assert_not_called()

    def test_signal_runs_below_size_floor(self):
        """The URL-shape signal is free and runs regardless of cohort
        size — a tiny Getro cohort (3 postings) is still caught."""
        with patch(_GATE_PATCH_TARGET) as mock_fetch:
            verdict = evaluate_cohort_legitimacy(
                "Next Frontier Capital", _getro_cohort(3, "aumni"), {}
            )
        assert verdict.flagged is True
        assert "portfolio_board_companies_path_aumni" in verdict.reason
        mock_fetch.assert_not_called()

    def test_slug_affine_to_company_does_not_flag(self):
        """A real company whose own careers site nests under
        /companies/<its-own-slug>/ is exempt — the slug is name-affine
        to the crawled company."""
        jobs = _getro_cohort(12, "acme")
        # PORT-SEAM: renamed to _mock_fetch (unused-variable lint, ruff F841)
        with patch(_GATE_PATCH_TARGET, return_value=None) as _mock_fetch:
            verdict = evaluate_cohort_legitimacy("Acme Corp", jobs, {})
        # Slug "acme" is affine to "Acme Corp" → URL-shape signal exempt.
        # Cohort is below the default large-cohort threshold (12 >= 10 so
        # the size gate DOES trip the fetch, but fetch returns None → no
        # data → not flagged).
        assert verdict.flagged is False

    def test_single_matching_posting_does_not_flag(self):
        """A single /companies/<slug>/ URL among an otherwise-clean
        cohort is insufficient — the positive-evidence floor (>= 2)
        applies to the URL-shape signal too."""
        jobs = [
            {"title": "Engineer 0", "url": "https://co.com/companies/foo/j/0", "description": ""},
            {"title": "Engineer 1", "url": "https://co.com/jobs/1", "description": ""},
            {"title": "Engineer 2", "url": "https://co.com/jobs/2", "description": ""},
        ]
        with patch(_GATE_PATCH_TARGET) as mock_fetch:
            verdict = evaluate_cohort_legitimacy("Unrelated Co", jobs, {})
        assert verdict.flagged is False
        mock_fetch.assert_not_called()

    def test_companies_slug_signal_helper_affine_exempt(self):
        """The helper returns an empty dominant slug when the dominant
        slug is affine to the company name (exempt path), but still
        reports total_matched for observability."""
        jobs = _getro_cohort(5, "acme")
        slug, count, total = _companies_slug_signal(jobs, "Acme Corp")
        assert slug == ""
        assert count == 0
        assert total == 5

    def test_companies_slug_signal_helper_no_path(self):
        """No /companies/<slug>/ path anywhere → empty signal."""
        jobs = _jobs(20)
        slug, count, total = _companies_slug_signal(jobs, "Acme Corp")
        assert slug == ""
        assert count == 0
        assert total == 0


# ---------------------------------------------------------------------------
# evaluate_cohort_legitimacy — location chrome-bleed signal
# ---------------------------------------------------------------------------


class TestLocationChromeSignal:
    def test_repeated_chrome_bleed_flags(self):
        """The tell that actually surfaced the role.com incident: aggregator
        sidebar text bled into a structured jobLocation field."""

        def fake_fetch(url):
            if url.endswith("engineer-0") or url.endswith("engineer-25"):
                return {
                    "hiringOrganization": {"name": "Acme Corp"},
                    "jobLocation": "Popular Jobs Near You",
                }
            return {"hiringOrganization": {"name": "Acme Corp"}}

        with patch(_GATE_PATCH_TARGET, side_effect=fake_fetch):
            verdict = evaluate_cohort_legitimacy("Acme Corp", _jobs(100), {})
        assert verdict.flagged is True
        assert verdict.reason is not None
        assert "location_chrome_bleed" in verdict.reason

    def test_single_chrome_hit_does_not_flag(self):
        def fake_fetch(url):
            if url.endswith("engineer-0"):
                return {
                    "hiringOrganization": {"name": "Acme Corp"},
                    "jobLocation": "Popular Jobs Near You",
                }
            return {"hiringOrganization": {"name": "Acme Corp"}}

        with patch(_GATE_PATCH_TARGET, side_effect=fake_fetch):
            verdict = evaluate_cohort_legitimacy("Acme Corp", _jobs(100), {})
        assert verdict.flagged is False


# ---------------------------------------------------------------------------
# _sample_urls — spread across the full cohort, not just the head
# ---------------------------------------------------------------------------


class TestSampleUrls:
    def test_returns_all_when_below_sample_size(self):
        urls = [f"u{i}" for i in range(5)]
        assert _sample_urls(urls, 8) == urls

    def test_spreads_across_full_list(self):
        """An aggregator can group one real employer's postings
        contiguously (e.g. per upstream feed) — sampling must not draw
        only from the head of the list."""
        urls = [f"u{i}" for i in range(100)]
        sample = _sample_urls(urls, 8)
        assert len(sample) == 8
        indices = [int(u[1:]) for u in sample]
        # Spread requirement: the sample must reach into the back half of
        # the list, not cluster entirely in the first 8.
        assert max(indices) >= 50
        assert indices == sorted(indices)  # order-preserving

    def test_zero_sample_size_returns_all(self):
        urls = [f"u{i}" for i in range(20)]
        assert _sample_urls(urls, 0) == urls


# ---------------------------------------------------------------------------
# _hiring_org_name — schema.org shape handling
# ---------------------------------------------------------------------------


class TestHiringOrgName:
    def test_plain_string_shape(self):
        assert _hiring_org_name({"hiringOrganization": "Acme Corp"}) == "Acme Corp"

    def test_organization_object_shape(self):
        posting = {"hiringOrganization": {"@type": "Organization", "name": "Acme Corp"}}
        assert _hiring_org_name(posting) == "Acme Corp"

    def test_absent_returns_empty(self):
        assert _hiring_org_name({}) == ""

    def test_malformed_object_without_name_returns_empty(self):
        assert _hiring_org_name({"hiringOrganization": {"@type": "Organization"}}) == ""

    def test_unexpected_type_returns_empty(self):
        assert _hiring_org_name({"hiringOrganization": 12345}) == ""


# ---------------------------------------------------------------------------
# _location_is_chrome
# ---------------------------------------------------------------------------


class TestLocationIsChrome:
    @pytest.mark.parametrize(
        "location",
        [
            "Popular Jobs Near You",
            "View all jobs in Engineering",
            "Trending Jobs This Week",
            "APPLY NOW",
        ],
    )
    def test_detects_chrome_substrings(self, location):
        assert _location_is_chrome(location) is True

    @pytest.mark.parametrize(
        "location",
        ["Hyderabad, India", "Remote", "San Francisco, CA", "", None],
    )
    def test_real_locations_not_flagged(self, location):
        assert _location_is_chrome(location) is False


# ---------------------------------------------------------------------------
# record_legitimacy_flag — DB persistence
#
# PORT-SEAM: (L-0464) the public signature dropped db_path in favor of
# get_services().connection_factory() (ScanServices seam) -- this fixture
# wires that seam against a real on-disk sqlite3 companies table using the
# shared tests/engine/helpers/ats_scan_services.py connection-factory
# builder, in place of the private test's bare sqlite3.connect(db_path)
# fixture (tmp_companies_db, a tempfile.mkstemp-backed yield fixture).
# ---------------------------------------------------------------------------


@pytest.fixture
def companies_db_services(tmp_path):
    # PORT-SEAM: tmp_path replaces tempfile.mkstemp + manual os.remove teardown
    import sqlite3

    db_path = str(tmp_path / "companies.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name_raw TEXT NOT NULL,
            careers_crawl_flag_reason TEXT DEFAULT NULL
        )"""
    )
    conn.execute("INSERT INTO companies (id, name_raw) VALUES (1, 'AggregatorCo')")
    conn.commit()
    conn.close()

    # PORT-SEAM: wires the ScanServices seam instead of returning a bare db_path
    services.set_services(make_scan_services(db_path))
    return db_path


class TestRecordLegitimacyFlag:
    def test_persists_reason_to_company_row(self, companies_db_services):
        # PORT-SEAM: db_path positional arg dropped (L-0464 seam)
        record_legitimacy_flag(1, "aggregator_suspected:test")

        # PORT-SEAM: open_connection (shared helper) replaces a bare sqlite3.connect
        with open_connection(companies_db_services) as conn:
            row = conn.execute(
                "SELECT careers_crawl_flag_reason FROM companies WHERE id = 1"
            ).fetchone()
        assert row["careers_crawl_flag_reason"] == "aggregator_suspected:test"


# ---------------------------------------------------------------------------
# _cluster_titles — order independence and degenerate-title handling
# ---------------------------------------------------------------------------


class TestClusterTitles:
    def test_order_independence(self):
        """The same title set in different orders must produce the same clusters."""
        a = "Data Scientist At General Tech Jobs"
        b = "Senior Data Scientist At General Tech Jobs Remote"
        c = "Senior Data Scientist At General Tech Jobs Remote Part Time"

        order1 = [a, b, c]
        order2 = [b, a, c]

        def normalize(clusters):
            return sorted(sorted(c) for c in clusters)

        assert normalize(_cluster_titles(order1)) == normalize(_cluster_titles(order2))

    def test_all_punctuation_titles_create_no_orphan_clusters(self):
        """Titles that normalize to nothing should be skipped, not each placed
        in an empty cluster."""
        assert _cluster_titles(["!!!", "???", "..."]) == []
        assert _cluster_titles(["!!!", "???", ""]) == []


class TestTitleTemplateRatio:
    def test_degenerate_titles_excluded_from_ratio(self):
        """All-punctuation titles must not inflate the denominator or create
        orphan clusters that hide a real template collapse."""
        jobs = [
            {"title": "Data Scientist At General Tech Jobs", "url": "", "description": ""},
            {"title": "!!!", "url": "", "description": ""},
            {"title": "???", "url": "", "description": ""},
            {"title": "Senior Data Scientist At General Tech Jobs", "url": "", "description": ""},
        ]
        ratio, largest, total = _title_template_ratio(jobs)
        assert ratio == 0.5
        assert largest == 2
        assert total == 2

    def test_exact_equality_boundary_flags(self):
        """A distinct-title ratio exactly equal to the threshold must be treated
        as templated (the code enforces 'stay strictly above')."""
        jobs = [
            # Cluster A: 3 templated titles
            {"title": "Data Scientist At General Tech Jobs", "url": "", "description": ""},
            {"title": "Senior Data Scientist At General Tech Jobs", "url": "", "description": ""},
            {"title": "Remote Data Scientist At General Tech Jobs", "url": "", "description": ""},
            # Cluster B: 2 distinct titles, no token overlap with cluster A
            {"title": "Software Engineer Backend", "url": "", "description": ""},
            {"title": "Senior Software Engineer Backend", "url": "", "description": ""},
        ]
        with patch(_GATE_PATCH_TARGET) as mock_fetch:
            verdict = evaluate_cohort_legitimacy("Agg", jobs, {})
        assert verdict.flagged is True
        assert "title_template" in verdict.reason
        assert verdict.sampled == 0
        mock_fetch.assert_not_called()
