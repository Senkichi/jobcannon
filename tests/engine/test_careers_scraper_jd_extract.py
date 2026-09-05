# PORTED from tests/test_careers_scraper_jd_extract.py @ b1f596520bf1a1e987d6708813618f6c6c468736 (private job-cannon). Ledger L-0601.
"""Tests for _scraper_extract._fetch_job_description's trafilatura wiring (JD Layer 2a).

`_fetch_job_description` delegates HTML→text extraction to the engine's
single chokepoint (`platform_extractor.extract_clean_jd`, which for an
unrecognised host falls through to `html_extract.html_to_clean_text`
(trafilatura → markdown + block dedup, BeautifulSoup fallback)). These tests
assert at the call-site level that:
  - gross within-document block duplication is collapsed and nav/footer chrome
    is stripped,
  - terse `Compensation: $X` / single-line `Location:` text survives
    (favor_recall regression guard),
  - the auth-wall signature check still rejects login-walled pages (returns ""),
  - the empty-string-on-failure contract holds (returns "", never None).

# PORT-SEAM: the private version mocked ``requests.get`` directly;
# ``_fetch_job_description`` now fetches via ``http_fetch.fetch_with_deadline``,
# which resolves ``requests.get`` from its OWN module at call time -- so the
# patch target here is ``jobcannon.engine.http_fetch.requests.get``, not this
# module's. The two "real extraction" tests also need a ``ScanServices``
# bundle configured (``jd_storage_max_chars``) since the function calls
# ``get_services()`` on the success path.
"""

from unittest.mock import MagicMock, patch

from jobcannon.engine import services
from jobcannon.engine._scraper_extract import _fetch_job_description
from tests.engine.helpers.ats_scan_services import make_scan_services

# A realistic, real-length JD block. trafilatura emits degenerate output on
# too-short documents, so fixtures use production-length prose.
_JD_BLOCK = """<h2>About the Role</h2>
<p>We are hiring a Senior Platform Engineer to join our infrastructure team and
own reliability for a high-traffic API used by millions of customers every day.
This is a full-time hybrid role with strong benefits, equity, and real growth.</p>
<h3>Responsibilities</h3>
<ul>
<li>Build and own batch and streaming data pipelines end to end.</li>
<li>Partner with analytics and ML teams on the warehouse data model.</li>
</ul>"""


def _mock_response(text, status_code=200):
    resp = MagicMock()
    resp.text = text
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


def test_fetch_job_description_uses_trafilatura_dedup(tmp_path):
    """A JD block repeated >=10x inside nav/header/footer chrome collapses to one
    occurrence and excludes boilerplate."""
    services.set_services(make_scan_services(str(tmp_path / "scan.db")))
    html = (
        "<html><body>"
        "<nav>Home About Careers Login Sign In</nav>"
        "<header>MegaCorp Global Holdings</header>"
        f"<main>{_JD_BLOCK * 12}</main>"
        "<footer>Equal Opportunity Employer. Copyright 2026 MegaCorp. Privacy Policy.</footer>"
        "</body></html>"
    )
    resp = _mock_response(html)

    with patch("jobcannon.engine.http_fetch.requests.get", return_value=resp):
        out = _fetch_job_description("https://example.com/jobs/1")

    # JD content present exactly once, not twelve times.
    assert out.count("About the Role") == 1
    assert out.count("own reliability for a high-traffic API") == 1
    # Page chrome stripped by trafilatura's structure-aware extraction.
    assert "Privacy Policy" not in out
    assert "Equal Opportunity Employer" not in out
    assert "Sign In" not in out


def test_fetch_job_description_keeps_terse_compensation_and_location(tmp_path):
    """A standalone terse comp line and a single-line location both survive the
    favor_recall extraction at the call-site level."""
    services.set_services(make_scan_services(str(tmp_path / "scan.db")))
    html = (
        "<html><body><main>"
        f"{_JD_BLOCK}"
        "<p>Compensation: $185,000</p>"
        "<p>Location: Remote (US)</p>"
        "</main></body></html>"
    )
    resp = _mock_response(html)

    with patch("jobcannon.engine.http_fetch.requests.get", return_value=resp):
        out = _fetch_job_description("https://example.com/jobs/2")

    assert "$185,000" in out
    assert "Remote (US)" in out


def test_fetch_job_description_auth_wall_still_rejected():
    """Extracted text containing an _AUTH_WALL_SIGNATURES token returns ""."""
    html = (
        "<html><body><main>"
        "<p>Access denied. You must be signed in to a verified corporate account "
        "before you can view this internal job posting or any of its details. "
        "Please contact your administrator to request the appropriate access.</p>"
        "</main></body></html>"
    )
    resp = _mock_response(html)

    with patch("jobcannon.engine.http_fetch.requests.get", return_value=resp):
        out = _fetch_job_description("https://example.com/jobs/3")

    assert out == ""


def test_fetch_job_description_empty_html_returns_empty_string():
    """Empty / whitespace-only fetch returns "" (never None)."""
    resp = _mock_response("   \n\t  ")

    with patch("jobcannon.engine.http_fetch.requests.get", return_value=resp):
        out = _fetch_job_description("https://example.com/jobs/4")

    assert out == ""
    assert out is not None
