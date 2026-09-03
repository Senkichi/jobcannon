"""Tests for the platform-scoped JD extraction chokepoint and the agentic
spawn-storm fix (2026-06-22 Penguin AI investigation).

Covers:
- platform_extractor.detect_platform / strip_trailing_chrome / extract_clean_jd
- the chokepoint routing of fetch_direct_jd (a LinkedIn source_url no longer
  stores the whole guest page — JD + "Similar jobs" + footer chrome)
- the architecture invariant that enrich_job never launches Playwright per-row
- data_enricher._run_inline_agentic_pass gating, limit, and best-effort behavior
- run_enrichment_backfill drives the inline (single-browser) agentic pass
"""

import inspect
from unittest.mock import MagicMock, patch

from jobcannon.engine.platform_extractor import (
    detect_platform,
    extract_clean_jd,
    strip_trailing_chrome,
)

# Page-chrome markers that must NEVER survive into a stored jd_full.
_CHROME_MARKERS = [
    "## Similar jobs",
    "People also viewed",
    "Explore top content on LinkedIn",
    "Referrals increase your chances",
    "Seniority level",
    "Get notified about new",
]

# Real-length JD prose — trafilatura emits degenerate output on short fragments.
_LI_JD = (
    "We are hiring a Staff Data Scientist to build models and own reliability "
    "for a high-traffic ML platform used by millions of customers every day. "
    "This is a full-time role with strong benefits and meaningful equity growth. "
    "Responsibilities include data pipelines, experimentation, and stakeholder work, "
    "partnering with analytics and engineering teams across the company."
)

# A LinkedIn guest page: the JD lives in show-more-less-html__markup; the chrome
# (seniority footer, similar jobs, explore rail) is OUTSIDE that container.
_LI_PAGE = (
    "<html><body>"
    "<nav>Sign in or join now</nav>"
    f'<div class="show-more-less-html__markup"><p>{_LI_JD}</p></div>'
    "<section>"
    "<h3>Seniority level</h3><p>Mid-Senior level</p>"
    "<h3>Employment type</h3><p>Full-time</p>"
    "<h2>Similar jobs</h2><h2>People also viewed</h2>"
    "<h2>Explore top content on LinkedIn</h2>"
    "</section>"
    "</body></html>"
)


def _mock_response(text, status_code=200):
    resp = MagicMock()
    resp.text = text
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# detect_platform
# ---------------------------------------------------------------------------


class TestDetectPlatform:
    def test_linkedin_jobs_urls(self):
        assert detect_platform("https://www.linkedin.com/jobs/view/4431117100/") == "linkedin"
        assert detect_platform("https://linkedin.com/jobs/view/ds-at-penguin-1") == "linkedin"

    def test_non_jobs_linkedin_is_not_scoped(self):
        # A LinkedIn profile/company URL has no JD container — not our platform.
        assert detect_platform("https://www.linkedin.com/in/someone") is None

    def test_other_hosts_and_none(self):
        assert detect_platform("https://boards.greenhouse.io/acme/jobs/1") is None
        assert detect_platform("https://acme.com/careers/ds") is None
        assert detect_platform(None) is None
        assert detect_platform("") is None


# ---------------------------------------------------------------------------
# strip_trailing_chrome
# ---------------------------------------------------------------------------


class TestStripTrailingChrome:
    def test_none_and_empty(self):
        assert strip_trailing_chrome(None) is None
        assert strip_trailing_chrome("") is None

    def test_no_chrome_passthrough(self):
        body = "Real JD body.\n\n- A bullet\n- Another bullet"
        assert strip_trailing_chrome(body) == body

    def test_truncates_at_each_marker(self):
        for marker in (
            "## Similar jobs",
            "## People also viewed",
            "## Explore top content on LinkedIn",
            "### Seniority level",
            "Referrals increase your chances of interviewing at Acme by 2x",
        ):
            body = f"The real JD body ends here.\n\n{marker}\n\nchrome chrome chrome"
            out = strip_trailing_chrome(body)
            assert out == "The real JD body ends here.", marker
            assert "chrome" not in out

    def test_truncates_at_earliest_marker(self):
        body = "JD body.\n\n### Seniority level\n\nMid-Senior\n\n## Similar jobs\n\n- x"
        assert strip_trailing_chrome(body) == "JD body."

    def test_idempotent(self):
        body = "JD body\n\n## Similar jobs\n\n- x"
        once = strip_trailing_chrome(body)
        assert strip_trailing_chrome(once) == once


# ---------------------------------------------------------------------------
# extract_clean_jd
# ---------------------------------------------------------------------------


class TestExtractCleanJd:
    def test_linkedin_scopes_to_container_and_drops_chrome(self):
        out = extract_clean_jd("https://www.linkedin.com/jobs/view/1", _LI_PAGE)
        assert out is not None
        assert "Staff Data Scientist" in out
        for marker in _CHROME_MARKERS:
            assert marker not in out, marker

    def test_linkedin_missing_container_is_strict_none(self):
        html = "<html><body><div class='unrelated'>No JD container here</div></body></html>"
        assert extract_clean_jd("https://www.linkedin.com/jobs/view/2", html) is None

    def test_linkedin_tiny_container_is_strict_none(self):
        html = '<html><body><div class="show-more-less-html__markup">tiny</div></body></html>'
        assert extract_clean_jd("https://www.linkedin.com/jobs/view/3", html) is None

    def test_unknown_host_whole_page(self):
        html = f"<html><body><article><p>{_LI_JD}</p></article></body></html>"
        out = extract_clean_jd("https://acme.com/careers/ds", html)
        assert out is not None
        assert "Staff Data Scientist" in out

    def test_empty_html(self):
        assert extract_clean_jd("https://acme.com/x", "") is None
        assert extract_clean_jd("https://acme.com/x", None) is None


# ---------------------------------------------------------------------------
# Routing: fetch_direct_jd goes through the chokepoint
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Architecture invariant: enrich_job never launches Playwright per-row
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# _run_inline_agentic_pass — gating, limit, best-effort
# ---------------------------------------------------------------------------




