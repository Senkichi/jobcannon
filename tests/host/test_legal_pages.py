"""jobcannon/web/legal.py — GET /privacy and GET /terms (issue #94).

No DB access, so tests here need no throwaway Postgres — same no-DB shape
the prior tests/host/test_privacy_route.py used (removed: this file
supersedes it) and tests/host/test_pages.py's /demo tests use. The
_app() helper below is that same pattern.

Things this module pins:
  (a) both committed .md files under jobcannon/web/legal/ pass the guard
  (b) every rule in jobcannon.web.legal_guard fires on a seeded positive
      control — parametrized off the guard's own exported rule data
      (legal_guard.FORBIDDEN_PHRASES) rather than a hand-copied list, so a
      phrase added there with no test case fails this suite
  (c) /privacy and /terms render 200 (GET and HEAD) unauthenticated, contain
      no leftover "<!--", and contain their document's H1
  (d) /terms is in PUBLIC_PATHS (/privacy already was, kept)
  (e) base.html's footer links both /privacy and /terms
  (f) consent.html links /privacy and no longer says the policy is pending
  (g) jobcannon.web.legal._render() calls the guard and raises at boot time
      (issue #94 guard-hardening review), against a sabotaged temp file —
      never against jobcannon/web/legal/ itself
  (h) /privacy and /terms send Cache-Control: private, max-age=300 on both
      GET and HEAD (issue #182 item 4), unchanged by auth state — private
      because ensure_session_ids() mints a per-visitor session cookie on
      first contact, AND (issue #205) because base.html's nav/footer now
      varies by real visitor identity on these routes even though each
      route's own BODY stays identity-independent — g.clerk_user is still
      unconditionally None here (see jobcannon/web/legal.py's Cache-Control
      comment for the full reasoning)
  (i) every PUBLIC_PATHS response carries Vary: Cookie and a private
      Cache-Control, via the shared after_request hook in
      jobcannon/web/__init__.py (issue #205) — covered in
      tests/host/test_public_cache_headers.py, not this file
  (j) /privacy and /terms render byte-identical document bodies for an
      anonymous vs. an authed visitor (issue #205's own claim, verified
      directly — closes review-1's Lens A(4) gap)
"""

from __future__ import annotations

import pathlib
import re

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


def test_guard_fires_on_mixed_case_bracket_placeholder():
    """The bracket rule used to be ALL-CAPS-only, so [Effective Date] /
    [Contact Email] slipped through both the guard and the import script's
    case-sensitive fill (issue #94 guard-hardening review)."""
    violations = check_published_text(_CLEAN_BASE + "Contact us at [Contact Email].")
    assert any("bracket placeholder" in v for v in violations)


def test_guard_does_not_false_positive_on_markdown_inline_link():
    text = _CLEAN_BASE + "See our [Privacy Policy](/privacy) for details.\n"
    assert check_published_text(text) == []


def test_guard_does_not_false_positive_on_markdown_reference_link():
    text = _CLEAN_BASE + "See our [Privacy Policy][ref] for details.\n\n[ref]: /privacy\n"
    assert check_published_text(text) == []


def test_guard_fires_on_raw_html_tag():
    violations = check_published_text(_CLEAN_BASE + "Click <script>alert(1)</script> to continue.")
    assert any("HTML tag" in v for v in violations)


def test_guard_does_not_false_positive_on_less_than_comparison():
    text = _CLEAN_BASE + "Latency must stay under 5 < 10 milliseconds for this check.\n"
    assert check_published_text(text) == []


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


def test_guard_fires_on_malformed_last_updated_value():
    """Effective date was validated but Last updated was not (issue #94
    guard-hardening review) — same YYYY-MM-DD check, but only when the line
    is present at all (it's optional, unlike Effective date)."""
    violations = check_published_text(_CLEAN_BASE + "**Last updated:** sometime soon\n")
    assert any("'Last updated:' line does not contain a YYYY-MM-DD date" in v for v in violations)


def test_guard_allows_well_formed_last_updated_line():
    text = _CLEAN_BASE + "**Last updated:** 2026-08-27\n"
    assert check_published_text(text) == []


def test_guard_does_not_require_a_last_updated_line():
    """Unlike Effective date, Last updated is optional — its absence alone
    is not a violation."""
    assert check_published_text(_CLEAN_BASE) == []


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


@pytest.mark.parametrize("path", ["/privacy", "/terms"])
def test_legal_page_head_request_200(path):
    """HEAD must resolve the same route as GET (Flask derives it
    automatically for a GET-only view) — a pre-signup visitor's browser or a
    link-checker HEAD-ing these before rendering must not see a 404/405."""
    app = _app(verify=lambda req: None)
    client = app.test_client()

    resp = client.head(path)

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# (h) Cache-Control (issue #182 item 4). `private`, not `public`: every
# request here — including this public-path branch — mints a per-visitor
# session cookie via ensure_session_ids() on first contact (see
# jobcannon/web/legal.py's Cache-Control comment for the full reasoning, and
# jobcannon/web/anon_session.py for the cookie itself). A `public` directive
# would let a shared cache (CDN, corporate proxy) replay one visitor's
# Set-Cookie response onto a different first-time visitor. Issue #205 adds a
# second, independent reason `private` must hold here: base.html's nav/
# footer now varies by real visitor identity on these two routes (the
# document BODY does not — see test (j) below), so a `public` directive
# would also let a shared cache replay one visitor's authed nav to another.
# `Vary: Cookie` (the shared after_request hook, jobcannon/web/__init__.py)
# is covered separately in tests/host/test_public_cache_headers.py, derived
# from PUBLIC_PATHS rather than repeated per-route here.
# Parametrized over GET and HEAD (issue #182 item 2 was HEAD-specific) and
# asserts the raw header string, not just the parsed cache_control
# attributes, so a stray extra directive would also be caught.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize("path", ["/privacy", "/terms"])
def test_legal_page_sets_private_cache_control_with_max_age(path, method):
    app = _app(verify=lambda req: None)
    client = app.test_client()

    resp = getattr(client, method)(path)

    assert resp.headers["Cache-Control"] == "private, max-age=300"


@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize("path", ["/privacy", "/terms"])
def test_legal_page_cache_control_same_when_authed(path, method):
    """The directive is a property of the route (`_legal_response`), not of
    the requester's auth state — an authed VERIFY_REQUEST stub must not
    change it. Issue #205: VERIFY_REQUEST DOES now run here, via
    `_visitor_is_anonymous()`'s PUBLIC_PATHS fallback (so base.html's nav
    varies — see test (j) below) — but that fallback only feeds
    `visitor_is_authed`, never `_legal_response`'s own header-setting code,
    so the header value pinned below is unaffected either way."""
    from jobcannon.web.auth import ClerkIdentity

    app = _app(verify=lambda req: ClerkIdentity(user_id="user_1", claims={"sub": "user_1"}))
    client = app.test_client()

    resp = getattr(client, method)(path)

    assert resp.headers["Cache-Control"] == "private, max-age=300"


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
    """consent.html no longer carries this copy directly (issue #182 split
    it into _consent_panel.html, shared with the post-grant/decline
    fragment response) -- assert BOTH that consent.html still pulls the
    panel in, and that the panel itself (the actual owner of this copy
    now) still links /privacy and says nothing about "pending"."""
    consent_src = pathlib.Path("jobcannon/web/templates/consent.html").read_text(encoding="utf-8")
    assert 'include "_consent_panel.html"' in consent_src

    panel_src = pathlib.Path("jobcannon/web/templates/_consent_panel.html").read_text(
        encoding="utf-8"
    )
    assert 'href="/privacy"' in panel_src
    assert "pending" not in panel_src.lower()


# ---------------------------------------------------------------------------
# (g) jobcannon.web.legal._render() calls the guard and raises at boot time,
# not only in the import script and the CI test above (issue #94
# guard-hardening review) — sabotages a throwaway temp file via the same
# _LEGAL_DIR seam _render() reads through, never jobcannon/web/legal/ itself.
# ---------------------------------------------------------------------------


def test_render_raises_on_guard_violation(tmp_path, monkeypatch):
    from jobcannon.web import legal

    bad_file = tmp_path / "bad.md"
    bad_file.write_text(
        "# Bad\n\n**Effective date:** 2026-08-27\n\nThis section is still TBD.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(legal, "_LEGAL_DIR", tmp_path)

    with pytest.raises(RuntimeError) as exc_info:
        legal._render("bad.md")
    assert "tbd" in str(exc_info.value).lower()


def test_render_passes_through_a_clean_file(tmp_path, monkeypatch):
    """Positive control for the sabotage test above: a clean file (same seam)
    must still render normally, not raise."""
    from jobcannon.web import legal

    good_file = tmp_path / "good.md"
    good_file.write_text(
        "# Good Title\n\n**Effective date:** 2026-08-27\n\nOrdinary published prose.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(legal, "_LEGAL_DIR", tmp_path)

    title, html = legal._render("good.md")
    assert title == "Good Title"
    assert "<h1>Good Title</h1>" in html


# ---------------------------------------------------------------------------
# (j) issue #205: the document BODY stays byte-identical across auth states
# even though the surrounding nav now varies (closes review-1's Lens A(4)
# gap — this claim was made in comments/docstrings but never actually
# tested before this PR). Extracts the exact region legal_page.html wraps
# body_html in (<div class="legal-prose">...</div>) — unambiguous because
# jobcannon.web.legal_guard rejects any raw HTML tag in the source markdown
# (test_guard_fires_on_raw_html_tag above), so body_html itself can never
# contain a nested <div>, and the template emits exactly one such div.
# ---------------------------------------------------------------------------

_LEGAL_PROSE_RE = re.compile(r'<div class="legal-prose">(.*?)</div>', re.DOTALL)


def _legal_body(html: str) -> str:
    match = _LEGAL_PROSE_RE.search(html)
    assert match, "legal-prose div not found in response body"
    return match.group(1)


@pytest.mark.parametrize(
    "path,h1_text",
    [
        ("/privacy", "Job Cannon — Privacy Policy"),
        ("/terms", "Job Cannon — Terms of Service"),
    ],
)
def test_legal_page_body_is_byte_identical_regardless_of_auth_state(path, h1_text):
    from jobcannon.web.auth import ClerkIdentity

    anon_resp = _app(verify=lambda req: None).test_client().get(path)
    authed_resp = (
        _app(verify=lambda req: ClerkIdentity(user_id="user_1", claims={"sub": "user_1"}))
        .test_client()
        .get(path)
    )
    assert anon_resp.status_code == 200
    assert authed_resp.status_code == 200

    anon_html = anon_resp.data.decode("utf-8")
    authed_html = authed_resp.data.decode("utf-8")
    anon_body = _legal_body(anon_html)
    authed_body = _legal_body(authed_html)

    # Anchor: the extracted region must actually contain the document, not
    # an empty/wrong match on both sides (which would make the equality
    # assertion below trivially, uselessly true).
    assert h1_text in anon_body
    assert h1_text in authed_body
    assert anon_body == authed_body
    # And the full page did vary — proves issue #205's nav fix is actually
    # live for this route, not that the whole response happens to be static.
    assert anon_html != authed_html
