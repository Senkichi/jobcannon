# PORTED from tests/test_monster_parser.py @ e6ff9bf3c0f5b7505b127e6b6a0ca945382831be (private job-cannon). Ledger L-0557.
"""Tests for Monster job alert parser (monster@notifications.monster.com).

HTML structure is derived from inspection of real Monster emails.
Each job card is a <table class="width-100"> containing:
  - Title: <a class="hdline-2" href="click.monster.com/...">
  - Company/location: <td class="hdline-3 left-20"> with <span class="hdline-3"> spans
  - CTA: QUICK APPLY or VIEW JOB button
"""

from datetime import datetime

from jobcannon.engine.email_parsers.monster_parser import parse_monster_alert

# ---------------------------------------------------------------------------
# Minimal HTML helpers
# ---------------------------------------------------------------------------


def _job_card(
    title: str,
    company: str,
    city: str,
    state: str,
    title_href: str,
    cta: str = "QUICK APPLY",
) -> str:
    """Build a single Monster job card HTML block."""
    cta_href = f"http://click.monster.com/f/a/CTA_{title[:10].replace(' ', '_')}"
    return f"""
<table class="width-100">
  <tr><td class="left-20">
    <a class="hdline-2" href="{title_href}"><strong>{title}</strong></a>
  </td></tr>
  <tr><td>
    <table><tr>
      <td class="hdline-3 left-20">
        <span class="hdline-3">{company}</span>
        <span class="hdline-3"> - </span>
        <span class="hdline-3">{city}</span>
        <span class="hdline-3"> - </span>
        <span class="hdline-3"> {state}</span>
      </td>
    </tr></table>
  </td></tr>
  <tr><td>
    <table class="width-100"><tr>
      <td class="width-100 height button">
        <a class="width-100 button" href="{cta_href}"><span>{cta}</span></a>
      </td>
    </tr></table>
  </td></tr>
</table>
"""


def _email_body(*cards: str) -> str:
    """Wrap job cards in a minimal Monster email shell."""
    body = "\n".join(cards)
    return f"""<!DOCTYPE html>
<html><body>
<p>Alex, here are today's jobs</p>
{body}
<p>View Jobs in Last 7 Days | VIEW ALL JOBS</p>
<p>Unsubscribe | Monster Privacy Policy</p>
</body></html>"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TITLE_HREF_1 = "http://click.monster.com/f/a/AAAA"
TITLE_HREF_2 = "http://click.monster.com/f/a/BBBB"
TITLE_HREF_3 = "http://click.monster.com/f/a/CCCC"

SINGLE_JOB_HTML = _email_body(
    _job_card(
        title="Senior Data Scientist",
        company="Acme Corp",
        city="San Francisco",
        state="CA",
        title_href=TITLE_HREF_1,
    )
)

MULTI_JOB_HTML = _email_body(
    _job_card(
        title="Senior Data Scientist",
        company="Acme Corp",
        city="San Francisco",
        state="CA",
        title_href=TITLE_HREF_1,
    ),
    _job_card(
        title="Director, Data Science (Remote)",
        company="Kohl's",
        city="Menomonee Falls",
        state="WI",
        title_href=TITLE_HREF_2,
        cta="VIEW JOB",
    ),
    _job_card(
        title="Remote Software Developer / Data Scientist",
        company="SynergisticIT",
        city="Denver",
        state="CO",
        title_href=TITLE_HREF_3,
    ),
)

# Job card with whitespace in state span (mirrors real Monster email formatting)
WHITESPACE_STATE_HTML = _email_body(
    f"""
<table class="width-100">
  <tr><td class="left-20">
    <a class="hdline-2" href="{TITLE_HREF_1}"><strong>Data Analyst</strong></a>
  </td></tr>
  <tr><td>
    <table><tr>
      <td class="hdline-3 left-20">
        <span class="hdline-3">Widget Inc</span>
        <span class="hdline-3"> - </span>
        <span class="hdline-3">Menomonee Falls \n                   </span>
        <span class="hdline-3"> - </span>
        <span class="hdline-3"> \n                   WI</span>
      </td>
    </tr></table>
  </td></tr>
</table>
"""
)

# Nested deeper like the real "QUICK APPLY" format where title is more nested
NESTED_TITLE_HTML = _email_body(
    f"""
<table class="width-100">
  <tr><td>
    <table><tr>
      <td class="left-20">
        <table><tr><td class="left-20">
          <a class="hdline-2" href="{TITLE_HREF_1}"><strong>Principal Engineer</strong></a>
        </td></tr></table>
      </td>
    </tr></table>
  </td></tr>
  <tr><td>
    <table><tr>
      <td class="hdline-3 left-20">
        <span class="hdline-3">TechCo</span>
        <span class="hdline-3"> - </span>
        <span class="hdline-3">Austin</span>
        <span class="hdline-3"> - </span>
        <span class="hdline-3">TX</span>
      </td>
    </tr></table>
  </td></tr>
</table>
"""
)


# ---------------------------------------------------------------------------
# Tests: single job
# ---------------------------------------------------------------------------


class TestMonsterSingleJob:
    def test_parses_one_job(self):
        jobs = parse_monster_alert(SINGLE_JOB_HTML)
        assert len(jobs) == 1

    def test_title(self):
        jobs = parse_monster_alert(SINGLE_JOB_HTML)
        assert jobs[0].title == "Senior Data Scientist"

    def test_company(self):
        jobs = parse_monster_alert(SINGLE_JOB_HTML)
        assert jobs[0].company == "Acme Corp"

    def test_location(self):
        jobs = parse_monster_alert(SINGLE_JOB_HTML)
        assert jobs[0].location == "San Francisco, CA"

    def test_source(self):
        jobs = parse_monster_alert(SINGLE_JOB_HTML)
        assert jobs[0].source == "monster"

    def test_source_url_is_tracking_link(self):
        jobs = parse_monster_alert(SINGLE_JOB_HTML)
        assert jobs[0].source_url == TITLE_HREF_1
        assert "click.monster.com" in jobs[0].source_url

    def test_source_id_empty(self):
        # Monster tracking URLs expose no raw job ID
        jobs = parse_monster_alert(SINGLE_JOB_HTML)
        assert jobs[0].source_id == ""

    def test_posted_date_propagated(self):
        date = datetime(2026, 4, 5)
        jobs = parse_monster_alert(SINGLE_JOB_HTML, email_date=date)
        assert jobs[0].posted_date == date

    def test_no_salary(self):
        jobs = parse_monster_alert(SINGLE_JOB_HTML)
        assert jobs[0].salary_min is None
        assert jobs[0].salary_max is None


# ---------------------------------------------------------------------------
# Tests: multiple jobs
# ---------------------------------------------------------------------------


class TestMonsterMultiJob:
    def test_parses_three_jobs(self):
        jobs = parse_monster_alert(MULTI_JOB_HTML)
        assert len(jobs) == 3

    def test_first_job(self):
        jobs = parse_monster_alert(MULTI_JOB_HTML)
        assert jobs[0].title == "Senior Data Scientist"
        assert jobs[0].company == "Acme Corp"
        assert jobs[0].location == "San Francisco, CA"

    def test_second_job_with_view_job_cta(self):
        jobs = parse_monster_alert(MULTI_JOB_HTML)
        assert jobs[1].title == "Director, Data Science (Remote)"
        assert jobs[1].company == "Kohl's"
        assert jobs[1].location == "Menomonee Falls, WI"

    def test_third_job(self):
        jobs = parse_monster_alert(MULTI_JOB_HTML)
        assert jobs[2].title == "Remote Software Developer / Data Scientist"
        assert jobs[2].company == "SynergisticIT"
        assert jobs[2].location == "Denver, CO"

    def test_each_has_distinct_url(self):
        jobs = parse_monster_alert(MULTI_JOB_HTML)
        urls = {j.source_url for j in jobs}
        assert len(urls) == 3


# ---------------------------------------------------------------------------
# Tests: HTML formatting edge cases
# ---------------------------------------------------------------------------


class TestMonsterEdgeCases:
    def test_whitespace_in_state_span(self):
        """State span with leading/trailing whitespace is stripped correctly."""
        jobs = parse_monster_alert(WHITESPACE_STATE_HTML)
        assert len(jobs) == 1
        assert jobs[0].location == "Menomonee Falls, WI"

    def test_deeply_nested_title(self):
        """Title link nested multiple table levels up still resolves correctly."""
        jobs = parse_monster_alert(NESTED_TITLE_HTML)
        assert len(jobs) == 1
        assert jobs[0].title == "Principal Engineer"
        assert jobs[0].company == "TechCo"
        assert jobs[0].location == "Austin, TX"

    def test_dedup_same_href(self):
        """Same tracking URL appearing twice yields one job."""
        dupe_html = _email_body(
            _job_card("Job A", "Co A", "City", "CA", TITLE_HREF_1),
            _job_card("Job A", "Co A", "City", "CA", TITLE_HREF_1),
        )
        jobs = parse_monster_alert(dupe_html)
        assert len(jobs) == 1

    def test_cta_links_not_parsed_as_jobs(self):
        """QUICK APPLY and VIEW JOB buttons must not appear as job titles."""
        jobs = parse_monster_alert(MULTI_JOB_HTML)
        titles = {j.title for j in jobs}
        assert "QUICK APPLY" not in titles
        assert "VIEW JOB" not in titles

    def test_empty_body(self):
        assert parse_monster_alert("") == []

    def test_none_body(self):
        assert parse_monster_alert(None) == []

    def test_no_monster_links(self):
        assert parse_monster_alert("<html><body>Just text</body></html>") == []

    def test_missing_company_cell_returns_unknown(self):
        """Job card with no company/location cell falls back to Unknown."""
        no_company_html = _email_body(f"""
<table class="width-100">
  <tr><td class="left-20">
    <a class="hdline-2" href="{TITLE_HREF_1}"><strong>Some Job</strong></a>
  </td></tr>
</table>
""")
        jobs = parse_monster_alert(no_company_html)
        assert len(jobs) == 1
        assert jobs[0].title == "Some Job"
        assert jobs[0].company == "Unknown"
        assert jobs[0].location == "Unknown"
