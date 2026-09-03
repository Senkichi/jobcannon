"""Unit tests for the pure direct-link resolution helpers."""

from __future__ import annotations

import json

from jobcannon.engine.direct_link import (
    apply_targets,
    apply_url_for,
    careers_host_of,
    is_ats_or_careers_url,
    is_canonical_apply_url,
    pick_direct_link,
    promote_existing_direct_url,
    resolve_direct_link,
    resolve_primary_posting,
)


def test_is_ats_url_recognizes_known_platforms():
    assert is_ats_or_careers_url("https://boards.greenhouse.io/acme/jobs/1")
    assert is_ats_or_careers_url("https://jobs.lever.co/acme/abc-123")
    assert is_ats_or_careers_url("https://jobs.ashbyhq.com/acme/xyz")
    assert is_ats_or_careers_url("https://acme.wd5.myworkdayjobs.com/ext/job/1")
    assert is_ats_or_careers_url("https://careers.smartrecruiters.com/Acme/123")


def test_is_ats_url_rejects_aggregators():
    assert not is_ats_or_careers_url("https://www.linkedin.com/jobs/view/123")
    assert not is_ats_or_careers_url("https://www.glassdoor.com/job/abc")
    assert not is_ats_or_careers_url("https://jooble.org/jdp/123")
    assert not is_ats_or_careers_url("")
    assert not is_ats_or_careers_url(None)


def test_is_ats_url_recognizes_personio_subdomains():
    """The 'jobs.personio.' marker splits into explicit .de/.com host suffixes."""
    assert is_ats_or_careers_url("https://acme.jobs.personio.de/job/123")
    assert is_ats_or_careers_url("https://acme.jobs.personio.com/job/123")


def test_is_ats_url_rejects_lookalike_hosts():
    """A host that merely EMBEDS an ATS domain as a substring — not a real
    subdomain of it — must NOT be classified as a trusted ATS/company link.
    This gates the Apply-button URL (apply_url_for), so a false positive here
    would surface an attacker-controlled URL as a verified company posting."""
    assert not is_ats_or_careers_url("https://greenhouse.io.evil.example/apply")
    assert not is_ats_or_careers_url("https://notgreenhouse.io/apply")
    assert not is_ats_or_careers_url("https://myworkable.com.evil.example/")
    assert not is_ats_or_careers_url("https://jobvite.com.evil.example/apply")


def test_is_ats_url_userinfo_spoof_and_case_insensitive():
    """Pin the two spoof/normalization vectors on this Apply-button-gating check
    that the look-alike suite above does not cover.

    userinfo-spoof: an attacker embeds a trusted ATS domain in the URL's
    ``user:pass@`` userinfo so the *real* host is theirs. On the pre-fix
    netloc-substring code this returned True (the marker appears in netloc);
    urlparse().hostname strips userinfo to the real host ("evil.example"), so it
    must return False. This assert is the regression guard against any future
    edit reverting to netloc-based matching on the codebase's single
    highest-stakes trust check.

    case-insensitivity: an uppercase host must still match — guards against a
    dropped ``.lower()`` normalization.
    """
    assert not is_ats_or_careers_url("https://greenhouse.io@evil.example/apply")
    assert is_ats_or_careers_url("https://boards.GREENHOUSE.io/acme/1")


# ── careers_host_of (companies.careers_url -> host marker) ─────────────────────


def test_careers_host_of_strips_www_prefix():
    """A stored careers_url with a www. prefix marks both www and apex hosts."""
    assert careers_host_of("https://www.metacareers.com/jobs") == "metacareers.com"


def test_careers_host_of_keeps_non_www_subdomain():
    """A careers subdomain host is kept verbatim (only a leading www. is stripped)."""
    assert careers_host_of("https://careers.acme.com/openings") == "careers.acme.com"


def test_careers_host_of_returns_none_for_empty_or_unparseable():
    assert careers_host_of(None) is None
    assert careers_host_of("") is None
    # scheme-less / non-http(s) values carry no usable host marker
    assert careers_host_of("acme.com/careers") is None
    assert careers_host_of("ftp://acme.com/careers") is None


# ── is_canonical_apply_url (display-preference: host OR company careers domain) ─


def test_canonical_apply_url_includes_all_ats_hosts():
    """Everything is_ats_or_careers_url accepts is also canonical, with or without
    a company_careers_host."""
    assert is_canonical_apply_url("https://jobs.ashbyhq.com/acme/xyz")
    assert is_canonical_apply_url("https://boards.greenhouse.io/acme/jobs/1")
    assert is_canonical_apply_url("https://jobs.lever.co/acme/abc-123")


def test_canonical_apply_url_recognizes_company_careers_host():
    """An employer's OWN careers domain is canonical even though its host is NOT a
    multi-tenant ATS (the Handshake incident shape: joinhandshake.com is Handshake's
    own careers domain, and it is this job's company_careers_host)."""
    url = "https://joinhandshake.com/careers/job/?ashby_jid=c5753428-968f-4142-9dbc-49d419f0a020"
    assert is_canonical_apply_url(url, company_careers_host="joinhandshake.com")
    # ...and the strict host allowlist (which gates auto-submit) still does NOT
    # accept it, proving the broadening is display-only.
    assert not is_ats_or_careers_url(url)


def test_canonical_apply_url_matches_careers_host_subdomain():
    """A posting on a subdomain of the company careers host is canonical."""
    assert is_canonical_apply_url(
        "https://careers.acme.com/job/1", company_careers_host="acme.com"
    )
    assert is_canonical_apply_url("https://acme.com/job/1", company_careers_host="acme.com")


def test_canonical_apply_url_careers_host_anti_spoof():
    """The company_careers_host is matched on a LABEL boundary (exact or true
    subdomain), never a substring — so a look-alike host, a userinfo spoof, or the
    domain embedded in a path/query is never mistaken for the employer's own page."""
    # substring look-alikes: not a real subdomain of acme.com
    assert not is_canonical_apply_url(
        "https://acme.com.evil.example/apply", company_careers_host="acme.com"
    )
    assert not is_canonical_apply_url("https://notacme.com/apply", company_careers_host="acme.com")
    # careers host appears only in the query, real host is the aggregator
    assert not is_canonical_apply_url(
        "https://aggregator.example/go?dest=https://acme.com/x", company_careers_host="acme.com"
    )
    # userinfo spoof: real host is evil.example
    assert not is_canonical_apply_url(
        "https://acme.com@evil.example/apply", company_careers_host="acme.com"
    )
    # non-http(s) scheme
    assert not is_canonical_apply_url("ftp://acme.com/apply", company_careers_host="acme.com")


def test_canonical_apply_url_without_careers_host_falls_back_to_ats_only():
    """With no company_careers_host, only recognized ATS/careers HOSTS are canonical —
    a bare employer careers domain is NOT (the param is what generalizes it)."""
    url = "https://joinhandshake.com/careers/job/?ashby_jid=c5753428"
    assert not is_canonical_apply_url(url)  # company_careers_host defaults to None
    assert not is_canonical_apply_url(url, company_careers_host=None)


def test_canonical_apply_url_rejects_aggregators():
    assert not is_canonical_apply_url("https://www.linkedin.com/jobs/view/123")
    assert not is_canonical_apply_url("https://jooble.org/jdp/123")
    assert not is_canonical_apply_url("https://www.adzuna.com/details/2")
    assert not is_canonical_apply_url("")
    assert not is_canonical_apply_url(None)
    # even with a careers host set, an unrelated aggregator host is not canonical
    assert not is_canonical_apply_url(
        "https://www.linkedin.com/jobs/view/123", company_careers_host="acme.com"
    )


def test_promote_returns_first_ats_url():
    urls = [
        "https://www.linkedin.com/jobs/view/123",
        "https://jobs.lever.co/acme/abc-123",
        "https://boards.greenhouse.io/acme/jobs/1",
    ]
    assert promote_existing_direct_url(urls) == "https://jobs.lever.co/acme/abc-123"


def test_promote_returns_none_when_only_aggregators():
    urls = ["https://www.linkedin.com/jobs/view/123", "https://jooble.org/x"]
    assert promote_existing_direct_url(urls) is None
    assert promote_existing_direct_url([]) is None


def _posting(title, url=None, src=None):
    p = {"title": title}
    if url is not None:
        p["url"] = url
    if src is not None:
        p["source_url"] = src
    return p


def test_resolve_strict_unique_exact_title():
    postings = [
        _posting("Senior Data Scientist", src="https://jobs.lever.co/acme/1"),
        _posting("Product Manager", src="https://jobs.lever.co/acme/2"),
    ]
    assert resolve_direct_link(postings, "Senior Data Scientist") == (
        "https://jobs.lever.co/acme/1",
        "strict",
    )


def test_resolve_strict_uses_abbreviation_expansion():
    postings = [_posting("Sr DS", src="https://jobs.lever.co/acme/1")]
    assert resolve_direct_link(postings, "Senior Data Scientist") == (
        "https://jobs.lever.co/acme/1",
        "strict",
    )


def test_resolve_ambiguous_exact_title_falls_back_to_loose():
    postings = [
        _posting("Data Scientist", src="https://jobs.lever.co/acme/1"),
        _posting("Data Scientist", src="https://jobs.lever.co/acme/2"),
    ]
    assert resolve_direct_link(postings, "Data Scientist") == (
        "https://jobs.lever.co/acme/1",
        "loose",
    )


def test_resolve_returns_none_when_no_exact_match():
    """No exact-title match → None, NOT the first posting's URL (#1932).

    Previously this returned (first_posting_url, "loose"), which stamped one
    stale link across every unmatched job in a company's batch. A job whose
    title doesn't match any board posting has no plausible direct link; the
    LLM tie-breaker in the resolver can still recover a semantic match.
    """
    postings = [_posting("Staff Data Scientist", src="https://jobs.lever.co/acme/9")]
    assert resolve_direct_link(postings, "Data Scientist") is None


def test_resolve_reads_careers_url_key():
    postings = [_posting("Data Scientist", url="https://acme.com/careers/1")]
    assert resolve_direct_link(postings, "Data Scientist") == (
        "https://acme.com/careers/1",
        "strict",
    )


def test_resolve_skips_posting_without_link():
    postings = [_posting("Data Scientist")]  # no url, no source_url
    assert resolve_direct_link(postings, "Data Scientist") is None
    assert resolve_direct_link([], "Data Scientist") is None


def test_pick_prefers_existing_ats_source_url_strict():
    cand = pick_direct_link(
        source_urls=["https://boards.greenhouse.io/acme/jobs/1"],
        ats_result={
            "direct_url": "https://jobs.lever.co/acme/2",
            "direct_url_confidence": "loose",
        },
        careers_result={},
    )
    assert cand == ("https://boards.greenhouse.io/acme/jobs/1", "strict")


def test_pick_uses_ats_result_when_no_promotion():
    cand = pick_direct_link(
        source_urls=["https://www.linkedin.com/jobs/view/1"],
        ats_result={
            "direct_url": "https://jobs.lever.co/acme/2",
            "direct_url_confidence": "strict",
        },
        careers_result={
            "direct_url": "https://acme.com/careers/9",
            "direct_url_confidence": "strict",
        },
    )
    assert cand == ("https://jobs.lever.co/acme/2", "strict")


def test_pick_falls_back_to_careers():
    cand = pick_direct_link(
        source_urls=["https://www.linkedin.com/jobs/view/1"],
        ats_result={},
        careers_result={
            "direct_url": "https://acme.com/careers/9",
            "direct_url_confidence": "loose",
        },
    )
    assert cand == ("https://acme.com/careers/9", "loose")


def test_pick_returns_none_when_nothing_resolves():
    assert pick_direct_link(["https://www.linkedin.com/jobs/view/1"], {}, {}) is None
    assert pick_direct_link([], {}, {}) is None


# ── resolve_primary_posting (strict-gated data merge) ─────────────────────────


def test_primary_posting_strict_returns_matched_posting():
    postings = [
        _posting("Senior Data Scientist", src="https://jobs.lever.co/acme/1"),
        _posting("Product Manager", src="https://jobs.lever.co/acme/2"),
    ]
    posting, url, confidence = resolve_primary_posting(postings, "Senior Data Scientist")
    assert posting is postings[0]
    assert url == "https://jobs.lever.co/acme/1"
    assert confidence == "strict"


def test_primary_posting_ambiguous_returns_no_posting():
    """Contamination guard: ambiguous title match must not expose a posting."""
    postings = [
        _posting("Data Scientist", src="https://jobs.lever.co/acme/1"),
        _posting("Data Scientist", src="https://jobs.lever.co/acme/2"),
    ]
    posting, url, confidence = resolve_primary_posting(postings, "Data Scientist")
    assert posting is None
    assert url == "https://jobs.lever.co/acme/1"
    assert confidence == "loose"


def test_primary_posting_no_exact_match_returns_none():
    """No exact-title match → None (not a loose first-posting fallback, #1932).

    The old behavior returned (None, first_posting_url, "loose"), stamping one
    stale URL across every unmatched job in a batch. Now the heuristic returns
    None; the resolver's LLM tie-breaker can still recover a semantic match.
    """
    postings = [_posting("Staff Data Scientist", src="https://jobs.lever.co/acme/9")]
    assert resolve_primary_posting(postings, "Data Scientist") is None


def test_primary_posting_location_disambiguates_multi_location_board():
    """Same title in N locations: the job's location picks the strict match."""
    nyc = dict(_posting("Data Scientist", src="https://jobs.lever.co/acme/1"), location="New York")
    lon = dict(
        _posting("Data Scientist", src="https://jobs.lever.co/acme/2"), location="London, UK"
    )
    posting, url, confidence = resolve_primary_posting(
        [nyc, lon], "Data Scientist", "New York, NY"
    )
    assert posting is nyc
    assert url == "https://jobs.lever.co/acme/1"
    assert confidence == "strict"


def test_primary_posting_location_still_ambiguous_stays_loose():
    """Two postings sharing the job's location token: no strict promotion."""
    a = dict(_posting("Data Scientist", src="https://jobs.lever.co/acme/1"), location="Remote, US")
    b = dict(_posting("Data Scientist", src="https://jobs.lever.co/acme/2"), location="Remote, EU")
    posting, _url, confidence = resolve_primary_posting([a, b], "Data Scientist", "Remote")
    assert posting is None
    assert confidence == "loose"


def test_primary_posting_no_job_location_stays_loose():
    a = dict(_posting("Data Scientist", src="https://jobs.lever.co/acme/1"), location="New York")
    b = dict(_posting("Data Scientist", src="https://jobs.lever.co/acme/2"), location="London")
    posting, _url, confidence = resolve_primary_posting([a, b], "Data Scientist", "")
    assert posting is None
    assert confidence == "loose"


def test_primary_posting_none_when_no_links():
    assert resolve_primary_posting([_posting("Data Scientist")], "Data Scientist") is None
    assert resolve_primary_posting([], "Data Scientist") is None


def test_no_exact_match_batch_yields_no_shared_url():
    """Regression (#1932): N distinct job titles against a board with no
    exact-title match must each get None — NOT the same first-posting URL.

    Before the fix, resolve_primary_posting returned (None, linked[0][1],
    "loose") for every no-exact-match job, stamping one stale URL across
    the whole batch. This test would have failed on the old code: all six
    jobs would have received "https://jobs.lever.co/acme/sw1".
    """
    postings = [
        _posting("Software Engineer", src="https://jobs.lever.co/acme/sw1"),
        _posting("Product Manager", src="https://jobs.lever.co/acme/pm1"),
    ]
    titles = [
        "Financial Analyst",
        "Payroll Specialist",
        "Recruitment Coordinator",
        "Branch Manager",
        "Account Executive",
        "Operations Lead",
    ]
    results = [resolve_primary_posting(postings, t) for t in titles]
    # Every result must be None — no stale URL stamped on any job.
    assert all(r is None for r in results), f"Expected all None, got {results}"


# ── apply_url_for (Apply-button precedence) ───────────────────────────────────

_AGG = '["https://www.linkedin.com/jobs/view/1", "https://jooble.org/x"]'


def test_apply_strict_direct_url_wins():
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "strict",
        "source_urls": _AGG,
    }
    assert apply_url_for(job) == "https://jobs.lever.co/acme/1"


def test_apply_loose_direct_url_now_wins():
    """User decision (prefer-canonical-over-aggregator, the consistent default):
    a loose direct_url now ALWAYS wins over an aggregator sighting. direct_url is
    canonical by construction (never an aggregator); a loose title match may point
    at a nearby role at the SAME employer, an accepted trade for a clean apply page.
    (Previously this fell back to the aggregator unless a loose_apply_default flag
    was set — that flag no longer exists on apply_url_for.)"""
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "loose",
        "source_urls": _AGG,
    }
    assert apply_url_for(job) == "https://jobs.lever.co/acme/1"


def test_apply_no_direct_url_uses_first_source_url():
    assert apply_url_for({"source_urls": _AGG}) == "https://www.linkedin.com/jobs/view/1"


def test_apply_accepts_parsed_list_and_missing_keys():
    assert apply_url_for({"source_urls": ["https://a.example/1"]}) == "https://a.example/1"
    assert apply_url_for({}) is None
    assert apply_url_for({"source_urls": None}) is None
    assert apply_url_for({"source_urls": "not-json"}) is None


def test_apply_direct_url_without_confidence_is_ignored():
    """A direct_url with no confidence (neither 'strict' nor 'loose') is NOT the
    resolved company posting — it is ignored and the fallback runs."""
    job = {"direct_url": "https://jobs.lever.co/acme/1", "source_urls": _AGG}
    assert apply_url_for(job) == "https://www.linkedin.com/jobs/view/1"


# ── apply_url_for allow_loose_direct_url (browser-extension /match disambiguation) ─


def test_apply_url_for_disallow_loose_direct_url_falls_back_to_source():
    """The /match endpoint passes allow_loose_direct_url=False: a loose direct_url is
    SHARED across sibling jobs at a company, so it must be excluded from URL->job
    matching. Excluded, resolution falls back to the row's own source_urls. The
    display default (True) keeps the loose direct_url as the Apply target."""
    job = {
        "direct_url": "https://jobs.lever.co/acme/shared",
        "direct_url_confidence": "loose",
        "source_urls": _AGG,
    }
    assert apply_url_for(job) == "https://jobs.lever.co/acme/shared"  # display default
    assert (
        apply_url_for(job, allow_loose_direct_url=False) == "https://www.linkedin.com/jobs/view/1"
    )


def test_apply_url_for_disallow_loose_still_prefers_canonical_source():
    """With the loose direct_url excluded, the source_urls fallback still prefers a
    canonical link over positional order (a real ATS posting beats the aggregator)."""
    job = {
        "direct_url": "https://jobs.lever.co/acme/shared",
        "direct_url_confidence": "loose",
        "source_urls": json.dumps(
            ["https://jooble.org/x", "https://boards.greenhouse.io/acme/jobs/9"]
        ),
    }
    assert (
        apply_url_for(job, allow_loose_direct_url=False)
        == "https://boards.greenhouse.io/acme/jobs/9"
    )


def test_apply_url_for_disallow_loose_strict_direct_url_still_wins():
    """allow_loose_direct_url=False excludes only LOOSE direct_urls — a strict
    direct_url is the verified posting and still wins outright."""
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "strict",
        "source_urls": _AGG,
    }
    assert apply_url_for(job, allow_loose_direct_url=False) == "https://jobs.lever.co/acme/1"


# ── apply_url_for staleness fallback (Phase 5) ────────────────────────────────


def test_apply_expired_strict_direct_url_falls_back_to_aggregator():
    """An expired job's primary posting is dead — skip direct_url even when the
    column still holds a strict link (the window before the reconciler NULLs it)
    and send the user to the aggregator listing instead."""
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "strict",
        "expiry_status": "expired",
        "source_urls": _AGG,
    }
    assert apply_url_for(job) == "https://www.linkedin.com/jobs/view/1"


def test_apply_expired_loose_direct_url_also_falls_back():
    """The staleness guard is confidence-agnostic: an expired LOOSE direct_url is
    skipped too (regression guard now that loose wins on the happy path)."""
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "loose",
        "expiry_status": "expired",
        "source_urls": _AGG,
    }
    assert apply_url_for(job) == "https://www.linkedin.com/jobs/view/1"


def test_apply_expired_direct_url_with_no_source_urls_returns_none():
    """Expired direct_url and no aggregator fallback → no Apply target at all
    (better than a guaranteed 404)."""
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "strict",
        "expiry_status": "expired",
        "source_urls": "[]",
    }
    assert apply_url_for(job) is None


def test_apply_live_strict_direct_url_still_wins():
    """Non-expired strict link is unaffected by the staleness guard (regression
    on the happy path)."""
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "strict",
        "expiry_status": "live",
        "source_urls": _AGG,
    }
    assert apply_url_for(job) == "https://jobs.lever.co/acme/1"


def test_apply_url_for_fallback_prefers_ats_url_over_positional_first():
    """No direct_url set; source_urls has an aggregator link first and a real
    ATS link second. The Apply button must not point at the known-doomed
    aggregator redirect when a real posting link is sitting right there."""
    job = {
        "direct_url": None,
        "direct_url_confidence": None,
        "expiry_status": None,
        "source_urls": json.dumps(
            [
                "https://jooble.org/away/12345",
                "https://jobs.lever.co/acme/870c805e",
            ]
        ),
    }
    assert apply_url_for(job) == "https://jobs.lever.co/acme/870c805e"


def test_apply_url_for_fallback_positional_when_no_ats_url_present():
    """No ATS/careers URL anywhere in source_urls — falls back to the first
    entry, unchanged from today's behavior."""
    job = {
        "direct_url": None,
        "direct_url_confidence": None,
        "expiry_status": None,
        "source_urls": json.dumps(
            ["https://jooble.org/away/1", "https://www.adzuna.com/details/2"]
        ),
    }
    assert apply_url_for(job) == "https://jooble.org/away/1"


def test_apply_url_for_fallback_prefers_careers_host_over_aggregator():
    """The Handshake incident, generalized: no strict direct_url; source_urls is an
    aggregator/SERP link first and the employer's OWN careers page second. With the
    job's company_careers_host supplied, the Apply button picks the canonical
    employer page, not the aggregator redirect (whose odd-referrer arrival is what
    nudges an ATS spam-protection score down)."""
    handshake = (
        "https://joinhandshake.com/careers/job/?ashby_jid=c5753428-968f-4142-9dbc-49d419f0a020"
    )
    job = {
        "direct_url": None,
        "direct_url_confidence": None,
        "expiry_status": None,
        "source_urls": json.dumps(
            [
                "https://www.google.com/search?q=handshake+data+scientist",
                handshake,
            ]
        ),
    }
    assert apply_url_for(job, company_careers_host="joinhandshake.com") == handshake


def test_apply_url_for_fallback_careers_host_needs_the_param():
    """Without company_careers_host, the employer-careers-domain link is NOT
    recognized as canonical, so the fallback keeps its positional (aggregator)
    first entry — proving the param is exactly what enables the generalization."""
    handshake = "https://joinhandshake.com/careers/job/?ashby_jid=c5753428"
    job = {
        "direct_url": None,
        "direct_url_confidence": None,
        "expiry_status": None,
        "source_urls": json.dumps(
            [
                "https://www.google.com/search?q=handshake+data+scientist",
                handshake,
            ]
        ),
    }
    assert apply_url_for(job) == "https://www.google.com/search?q=handshake+data+scientist"


def test_apply_url_for_fallback_ats_host_beats_careers_host():
    """A true multi-tenant ATS host and an employer-careers-domain link are both
    canonical; canonical preference iterates in source order, so the first-listed
    (ATS host, matching promotion precedence) wins."""
    job = {
        "direct_url": None,
        "direct_url_confidence": None,
        "expiry_status": None,
        "source_urls": json.dumps(
            [
                "https://jobs.ashbyhq.com/handshake/c5753428",
                "https://joinhandshake.com/careers/job/?ashby_jid=c5753428",
            ]
        ),
    }
    assert (
        apply_url_for(job, company_careers_host="joinhandshake.com")
        == "https://jobs.ashbyhq.com/handshake/c5753428"
    )


def test_apply_url_for_fallback_ats_preference_yields_to_strict_direct_url():
    """A strict direct_url still wins outright — the canonical-preferring fallback
    only applies once the function has already fallen through to source_urls."""
    job = {
        "direct_url": "https://boards.greenhouse.io/acme/jobs/1",
        "direct_url_confidence": "strict",
        "expiry_status": None,
        "source_urls": json.dumps(["https://jooble.org/away/1"]),
    }
    assert apply_url_for(job) == "https://boards.greenhouse.io/acme/jobs/1"


# ── apply_targets (per-posting apply links) ───────────────────────────────────


def test_apply_targets_enumerates_one_per_posting():
    """A 2-descriptor postings list returns 2 targets, order preserved."""
    job = {
        "postings": [
            {
                "source_id": "ashby_sf",
                "apply_url": "https://jobs.ashbyhq.com/brigit/1",
                "locations_structured": [{"city": "San Francisco", "region_code": "CA"}],
                "workplace_type": "HYBRID",
                "location_fit": 4,
            },
            {
                "source_id": "ashby_nyc",
                "apply_url": "https://jobs.ashbyhq.com/brigit/2",
                "locations_structured": [{"city": "New York", "region_code": "NY"}],
                "workplace_type": "HYBRID",
                "location_fit": 3,
            },
        ],
    }
    targets = apply_targets(job)
    assert len(targets) == 2
    assert targets[0]["apply_url"] == "https://jobs.ashbyhq.com/brigit/1"
    assert targets[0]["label"] == "San Francisco, CA (Hybrid)"
    assert targets[0]["location_fit"] == 4
    assert targets[1]["apply_url"] == "https://jobs.ashbyhq.com/brigit/2"
    assert targets[1]["label"] == "New York, NY (Hybrid)"
    assert targets[1]["location_fit"] == 3


def test_apply_targets_falls_back_to_apply_url_for_when_postings_empty():
    """Empty/missing postings falls back to apply_url_for behavior."""
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "strict",
        "source_urls": _AGG,
    }
    targets = apply_targets(job)
    assert len(targets) == 1
    assert targets[0]["apply_url"] == "https://jobs.lever.co/acme/1"
    assert targets[0]["label"] == "Apply"


def test_apply_targets_empty_when_no_link_anywhere():
    """No postings and no source_urls returns empty list."""
    job = {}
    targets = apply_targets(job)
    assert targets == []


def test_apply_targets_parses_json_string_postings():
    """postings as JSON string yields same result as parsed list."""
    job_json = {
        "postings": '[{"apply_url": "https://jobs.ashbyhq.com/brigit/1", "locations_structured": [{"city": "San Francisco"}], "workplace_type": "HYBRID"}]',
    }
    job_list = {
        "postings": [
            {
                "apply_url": "https://jobs.ashbyhq.com/brigit/1",
                "locations_structured": [{"city": "San Francisco"}],
                "workplace_type": "HYBRID",
            }
        ],
    }
    targets_json = apply_targets(job_json)
    targets_list = apply_targets(job_list)
    assert len(targets_json) == len(targets_list) == 1
    assert targets_json[0]["apply_url"] == targets_list[0]["apply_url"]


def test_apply_targets_malformed_postings_degrades_to_fallback():
    """Non-JSON postings degrades to apply_url_for fallback."""
    job = {
        "postings": "not-valid-json",
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "strict",
    }
    targets = apply_targets(job)
    assert len(targets) == 1
    assert targets[0]["apply_url"] == "https://jobs.lever.co/acme/1"


def test_apply_targets_loose_only_wins_when_config_set():
    """A descriptor whose only link is non-canonical (aggregator) is emitted only
    with loose_apply_default=True. With loose_apply_default=False, the per-posting
    aggregator link is skipped and apply_targets falls back to apply_url_for so the
    user still gets an Apply button when the row carries any resolvable URL."""
    job = {
        "postings": [
            {
                "apply_url": "https://www.linkedin.com/jobs/view/1",  # aggregator (loose)
                "locations_structured": [{"city": "Remote"}],
            },
        ],
        "source_urls": ["https://www.glassdoor.com/job/abc"],
    }
    # Without loose_apply_default, the aggregator posting is skipped and we fall
    # back to the first source_url (labelled generically as "Apply").
    targets = apply_targets(job, loose_apply_default=False)
    assert len(targets) == 1
    assert targets[0]["apply_url"] == "https://www.glassdoor.com/job/abc"
    assert targets[0]["label"] == "Apply"
    assert targets[0]["location_fit_color"] is None

    # With loose_apply_default, the per-posting aggregator link is emitted directly.
    targets = apply_targets(job, loose_apply_default=True)
    assert len(targets) == 1
    assert targets[0]["apply_url"] == "https://www.linkedin.com/jobs/view/1"
    assert targets[0]["label"] == "Remote"


def test_apply_targets_falls_back_when_all_postings_are_loose():
    """When every postings descriptor is non-canonical and loose_apply_default is
    False, apply_targets falls back to apply_url_for so a strict direct_url (or any
    resolvable URL) still surfaces as an Apply button."""
    job = {
        "postings": [
            {
                "apply_url": "https://www.linkedin.com/jobs/view/1",
                "locations_structured": [{"city": "Remote"}],
            },
        ],
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "strict",
        "source_urls": ["https://www.glassdoor.com/job/abc"],
    }
    targets = apply_targets(job, loose_apply_default=False)
    assert len(targets) == 1
    # apply_url_for's precedence: strict direct_url beats any aggregator fallback.
    assert targets[0]["apply_url"] == "https://jobs.lever.co/acme/1"
    assert targets[0]["label"] == "Apply"
    assert targets[0]["location_fit_color"] is None


def test_apply_targets_treats_careers_host_link_as_canonical():
    """Consistency with apply_url_for: a descriptor whose apply_url is on the job's
    OWN company careers domain is canonical, so it is surfaced WITHOUT
    loose_apply_default — not skipped as a loose aggregator link."""
    job = {
        "postings": [
            {
                "apply_url": "https://joinhandshake.com/careers/job/?ashby_jid=c5753428",
                "locations_structured": [{"city": "San Francisco"}],
            },
        ],
    }
    targets = apply_targets(
        job, loose_apply_default=False, company_careers_host="joinhandshake.com"
    )
    assert len(targets) == 1
    assert targets[0]["apply_url"] == "https://joinhandshake.com/careers/job/?ashby_jid=c5753428"


def test_apply_targets_careers_host_link_skipped_without_param():
    """The same employer-careers-domain descriptor link is treated as loose (and
    skipped without loose_apply_default) when no company_careers_host is supplied —
    the careers-host recognition depends on the param."""
    job = {
        "postings": [
            {
                "apply_url": "https://joinhandshake.com/careers/job/?ashby_jid=c5753428",
                "locations_structured": [{"city": "San Francisco"}],
            },
        ],
    }
    assert apply_targets(job, loose_apply_default=False) == []


def test_apply_targets_skips_descriptor_without_apply_url():
    """A descriptor with empty apply_url is omitted; other descriptors still returned."""
    job = {
        "postings": [
            {
                "apply_url": "",
                "locations_structured": [{"city": "New York"}],
            },
            {
                "apply_url": "https://jobs.ashbyhq.com/brigit/1",
                "locations_structured": [{"city": "San Francisco"}],
            },
        ],
    }
    targets = apply_targets(job)
    assert len(targets) == 1
    assert targets[0]["apply_url"] == "https://jobs.ashbyhq.com/brigit/1"


def test_apply_targets_passes_through_none_location_fit():
    """A P1-shaped descriptor with missing location_fit yields location_fit=None."""
    job = {
        "postings": [
            {
                "apply_url": "https://jobs.ashbyhq.com/brigit/1",
                "locations_structured": [{"city": "San Francisco"}],
                # location_fit missing (P1 output)
            },
        ],
    }
    targets = apply_targets(job)
    assert len(targets) == 1
    assert targets[0]["location_fit"] is None


# ── apply_targets location_fit_color surfacing (issue #1215) ──────────────────


def test_apply_targets_surfaces_location_fit_color_when_present():
    """A posting enriched by the #1214 write path
    (location_policy.apply_location_policy_to_postings) carries a policy-driven
    location_fit_color; apply_targets must pass it through unchanged so the
    template can use it as the single source of truth for badge color instead
    of re-deriving one from the old fixed-scale location_fit thresholds."""
    job = {
        "postings": [
            {
                "apply_url": "https://jobs.ashbyhq.com/brigit/1",
                "locations_structured": [{"city": "San Francisco"}],
                # rank 2: eligible on-site posting at a primary-tier city — under
                # the retired >=4/==3 threshold ladder this would read red,
                # indistinguishable from ineligible (the bug issue #1215 fixes).
                "location_fit": 2,
                "location_fit_color": "bg-emerald-500",
            },
        ],
    }
    targets = apply_targets(job)
    assert len(targets) == 1
    assert targets[0]["location_fit"] == 2
    assert targets[0]["location_fit_color"] == "bg-emerald-500"


def test_apply_targets_sets_location_fit_color_none_for_legacy_posting():
    """A posting written before #1214 ever ran the location-policy engine over
    it has no location_fit_color key at all. apply_targets must ALWAYS set the
    key on the returned target (explicitly None) — the template must never see
    a Jinja Undefined and falls back to legacy threshold coloring in that case."""
    job = {
        "postings": [
            {
                "apply_url": "https://jobs.ashbyhq.com/brigit/1",
                "locations_structured": [{"city": "San Francisco"}],
                "location_fit": 5,
                # location_fit_color absent (legacy posting, pre-#1214)
            },
        ],
    }
    targets = apply_targets(job)
    assert len(targets) == 1
    assert "location_fit_color" in targets[0]
    assert targets[0]["location_fit_color"] is None


def test_apply_targets_fallback_path_sets_location_fit_color_none():
    """The single-posting fallback path (no postings column, apply_url_for-driven)
    has no per-posting color source at all (#1214 only writes location_fit_color
    onto postings-list descriptors, never a row-level column). location_fit_color
    must still be explicitly present and None so the key is never Undefined."""
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "strict",
        "source_urls": _AGG,
        "location_fit": 5,
    }
    targets = apply_targets(job)
    assert len(targets) == 1
    assert "location_fit_color" in targets[0]
    assert targets[0]["location_fit_color"] is None
