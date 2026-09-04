# PORTED from tests/test_jobright_parser.py @ a7f0f38a85dfa0af4d305c04da833785f723d649 (private job-cannon). Ledger L-0555.
"""Tests for the JobRight job-match alert parser (noreply@jobright.ai).

Grounded on a REAL captured alert (``tests/fixtures/emails/jobright.eml``, a
sanitized "Jobright Job Alert" digest of 6 AI-matched roles). The fixture-backed
class is the source of truth; the structure/edge/regression classes use small
HTML snippets that mirror the real markup (each card is one outer
``<a href=".../jobs/info/<id>">`` wrapping a bold ``<p>`` company, a muted
industry·stage line, a ``NN %`` match span, an inner title ``<a>``, and
``$…/yr`` salary + ``City, ST`` / ``Remote`` location paragraphs).
"""

import email
from email import policy
from pathlib import Path

from jobcannon.engine.email_parsers import extract_with_fallback
from jobcannon.engine.email_parsers.jobright_parser import parse_jobright_alert
from jobcannon.host.ingestion.imap_intake import _extract_body, _extract_date

FIXTURE = Path(__file__).parent / "fixtures" / "emails" / "jobright.eml"


# ---------------------------------------------------------------------------
# Real-structure HTML helpers (mirror tests/fixtures/emails/jobright.eml)
# ---------------------------------------------------------------------------


def _card(
    company: str,
    title: str,
    job_id: str,
    location: str,
    salary: str = "",
    industry: str = "Advertising",
    stage: str = "Public Company",
    match: str = "92",
) -> str:
    """Build one JobRight match card in the real DOM shape: the whole card is
    wrapped in a single outer job anchor; the title is an inner anchor sharing
    the href; company is a bold <p>; the muted line carries a middle-dot."""
    url = f"https://jobright.ai/jobs/info/{job_id}?utm_source=1121&imp_id=abc"
    salary_p = f'<p style="font-size:12px">{salary}</p>' if salary else ""
    return (
        f'<a href="{url}">'
        f"<table><tbody><tr><td>"
        f'<p style="font-weight:600;font-size:11px;color:#000">{company}</p>'
        f'<p style="font-weight:400;color:rgba(0,0,0,0.40)">{industry}    &#183; {stage}</p>'
        f"<span>{match} %</span>"
        f'<a style="color:rgba(0,0,0,1)" href="{url}">{title}</a>'
        f"{salary_p}"
        f'<p style="font-size:12px">{location}</p>'
        f'<p style="font-size:12px">5+ referrals</p>'
        f"<span>16 minutes ago &#183;</span>"
        f'<span style="font-weight:600">Be an early applicant</span>'
        f"<span>APPLY NOW</span>"
        f"</td></tr></tbody></table></a>"
    )


def _email_body(*cards: str, preamble: str = "Here are your top job matches today") -> str:
    """Wrap cards in a JobRight email shell with a trailing 'View More' footer
    link (a /jobs/recommend link that is NOT a posting)."""
    inner = "".join(cards)
    return (
        "<!DOCTYPE html><html><body>"
        f"<p>{preamble}</p>"
        f"<table><tbody>{inner}</tbody></table>"
        '<a href="https://jobright.ai/jobs/recommend?utm_source=1121">View More Opportunities</a>'
        "</body></html>"
    )


SINGLE = _email_body(
    _card(
        "Acme Corp",
        "Senior Data Scientist",
        "66a1f0c2e4b0a1d2c3e4f5a6",
        "San Francisco, CA",
        salary="$150K/yr - $190K/yr",
    )
)


# ---------------------------------------------------------------------------
# Real fixture — source of truth
# ---------------------------------------------------------------------------

# (title, company, location, salary_min, salary_max, source_id) in document order.
EXPECTED = [
    (
        "Product Data Scientist, Google Play, DSA",
        "Google",
        "Mountain View, CA",
        138000,
        198000,
        "6a453e93c2d11a6a466687b1",
    ),
    ("Product Analytics Lead", "Napster Corp.", "Remote", None, None, "6a4541ee4f64ba41dcb4cbfb"),
    (
        "Staff Analytics, Product & Marketing",
        "EarnIn",
        "Mountain View, US",
        215000,
        263000,
        "6a2b15dec07d4b6ae1c4921b",
    ),
    (
        "Lead Analyst (Supply Analytics, Bangkok-based, Relocation provided)",
        "Agoda",
        "San Jose, CA",
        None,
        None,
        "698e21a2f64d441a16505b3b",
    ),
    (
        "Senior Advisor, Business Analytics - Digital Product",
        "The Cigna Group",
        "Remote",
        113000,
        188000,
        "6a43f147ef17a815538a2589",
    ),
    (
        "Data Scientist 4/5 - Identity DSE",
        "Netflix",
        "Remote",
        372000,
        600000,
        "6a2a4ca00c4972328e7e826e",
    ),
]


def _fixture_jobs():
    """Parse the real fixture through the production IMAP decode path."""
    message = email.message_from_bytes(FIXTURE.read_bytes(), policy=policy.default)
    # PORT-SEAM: host module-level decode fn replaces private ImapSource instance method
    body = _extract_body(message)
    date = _extract_date(message)
    assert body, "fixture body failed to decode"
    return parse_jobright_alert(body, date), date


class TestJobRightRealFixture:
    def test_parses_all_six(self):
        jobs, _ = _fixture_jobs()
        assert len(jobs) == len(EXPECTED)

    def test_titles_in_order(self):
        jobs, _ = _fixture_jobs()
        assert [j.title for j in jobs] == [e[0] for e in EXPECTED]

    def test_companies(self):
        jobs, _ = _fixture_jobs()
        assert [j.company for j in jobs] == [e[1] for e in EXPECTED]

    def test_locations(self):
        jobs, _ = _fixture_jobs()
        assert [j.location for j in jobs] == [e[2] for e in EXPECTED]

    def test_salaries(self):
        jobs, _ = _fixture_jobs()
        assert [(j.salary_min, j.salary_max) for j in jobs] == [(e[3], e[4]) for e in EXPECTED]

    def test_source_ids_are_canonical_hex(self):
        jobs, _ = _fixture_jobs()
        assert [j.source_id for j in jobs] == [e[5] for e in EXPECTED]

    def test_source_urls_are_canonical(self):
        jobs, _ = _fixture_jobs()
        # Tracking params (utm/imp_id) are stripped; the id-only URL is stable.
        assert all(j.source_url == f"https://jobright.ai/jobs/info/{j.source_id}" for j in jobs)

    def test_source_is_jobright(self):
        jobs, _ = _fixture_jobs()
        assert all(j.source == "jobright" for j in jobs)

    def test_posted_date_propagated_from_email(self):
        jobs, date = _fixture_jobs()
        assert date  # fixture carries a Date header
        assert all(j.posted_date == date for j in jobs)

    def test_recommend_footer_link_is_not_a_job(self):
        jobs, _ = _fixture_jobs()
        assert all("recommend" not in j.source_id for j in jobs)


# ---------------------------------------------------------------------------
# Structure unit tests (synthetic, real-shaped)
# ---------------------------------------------------------------------------


class TestJobRightStructure:
    def test_single_all_fields(self):
        (job,) = parse_jobright_alert(SINGLE)
        assert job.title == "Senior Data Scientist"
        assert job.company == "Acme Corp"
        assert job.location == "San Francisco, CA"
        assert (job.salary_min, job.salary_max) == (150000, 190000)
        assert job.source == "jobright"
        assert job.source_id == "66a1f0c2e4b0a1d2c3e4f5a6"
        assert job.source_url == "https://jobright.ai/jobs/info/66a1f0c2e4b0a1d2c3e4f5a6"

    def test_multi_job(self):
        html = _email_body(
            _card("Acme Corp", "Senior Data Scientist", "66a1f0c2e4b0a1d2c3e4f5a6", "Remote"),
            _card("Globex", "ML Engineer", "66a1f0c2e4b0a1d2c3e4f5b7", "Austin, TX"),
            _card("Initech", "Analytics Engineer", "66a1f0c2e4b0a1d2c3e4f5c8", "Remote"),
        )
        jobs = parse_jobright_alert(html)
        assert [j.company for j in jobs] == ["Acme Corp", "Globex", "Initech"]
        assert len({j.source_id for j in jobs}) == 3

    def test_no_salary_is_none(self):
        (job,) = parse_jobright_alert(
            _email_body(_card("Globex", "ML Engineer", "66a1f0c2e4b0a1d2c3e4f5b7", "Remote"))
        )
        assert (job.salary_min, job.salary_max) == (None, None)

    def test_location_country_code(self):
        (job,) = parse_jobright_alert(
            _email_body(
                _card("EarnIn", "Staff Analyst", "66a1f0c2e4b0a1d2c3e4f5b7", "Mountain View, US")
            )
        )
        assert job.location == "Mountain View, US"

    def test_location_remote(self):
        (job,) = parse_jobright_alert(
            _email_body(_card("Globex", "ML Engineer", "66a1f0c2e4b0a1d2c3e4f5b7", "Remote"))
        )
        assert job.location == "Remote"

    def test_match_score_not_company(self):
        # The "92 %" match span must never be read as the company.
        (job,) = parse_jobright_alert(SINGLE)
        assert "%" not in job.company
        assert job.company == "Acme Corp"

    def test_early_applicant_badge_not_company(self):
        (job,) = parse_jobright_alert(SINGLE)
        assert job.company != "Be an early applicant"

    def test_canonical_url_strips_tracking(self):
        (job,) = parse_jobright_alert(SINGLE)
        assert "utm_" not in job.source_url
        assert "imp_id" not in job.source_url

    def test_hourly_salary_normalized(self):
        # Regression: the period cue must survive normalization in a form the
        # shared salary_normalizer recognizes ("/hr", not "/ hour") -- otherwise
        # period resolves to 'unknown' and $60/hr silently mis-annualizes to
        # $60,000/yr (half the true ~$124,800/yr) instead of annualizing (x2080).
        (job,) = parse_jobright_alert(
            _email_body(
                _card(
                    "Acme",
                    "Data Analyst",
                    "66a1f0c2e4b0a1d2c3e4f5a6",
                    "Remote",
                    salary="$60/hr - $80/hr",
                )
            )
        )
        assert (job.salary_min, job.salary_max) == (124800, 166400)

    def test_monthly_salary_normalized(self):
        # Regression: same period-cue bug for "/mo" -- a raw $8K-$10K/month
        # figure falls below the annual plausibility floor if misread as
        # already-annual, silently dropping a valid salary.
        (job,) = parse_jobright_alert(
            _email_body(
                _card(
                    "Acme",
                    "Contractor",
                    "66a1f0c2e4b0a1d2c3e4f5a6",
                    "Remote",
                    salary="$8K/mo - $10K/mo",
                )
            )
        )
        assert (job.salary_min, job.salary_max) == (96000, 120000)

    def test_title_with_percent_is_not_dropped(self):
        # Regression: a "%" filter on the title candidate used to empty the
        # candidate pool and drop the ENTIRE job whenever the real title itself
        # contained a percent sign (e.g. work-arrangement or travel phrasing).
        (job,) = parse_jobright_alert(
            _email_body(
                _card(
                    "Acme",
                    "Data Scientist (100% Remote)",
                    "66a1f0c2e4b0a1d2c3e4f5a6",
                    "Remote",
                )
            )
        )
        assert job.title == "Data Scientist (100% Remote)"

    def test_title_with_location_token_is_not_dropped(self):
        # Regression: a location-shaped filter on the title candidate used to
        # empty the pool and drop the ENTIRE job whenever the title itself
        # baked in a location/work-arrangement token (e.g. "Sales Manager, TX").
        (job,) = parse_jobright_alert(
            _email_body(
                _card(
                    "Acme",
                    "Sales Manager, TX",
                    "66a1f0c2e4b0a1d2c3e4f5a6",
                    "Austin, TX",
                )
            )
        )
        assert job.title == "Sales Manager, TX"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestJobRightEdgeCases:
    def test_empty_body(self):
        assert parse_jobright_alert("") == []

    def test_none_body(self):
        assert parse_jobright_alert(None) == []

    def test_no_jobright_links(self):
        assert parse_jobright_alert("<html><body>Just some text</body></html>") == []

    def test_recommend_link_only_yields_nothing(self):
        html = _email_body()  # no cards, only the /jobs/recommend footer link
        assert parse_jobright_alert(html) == []

    def test_bare_jobs_listing_link_ignored(self):
        html = "<html><body><a href='https://jobright.ai/jobs'>Browse all jobs</a></body></html>"
        assert parse_jobright_alert(html) == []

    def test_account_email_yields_no_jobs(self):
        html = (
            "<html><body><p>Verify your email to activate your JobRight account.</p></body></html>"
        )
        assert parse_jobright_alert(html) == []


# ---------------------------------------------------------------------------
# Zero-yield WARNING (issue #259 convention) -- pins the format-drift alarm
# itself, not just the parser's `== []` output, so a regression that silences
# it (mis-tuned _MIN_WARN_BODY_LEN, an over-broad _is_account_email) is caught.
# ---------------------------------------------------------------------------


class TestJobRightZeroYieldWarning:
    def test_warning_fires_on_long_unrecognized_body(self, caplog):
        long_body = "<html><body><p>" + ("no jobright links here " * 40) + "</p></body></html>"
        assert len(long_body.strip()) > 500
        with caplog.at_level("WARNING"):
            assert parse_jobright_alert(long_body) == []
        assert any("format may have changed" in r.message for r in caplog.records)

    def test_warning_suppressed_for_account_email(self, caplog):
        long_body = (
            "<html><body><p>Verify your email to activate your JobRight account. "
            + ("Thanks for joining JobRight! " * 30)
            + "</p></body></html>"
        )
        assert len(long_body.strip()) > 500
        with caplog.at_level("WARNING"):
            assert parse_jobright_alert(long_body) == []
        assert not any("format may have changed" in r.message for r in caplog.records)

    def test_warning_suppressed_for_short_body(self, caplog):
        with caplog.at_level("WARNING"):
            assert parse_jobright_alert("<html><body>too short</body></html>") == []
        assert not any("format may have changed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Regression tests — real-world variations / adversarial findings
# ---------------------------------------------------------------------------

# Plaintext alternative (what _extract_body returns if a future JobRight email
# carries a non-empty text/plain part). JobRight is intentionally excluded from
# the positional URL fallback, so this must ingest NOTHING, not garbage rows.
JOBRIGHT_PLAINTEXT = (
    "Here are your top matches:\n\n"
    "Senior Data Scientist\nAcme Corp\nSan Francisco, CA\n"
    "https://jobright.ai/jobs/info/66a1f0c2e4b0a1d2c3e4f5a6\n"
)


class TestJobRightRegressions:
    def test_plaintext_body_yields_no_garbage_via_fallback(self):
        assert extract_with_fallback(parse_jobright_alert, JOBRIGHT_PLAINTEXT, None) == []

    def test_source_id_unwraps_click_tracker(self):
        # An unencoded click-tracker still yields the inner hex id.
        url = "https://click.jobright.ai/CL0/https://jobright.ai/jobs/info/deadbeef12345678/1/abc"
        html = (
            "<!DOCTYPE html><html><body>"
            f'<a href="{url}">'
            "<table><tbody><tr><td>"
            '<p style="font-weight:600">Acme Corp</p>'
            f'<a href="{url}">Data Engineer</a>'
            "<span>APPLY NOW</span>"
            "</td></tr></tbody></table></a>"
            "</body></html>"
        )
        (job,) = parse_jobright_alert(html)
        assert job.source_id == "deadbeef12345678"
        assert job.source_url == "https://jobright.ai/jobs/info/deadbeef12345678"

    def test_company_with_trailing_comma_not_location(self):
        # 'Globex, Co' must NOT be read as "City, ST" — the state code stays
        # case-sensitive so ', Co' (lowercase o) is not [A-Z]{2}.
        html = _email_body(
            _card("Globex, Co", "Data Analyst", "66a1f0c2e4b0a1d2c3e4f5a6", "Austin, TX")
        )
        (job,) = parse_jobright_alert(html)
        assert job.company == "Globex, Co"
        assert job.location == "Austin, TX"

    def test_generic_cta_alone_is_not_a_title(self):
        # A card whose only clean anchor text is a generic CTA yields no title.
        url = "https://jobright.ai/jobs/info/66a1f0c2e4b0a1d2c3e4f5a6"
        html = (
            f'<!DOCTYPE html><html><body><a href="{url}"><span>APPLY NOW</span></a></body></html>'
        )
        assert parse_jobright_alert(html) == []
