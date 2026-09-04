# PORTED from tests/test_autoheal_careers_rekey.py @ a7f0f38a85dfa0af4d305c04da833785f723d649 (private job-cannon). Ledger L-0576.
"""Phase D / D3 — careers per-company re-keying + structural detection counts.

Covers:
- ``careers_source_key`` (I5: hostname only, lowercase, port stripped,
  garbage → ``careers:unknown``).
- The ``_extract_candidates`` / ``_filter_candidates`` split:
  ``_extract_jobs_from_soup`` output unchanged (regression);
  ``_extract_candidates`` returns the structural superset (non-matching
  titles included; nav/metadata-blob links excluded).
- Capture re-key + structural counts at all 3 sites (I4): a page full of
  structural candidates with zero title-matches is NOT a break
  (``job_count`` = structural, ``filtered_count`` rides in output_json);
  a genuinely empty page records ``job_count=0``.

# PORT-SEAM: DROPPED (private-only surface, listed in the PR body): the
# static/Playwright capture tests (test_static_capture_*, test_playwright_*_
# capture_*) asserted on corpus_sample/source_health rows written by the
# private autoheal.health_monitor.record_extraction writer, which is DIES
# (single-user-desktop -- L-0138/L-0139/L-0140). The public seam
# (ScanServices.record_careers_extraction, _autoheal_seam.py L-0469) is
# call-contract-only and unwired by default -- same precedent as
# tests/engine/test_autoheal_email_capture.py (L-0562) dropping the
# break-counter half of that port. The json/sqlite3/mock imports below, the
# _try_static_extract import, and the _CAREERS_URL/_PAD/_JOB_HTML/etc.
# fixtures and capture test bodies that followed in the private file fed
# only those dropped tests.
"""

from __future__ import annotations

# PORT-SEAM: json/sqlite3/unittest.mock imports dropped (fed only the DIES capture tests above)

from bs4 import BeautifulSoup

from jobcannon.engine.careers_crawler._autoheal_seam import careers_source_key
from jobcannon.engine.careers_crawler._static_tier import (
    _extract_candidates,
    _extract_jobs_from_soup,
    _filter_candidates,
    # PORT-SEAM: _try_static_extract import dropped (fed only the DIES capture tests above)
)

# PORT-SEAM: Helpers block (_CAREERS_URL/_PAD/_JOB_HTML/_NO_MATCH_HTML/_EMPTY_HTML fixtures,
# _setup_db/_mock_response/_capture_row helpers) dropped here -- see the module docstring
# DROPPED note above.

# ---------------------------------------------------------------------------
# careers_source_key (I5)
# ---------------------------------------------------------------------------


def test_key_https_url():
    assert careers_source_key("https://Example.com/careers") == "careers:example.com"


def test_key_subdomain_lowercased():
    assert careers_source_key("https://Jobs.Acme.COM/openings") == "careers:jobs.acme.com"


def test_key_port_stripped():
    assert careers_source_key("https://x.acme.com:8443/jobs") == "careers:x.acme.com"


def test_key_empty_and_garbage():
    assert careers_source_key("") == "careers:unknown"
    assert careers_source_key(None) == "careers:unknown"
    assert careers_source_key("not a url") == "careers:unknown"


# ---------------------------------------------------------------------------
# Candidate / filter split
# ---------------------------------------------------------------------------

_SPLIT_HTML = (
    "<html><body>"
    '<a href="/about">About us page link</a>'
    '<a href="/jobs/eng-1">Software Engineer</a>'
    '<a href="/jobs/sales-1">Sales Executive</a>'
    '<a href="/jobs/eng-1">Apply Now and join the team</a>'
    "</body></html>"
)


def test_extract_jobs_from_soup_output_unchanged():
    """Regression: the public wrapper still returns only title-matched jobs."""
    soup = BeautifulSoup(_SPLIT_HTML, "html.parser")
    jobs = _extract_jobs_from_soup(soup, "https://acme.com", ["engineer"], [])
    assert [j["title"] for j in jobs] == ["Software Engineer"]
    assert jobs[0]["url"] == "https://acme.com/jobs/eng-1"


def test_extract_candidates_is_structural_superset():
    """Candidates include non-matching titles; nav links are excluded."""
    soup = BeautifulSoup(_SPLIT_HTML, "html.parser")
    cands = _extract_candidates(soup, "https://acme.com")
    titles = [c["title"] for c in cands]
    assert "Software Engineer" in titles
    assert "Sales Executive" in titles  # no title filter (I4)
    assert "About us page link" not in titles  # nav path excluded (structural)


def test_filter_candidates_dedups_after_matching():
    """A generic 'Apply' anchor sharing the title anchor's URL never shadows it."""
    soup = BeautifulSoup(_SPLIT_HTML, "html.parser")
    cands = _extract_candidates(soup, "https://acme.com")
    jobs = _filter_candidates(cands, ["engineer"], [])
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Software Engineer"


def test_filter_candidates_excludes_by_keyword():
    soup = BeautifulSoup(_SPLIT_HTML, "html.parser")
    cands = _extract_candidates(soup, "https://acme.com")
    jobs = _filter_candidates(cands, ["engineer", "sales"], ["sales"])
    assert [j["title"] for j in jobs] == ["Software Engineer"]


# PORT-SEAM: static-tier capture tests (test_static_capture_keys_per_company,
# test_static_capture_roles_filled_is_not_a_break, test_static_capture_empty_page_records_zero,
# test_static_capture_break_detection_per_company) and Playwright-tier capture tests
# (_FakePage, test_playwright_render_capture_keys_per_company,
# test_playwright_active_capture_uses_final_page_structural_count) dropped here -- see the
# module docstring DROPPED note above.
