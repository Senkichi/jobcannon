"""Tests for Phase 49.01 — URL canonicalization.

Ported from the private repo's tests/test_url_canonical.py. The m080
migration tests (schema migration infra — job_finder.web.migrations) are NOT
ported — migrations are outside this task's manifest.
"""

from __future__ import annotations

from jobcannon.engine.models import Job
from jobcannon.engine.parsed_job import ParsedJob
from jobcannon.engine.url_canonical import canonicalize_url

# ---------------------------------------------------------------------------
# canonicalize_url
# ---------------------------------------------------------------------------


def test_strips_utm_tracking_param():
    canonical, raw = canonicalize_url("https://example.com/job?utm_source=foo&id=42")
    assert canonical == "https://example.com/job?id=42"
    assert raw == "https://example.com/job?utm_source=foo&id=42"


def test_strips_wildcard_mc_family():
    canonical, _ = canonicalize_url("https://example.com/job?mc_cid=abc&id=42")
    assert canonical == "https://example.com/job?id=42"


def test_strips_exact_allowlist_keys():
    # fbclid, refId, trk, lipi, ref, _hsenc, _hsmi are stripped. gh_jid is NOT a
    # tracking param — it is Greenhouse's posting id and is deliberately preserved.
    url = "https://example.com/x?gh_jid=1&fbclid=2&refId=3&trk=4&lipi=5&ref=6&_hsenc=7&_hsmi=8&keep=ok"
    canonical, _ = canonicalize_url(url)
    assert canonical == "https://example.com/x?gh_jid=1&keep=ok"


def test_preserves_gh_jid_posting_identifier():
    """gh_jid is Greenhouse's stable posting id (self-hosted board redirects), not
    tracking noise — canonicalization must keep it so the ATS layer can extract it."""
    canonical, _ = canonicalize_url(
        "https://careers.airbnb.com/positions/7662244?gh_jid=7662244&utm_source=foo"
    )
    assert canonical == "https://careers.airbnb.com/positions/7662244?gh_jid=7662244"


def test_query_order_normalization_is_stable():
    a, _ = canonicalize_url("https://example.com/job?b=2&a=1")
    b, _ = canonicalize_url("https://example.com/job?a=1&b=2")
    assert a == b == "https://example.com/job?a=1&b=2"


def test_lowercases_scheme_and_host_preserves_path():
    canonical, _ = canonicalize_url("HTTPS://Example.COM/Jobs/View/42?utm_term=x")
    assert canonical == "https://example.com/Jobs/View/42"


def test_unparseable_returns_raw_without_raising():
    bad = "http://[::1"  # malformed IPv6 host → urlsplit raises ValueError
    canonical, raw = canonicalize_url(bad)
    assert canonical == bad
    assert raw == bad


def test_empty_string_roundtrips():
    assert canonicalize_url("") == ("", "")


# ---------------------------------------------------------------------------
# ParsedJob integration — canonicalization at construction
# ---------------------------------------------------------------------------


def test_parsed_job_canonicalizes_source_urls_and_preserves_raw():
    job = Job(
        title="Data Scientist",
        company="Acme",
        location="Remote",
        source="greenhouse",
        source_url="https://acme.com/jobs/1?utm_source=foo&id=1",
    )
    parsed = ParsedJob.from_job(
        job,
        source_meta={
            "source_urls": [
                "https://acme.com/jobs/1?utm_source=foo&id=1",
                "https://Boards.Greenhouse.io/acme/jobs/2?gh_jid=9&b=2&a=1",
            ],
        },
    )
    assert parsed.source_urls == [
        "https://acme.com/jobs/1?id=1",
        # gh_jid is preserved (Greenhouse posting id, not tracking noise); utm_* dropped,
        # remaining params sorted alphabetically (a, b, gh_jid).
        "https://boards.greenhouse.io/acme/jobs/2?a=1&b=2&gh_jid=9",
    ]
    assert parsed.source_urls_raw == [
        "https://acme.com/jobs/1?utm_source=foo&id=1",
        "https://Boards.Greenhouse.io/acme/jobs/2?gh_jid=9&b=2&a=1",
    ]
