# PORTED from tests/test_trueup_parser.py @ caf50e8ce8e13a606d83630002c219e3bd034bf2 (private job-cannon). Ledger L-0561.
"""Tests for TrueUp weekly digest parser (hello@trueup.io)."""

from datetime import datetime

from jobcannon.engine.email_parsers.trueup_parser import parse_trueup_alert

# Minimal realistic HTML fixture with 2 job cards
SAMPLE_TRUEUP_HTML = """\
<!DOCTYPE html>
<html>
<body>
<table width="100%">
  <tr><td>
    <table width="540" style="margin:0 auto;">
      <tr><td>
        <div style="margin:40px 0 10px;font-size:20px;font-weight:600;">
          <a href="http://url3500.trueup.io/ls/click?upn=u001.abc123">
            94 new jobs
          </a> for you this week
        </div>
        <div style="padding:0 0 30px;">All open jobs that fit your profile: 234</div>

        <div style="margin-bottom:16px;padding:16px;border:1px solid #ddd;border-radius:10px;">
          <table style="width:100%;border-spacing:0;">
            <tr>
              <td style="vertical-align:top;padding-right:20px;width:48px;">
                <img src="https://img.logo.dev/meta.com" alt="Meta logo">
              </td>
              <td style="vertical-align:top;">
                <div style="font-weight:600;font-size:16px;margin-bottom:6px;">
                  <a href="http://url3500.trueup.io/ls/click?upn=u001.job1title">
                    Area Schedule Lead, Leased Data Centers
                  </a>
                </div>
                <div style="font-size:16px;margin-bottom:10px;">
                  <a href="http://url3500.trueup.io/ls/click?upn=u001.job1company">
                    Meta
                  </a>
                  <div style="font-size:14px;color:#6c757d;">Social networking</div>
                </div>
                <div style="font-size:14px;font-weight:500;color:#6c757d;margin-bottom:10px;">
                  MENLO PARK, CA
                </div>
                <div style="font-size:14px;color:#6c757d;">Yesterday</div>
              </td>
            </tr>
          </table>
        </div>

        <div style="margin-bottom:16px;padding:16px;border:1px solid #ddd;border-radius:10px;">
          <table style="width:100%;border-spacing:0;">
            <tr>
              <td style="vertical-align:top;padding-right:20px;width:48px;">
                <img src="https://img.logo.dev/tesla.com" alt="Tesla logo">
              </td>
              <td style="vertical-align:top;">
                <div style="font-weight:600;font-size:16px;margin-bottom:6px;">
                  <a href="http://url3500.trueup.io/ls/click?upn=u001.job2title">
                    Sr. Business Intelligence Analyst
                  </a>
                </div>
                <div style="font-size:16px;margin-bottom:10px;">
                  <a href="http://url3500.trueup.io/ls/click?upn=u001.job2company">
                    Tesla
                  </a>
                  <div style="font-size:14px;color:#6c757d;">Electric vehicles</div>
                </div>
                <div style="font-size:14px;font-weight:500;color:#6c757d;margin-bottom:10px;">
                  FREMONT, CALIFORNIA
                </div>
                <div style="font-size:14px;color:#6c757d;">3 days ago</div>
              </td>
            </tr>
          </table>
        </div>

        <table width="100%" style="margin:30px 0;">
          <tr>
            <td align="center">
              <a href="http://url3500.trueup.io/ls/click?upn=u001.viewall"
                 style="background:#000;color:#fff;">
                View all open jobs  →
              </a>
            </td>
          </tr>
        </table>

        <div style="padding:32px 0;font-size:14px;">
          Visit <a href="http://url3500.trueup.io/ls/click?upn=u001.mytrueup"
                    style="color:#000;">My TrueUp</a> to update your profile.
        </div>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

SAMPLE_TRUEUP_EMPTY = """\
<!DOCTYPE html><html><body>
<table><tr><td>No jobs this week.</td></tr></table>
</body></html>"""


class TestTrueUpParser:
    def test_parses_job_cards(self):
        jobs = parse_trueup_alert(SAMPLE_TRUEUP_HTML)
        assert len(jobs) == 2

    def test_first_job_title(self):
        jobs = parse_trueup_alert(SAMPLE_TRUEUP_HTML)
        assert jobs[0].title == "Area Schedule Lead, Leased Data Centers"

    def test_first_job_company(self):
        jobs = parse_trueup_alert(SAMPLE_TRUEUP_HTML)
        assert jobs[0].company == "Meta"

    def test_first_job_location(self):
        jobs = parse_trueup_alert(SAMPLE_TRUEUP_HTML)
        assert jobs[0].location == "MENLO PARK, CA"

    def test_second_job_title(self):
        jobs = parse_trueup_alert(SAMPLE_TRUEUP_HTML)
        assert jobs[1].title == "Sr. Business Intelligence Analyst"

    def test_second_job_company(self):
        jobs = parse_trueup_alert(SAMPLE_TRUEUP_HTML)
        assert jobs[1].company == "Tesla"

    def test_second_job_location(self):
        jobs = parse_trueup_alert(SAMPLE_TRUEUP_HTML)
        assert jobs[1].location == "FREMONT, CALIFORNIA"

    def test_source_is_trueup(self):
        jobs = parse_trueup_alert(SAMPLE_TRUEUP_HTML)
        assert all(j.source == "trueup" for j in jobs)

    def test_source_url_is_tracking_redirect(self):
        jobs = parse_trueup_alert(SAMPLE_TRUEUP_HTML)
        assert all("trueup.io/ls/click" in j.source_url for j in jobs)

    def test_source_id_not_persisted(self):
        # TrueUp links expose only the SendGrid `upn` token (per-recipient, not
        # per-job), so no source_id is persisted (I-11 contract).
        jobs = parse_trueup_alert(SAMPLE_TRUEUP_HTML)
        assert all(not j.source_id for j in jobs)

    def test_no_salary(self):
        """TrueUp doesn't include salary info."""
        jobs = parse_trueup_alert(SAMPLE_TRUEUP_HTML)
        assert all(j.salary_min is None for j in jobs)
        assert all(j.salary_max is None for j in jobs)

    def test_email_date_propagated(self):
        date = datetime(2026, 3, 18)
        jobs = parse_trueup_alert(SAMPLE_TRUEUP_HTML, email_date=date)
        assert all(j.posted_date == date for j in jobs)

    def test_footer_links_excluded(self):
        """'View all open jobs' and 'My TrueUp' should not be parsed as jobs."""
        jobs = parse_trueup_alert(SAMPLE_TRUEUP_HTML)
        for job in jobs:
            assert "view all" not in job.title.lower()
            assert "my trueup" not in job.title.lower()


class TestTrueUpEdgeCases:
    def test_empty_body(self):
        assert parse_trueup_alert("") == []

    def test_none_body(self):
        assert parse_trueup_alert(None) == []

    def test_no_cards(self):
        assert parse_trueup_alert(SAMPLE_TRUEUP_EMPTY) == []

    def test_non_html_body(self):
        assert parse_trueup_alert("Just plain text") == []
