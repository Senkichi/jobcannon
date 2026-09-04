# PORTED from tests/test_parser_zero_yield_warnings.py @ 5508056d80b37d12248da98ac08bd86550ee8b75 (private job-cannon). Ledger L-0558.
"""Tests for zero-yield warning logging in email parsers (issue #259).

Each parser must emit exactly one WARNING when a non-empty (>500 char) body
yields 0 jobs. Meta-emails and empty bodies must NOT emit a warning.
"""

import logging

from jobcannon.engine.email_parsers.glassdoor_parser import parse_glassdoor_alert
from jobcannon.engine.email_parsers.greenhouse_parser import parse_greenhouse_alert
from jobcannon.engine.email_parsers.indeed_parser import (
    parse_indeed_alert,
    parse_indeed_match_alert,
)
from jobcannon.engine.email_parsers.jobright_parser import parse_jobright_alert
from jobcannon.engine.email_parsers.linkedin_parser import parse_linkedin_alert
from jobcannon.engine.email_parsers.monster_parser import parse_monster_alert
from jobcannon.engine.email_parsers.trueup_parser import parse_trueup_alert
from jobcannon.engine.email_parsers.ziprecruiter_parser import parse_ziprecruiter_alert

# A large block of plausible-looking but unparseable body text (>500 chars).
_JUNK_BODY = "This is a job-related email that cannot be parsed into individual job listings. " * 12

# Short/trivial body — should not trigger the >500 guard.
_SHORT_BODY = "Too short to parse."

# A LinkedIn-style meta email body (notification email, not a real alert).
_LINKEDIN_META_BODY = "You'll receive notifications when new jobs match your search criteria. " * 10


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------


class TestLinkedInZeroYieldWarning:
    def test_large_junk_body_emits_warning(self, caplog):
        """Non-empty (>500 char) body that yields 0 jobs → one WARNING naming linkedin."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.linkedin_parser"
        ):
            result = parse_linkedin_alert(_JUNK_BODY)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "linkedin" in warnings[0].message.lower()

    def test_meta_email_emits_no_warning(self, caplog):
        """Meta-email early return must not emit a WARNING."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.linkedin_parser"
        ):
            result = parse_linkedin_alert(_LINKEDIN_META_BODY)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []

    def test_short_body_emits_no_warning(self, caplog):
        """Body under 500 chars must not emit a WARNING."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.linkedin_parser"
        ):
            result = parse_linkedin_alert(_SHORT_BODY)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []


# ---------------------------------------------------------------------------
# Indeed alert
# ---------------------------------------------------------------------------


class TestIndeedAlertZeroYieldWarning:
    def test_large_junk_body_emits_warning(self, caplog):
        """Non-empty (>500 char) plain-text body yields 0 → at least one WARNING."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.indeed_parser"
        ):
            result = parse_indeed_alert(_JUNK_BODY)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1

    def test_meta_email_emits_no_warning(self, caplog):
        """Meta-email (digest/admin) early return must not emit a WARNING."""
        meta_body = "confirm your email address subscription unsubscribe from alerts"
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.indeed_parser"
        ):
            result = parse_indeed_alert(meta_body)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []


# ---------------------------------------------------------------------------
# Indeed match
# ---------------------------------------------------------------------------


class TestIndeedMatchZeroYieldWarning:
    def test_large_junk_body_emits_warning(self, caplog):
        """Non-empty (>500 char) match body with no recognized URLs → one WARNING."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.indeed_parser"
        ):
            result = parse_indeed_match_alert(_JUNK_BODY)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "indeed" in warnings[0].message.lower()

    def test_short_body_emits_no_warning(self, caplog):
        """Short body → no WARNING."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.indeed_parser"
        ):
            result = parse_indeed_match_alert(_SHORT_BODY)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []


# ---------------------------------------------------------------------------
# Glassdoor
# ---------------------------------------------------------------------------

# Brand-views pixel — Glassdoor review/follow digest, expected debug (not warning).
_GLASSDOOR_BRAND_VIEWS = (
    '<html><body><img src="https://www.glassdoor.com/brand-views/pixel.png">'
    + "x" * 600
    + "</body></html>"
)

# Large HTML with no job links, not a meta-email and not brand-views.
_GLASSDOOR_JUNK_HTML = "<html><body>" + "Some text. " * 60 + "</body></html>"


class TestGlassdoorZeroYieldWarning:
    def test_large_junk_html_emits_warning(self, caplog):
        """HTML body >500 chars with no job links and not meta/brand-views → one WARNING."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.glassdoor_parser"
        ):
            result = parse_glassdoor_alert(_GLASSDOOR_JUNK_HTML)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "glassdoor" in warnings[0].message.lower()

    def test_brand_views_digest_emits_no_warning(self, caplog):
        """Brand-views pixel email must stay at debug (not warning)."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.glassdoor_parser"
        ):
            result = parse_glassdoor_alert(_GLASSDOOR_BRAND_VIEWS)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []

    def test_short_body_emits_no_warning(self, caplog):
        """Short body → no WARNING."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.glassdoor_parser"
        ):
            result = parse_glassdoor_alert("<html></html>")
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []


# ---------------------------------------------------------------------------
# ZipRecruiter
# ---------------------------------------------------------------------------

_ZIPRECRUITER_JUNK_HTML = "<html><body>" + "No job links here. " * 30 + "</body></html>"


class TestZipRecruiterZeroYieldWarning:
    def test_large_junk_html_emits_warning(self, caplog):
        """HTML body >500 chars with no job links → one WARNING naming ziprecruiter."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.ziprecruiter_parser"
        ):
            result = parse_ziprecruiter_alert(_ZIPRECRUITER_JUNK_HTML)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "ziprecruiter" in warnings[0].message.lower()

    def test_short_body_emits_no_warning(self, caplog):
        """Short body → no WARNING."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.ziprecruiter_parser"
        ):
            result = parse_ziprecruiter_alert("<html></html>")
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []


# ---------------------------------------------------------------------------
# Monster
# ---------------------------------------------------------------------------

_MONSTER_JUNK_HTML = "<html><body>" + "No monster job links here. " * 30 + "</body></html>"

# Meta email — short enough that len(body.strip()) <= 500 at existing guard.
_MONSTER_META = "job alert digest notification"


class TestMonsterZeroYieldWarning:
    def test_large_junk_html_emits_warning(self, caplog):
        """HTML body >500 chars with no job links → WARNING naming monster."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.monster_parser"
        ):
            result = parse_monster_alert(_MONSTER_JUNK_HTML)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "monster" in warnings[0].message.lower()

    def test_short_body_emits_no_warning(self, caplog):
        """Short body → no WARNING."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.monster_parser"
        ):
            result = parse_monster_alert(_MONSTER_META)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []


# ---------------------------------------------------------------------------
# TrueUp
# ---------------------------------------------------------------------------

_TRUEUP_JUNK_HTML = "<html><body>" + "No trueup job links here. " * 30 + "</body></html>"


class TestTrueUpZeroYieldWarning:
    def test_large_junk_html_emits_warning(self, caplog):
        """HTML body >500 chars with no TrueUp links → one WARNING naming trueup."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.trueup_parser"
        ):
            result = parse_trueup_alert(_TRUEUP_JUNK_HTML)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "trueup" in warnings[0].message.lower()

    def test_short_body_emits_no_warning(self, caplog):
        """Short body → no WARNING."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.trueup_parser"
        ):
            result = parse_trueup_alert("<html></html>")
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []


# ---------------------------------------------------------------------------
# JobRight
# ---------------------------------------------------------------------------

_JOBRIGHT_JUNK_HTML = "<html><body>" + "No jobright job links here. " * 30 + "</body></html>"

# Account/marketing preamble (>500 chars) — first 200 chars mark it non-job, so
# the zero-yield warning must be suppressed even though it yields 0 jobs.
_JOBRIGHT_ACCOUNT = "Verify your email address to activate your JobRight account. " * 12


class TestJobRightZeroYieldWarning:
    def test_large_junk_html_emits_warning(self, caplog):
        """HTML body >500 chars with no JobRight links → one WARNING naming jobright."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.jobright_parser"
        ):
            result = parse_jobright_alert(_JOBRIGHT_JUNK_HTML)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "jobright" in warnings[0].message.lower()

    def test_account_email_emits_no_warning(self, caplog):
        """Account/marketing preamble must not emit a WARNING (not a parse failure)."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.jobright_parser"
        ):
            result = parse_jobright_alert(_JOBRIGHT_ACCOUNT)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []

    def test_short_body_emits_no_warning(self, caplog):
        """Body under 500 chars must not emit a WARNING."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.jobright_parser"
        ):
            result = parse_jobright_alert("<html></html>")
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------

_GREENHOUSE_JUNK_BODY = "This is a weekly update about new roles. No actual job URLs here. " * 12

# Greenhouse meta email — is_meta_email should catch this as a digest.
_GREENHOUSE_META = "job alert digest notification unsubscribe"


class TestGreenhouseZeroYieldWarning:
    def test_large_junk_body_emits_warning(self, caplog):
        """Plain-text body >500 chars with no Greenhouse URLs → one WARNING naming greenhouse."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.greenhouse_parser"
        ):
            result = parse_greenhouse_alert(_GREENHOUSE_JUNK_BODY)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "greenhouse" in warnings[0].message.lower()

    def test_short_body_emits_no_warning(self, caplog):
        """Short body → no WARNING."""
        with caplog.at_level(
            logging.WARNING, logger="jobcannon.engine.email_parsers.greenhouse_parser"
        ):
            result = parse_greenhouse_alert(_GREENHOUSE_META)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []
