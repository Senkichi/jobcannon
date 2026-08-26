"""jobcannon/web/legal.py — GET /privacy and GET /terms (issue #94).

No DB access, so tests here need no throwaway Postgres — same no-DB shape
the prior tests/host/test_privacy_route.py used (removed: this file
supersedes it) and tests/host/test_pages.py's /demo tests use. The
_app() helper below is that same pattern.

Six things this module pins:
  (a) both committed .md files under jobcannon/web/legal/ pass the guard
  (b) every rule in jobcannon.web.legal_guard fires on a seeded positive
      control — parametrized off the guard's own exported rule data
      (legal_guard.FORBIDDEN_PHRASES) rather than a hand-copied list, so a
      phrase added there with no test case fails this suite
  (c) /privacy and /terms render 200 unauthenticated, contain no leftover
      "<!--", and contain their document's H1
  (d) /terms is in PUBLIC_PATHS (/privacy already was, kept)
  (e) base.html's footer links both /privacy and /terms
  (f) consent.html links /privacy and no longer says the policy is pending
"""

from __future__ import annotations

import pathlib

import pytest

from jobcannon.web.legal_guard import FORBIDDEN_PHRASES, check_published_text

_LEGAL_DIR = pathlib.Path("jobcannon/web/legal")
_CLEAN_BASE = (
    "**Effective date:** 2026-08-27\n\nSome ordinary published sentence about the Service.\n"
)


def _app(verify=None):
    from jobcannon.web import create_app

    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q=",
        }
    )


# ---------------------------------------------------------------------------
# (a) committed files pass the guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", ["privacy.md", "terms.md"])
def test_committed_legal_file_passes_the_guard(filename):
    text = (_LEGAL_DIR / filename).read_text(encoding="utf-8")
    assert check_published_text(text) == []


# ---------------------------------------------------------------------------
# (b) sabotage self-tests — one positive control per rule, driven off the
# guard's own exported data so a rule added later with no seed here fails.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", FORBIDDEN_PHRASES)
def test_guard_fires_on_each_forbidden_phrase(phrase):
    seeded = _CLEAN_BASE + f"This sentence contains {phrase} in the middle of it.\n"
    violations = check_published_text(seeded)
    assert any(phrase in v for v in violations), (phrase, violations)


def test_guard_fires_on_html_comment_opener():
    assert check_published_text(_CLEAN_BASE + "<!-- a note -->") != []


def test_guard_fires_on_bare_comment_closer():
    # A stray '-->' with no matching opener still means the strip missed
    # something — check independently of the opener rule.
    assert check_published_text(_CLEAN_BASE + "stray closer -->") != []


def test_guard_fires_on_unfilled_bracket_placeholder():
    violations = check_published_text(_CLEAN_BASE + "Contact us at [CONTACT EMAIL].")
    assert any("bracket placeholder" in v for v in violations)


def test_guard_fires_on_commit_sha_shaped_token():
    violations = check_published_text(_CLEAN_BASE + "pinned at commit 689c945abc for reference.")
    assert any("commit-SHA-shaped" in v for v in violations)


def test_guard_does_not_false_positive_on_ordinary_hex_letter_words():
    """689c945abc looks like a SHA (digits + a-f letters); 'facade' and
    'defaced' do not (all-letter, a-f only) — the digit+letter requirement
    exists precisely so ordinary English isn't flagged."""
    text = _CLEAN_BASE + "The facade was defaced with graffiti near the cafe."
    assert check_published_text(text) == []


def test_guard_fires_on_missing_effective_date_line():
    violations = check_published_text("No date line anywhere in this text.")
    assert any("missing an 'Effective date:' line" in v for v in violations)


def test_guard_fires_on_malformed_effective_date_value():
    violations = check_published_text("**Effective date:** sometime soon\n\nBody text.")
    assert any("does not contain a YYYY-MM-DD date" in v for v in violations)


def test_guard_clean_base_has_no_violations():
    """Positive control for the positive controls: the seed text itself,
    with nothing sabotaged, must pass — otherwise every test above would be
    trivially true regardless of what it seeds."""
    assert check_published_text(_CLEAN_BASE) == []


# ---------------------------------------------------------------------------
# (c) routes render 200 unauthenticated, no leftover comments, H1 present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,h1_text",
    [
        ("/privacy", "Job Cannon — Privacy Policy"),
        ("/terms", "Job Cannon — Terms of Service"),
    ],
)
def test_legal_page_renders_unauthed_200(path, h1_text):
    app = _app(verify=lambda req: None)
    client = app.test_client()

    resp = client.get(path)

    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "<!--" not in body
    assert h1_text in body


@pytest.mark.parametrize("path", ["/privacy", "/terms"])
def test_legal_page_renders_authed_200(path):
    from jobcannon.web.auth import ClerkIdentity

    app = _app(verify=lambda req: ClerkIdentity(user_id="user_1", claims={"sub": "user_1"}))
    client = app.test_client()

    resp = client.get(path)

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# (d) /terms is public (mirrors the existing /privacy pin)
# ---------------------------------------------------------------------------


def test_privacy_and_terms_are_both_in_public_paths():
    from jobcannon.web import PUBLIC_PATHS

    assert "/privacy" in PUBLIC_PATHS
    assert "/terms" in PUBLIC_PATHS


# ---------------------------------------------------------------------------
# (e) / (f) template-source assertions
# ---------------------------------------------------------------------------


def test_footer_links_to_privacy_and_terms():
    src = pathlib.Path("jobcannon/web/templates/base.html").read_text(encoding="utf-8")
    assert 'href="/privacy"' in src
    assert 'href="/terms"' in src


def test_consent_copy_links_to_privacy_and_no_longer_says_pending():
    src = pathlib.Path("jobcannon/web/templates/consent.html").read_text(encoding="utf-8")
    assert 'href="/privacy"' in src
    assert "pending" not in src.lower()
