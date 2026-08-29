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

import pytest
from bs4 import BeautifulSoup

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
# (k) issue #253: the browser-tab <title> must carry the "Job Cannon" brand
# exactly once. Both terms.md and privacy.md's own H1 already reads
# "Job Cannon — <Page Name>" (ratified legal text — the on-page H1 itself,
# asserted via h1_text above, is untouched by this fix and correctly keeps
# that brand prefix). Before the fix, legal.py's _render() passed that whole
# H1 string through as `title`, and legal_page.html's `{% block title %}`
# unconditionally appended its own "— Job Cannon" suffix (the same suffix
# every other templated page's block title appends to a page-specific
# fragment — see e.g. feed.html, demo.html), doubling the brand into
# "Job Cannon — Terms of Service — Job Cannon". Parsed with BeautifulSoup
# rather than string-matched so this pins the actual <title> element, not
# an incidental substring elsewhere in the response.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected_title",
    [
        ("/privacy", "Privacy Policy — Job Cannon"),
        ("/terms", "Terms of Service — Job Cannon"),
    ],
)
def test_legal_page_title_tag_has_brand_exactly_once(path, expected_title):
    app = _app(verify=lambda req: None)
    client = app.test_client()

    resp = client.get(path)

    soup = BeautifulSoup(resp.data.decode("utf-8"), "html.parser")
    title_text = soup.title.string
    assert title_text == expected_title
    assert title_text.count("Job Cannon") == 1


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


def test_render_strips_brand_prefix_from_title_but_not_from_h1(tmp_path, monkeypatch):
    """issue #253 unit-level pin: an H1 that starts with the "Job Cannon — "
    brand prefix (the real shape of both terms.md and privacy.md) must have
    that prefix stripped from the extracted `title` — legal_page.html's
    `{% block title %}` appends its own "— Job Cannon" suffix, same as every
    other templated page, so a `title` that already embeds the brand would
    double it. The rendered `html` (what becomes the on-page <h1>) must keep
    the brand prefix untouched — this fix only changes what's extracted for
    the browser-tab title, not the document body."""
    from jobcannon.web import legal

    branded_file = tmp_path / "branded.md"
    branded_file.write_text(
        "# Job Cannon — Sample Policy\n\n**Effective date:** 2026-08-27\n\nOrdinary published prose.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(legal, "_LEGAL_DIR", tmp_path)

    title, html = legal._render("branded.md")
    assert title == "Sample Policy"
    assert "<h1>Job Cannon — Sample Policy</h1>" in html


# ---------------------------------------------------------------------------
# (j) issue #205: the document BODY stays byte-identical across auth states
# even though the surrounding nav now varies (closes review-1's Lens A(4)
# gap — this claim was made in comments/docstrings but never actually
# tested before this PR). Extracts the exact region legal_page.html wraps
# body_html in (<div class="legal-prose">...</div>).
#
# Parsed with BeautifulSoup, not a regex, since issue #229's fix
# (jobcannon/web/legal.py's `_wrap_tables_for_scroll`) wraps every rendered
# <table> in its own <div class="table-scroll">, so body_html now DOES
# contain nested <div>s. A prior version of this helper used a non-greedy
# regex (`<div class="legal-prose">(.*?)</div>`) that relied on body_html
# never nesting a <div> — true before #229, false now — and would have
# silently truncated at the FIRST </div> (the wrapper's own close tag)
# instead of .legal-prose's true close tag, weakening this test to only
# compare the prefix up to the first table rather than the whole body. A
# real parser finds the true matching close tag regardless of nesting
# depth, so this now checks the ENTIRE body, not a truncated prefix.
# ---------------------------------------------------------------------------


def _legal_body(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    div = soup.find("div", class_="legal-prose")
    assert div, "legal-prose div not found in response body"
    return div.decode_contents()


# ---------------------------------------------------------------------------
# Response-boundary closure (re-review, LOW): check_published_text above
# only inspects the committed .md SOURCE -- it has no visibility into
# jobcannon/web/templates/legal_page.html, which renders unconditionally
# alongside that source on every /privacy and /terms response. A re-review
# of issue #229's table-scroll fix found FORBIDDEN_PHRASES matter (the
# words "draft", "pr #", "issue #") living directly in that template's own
# <style> comments -- exactly the class of content the guard exists to keep
# off these pages, reaching real visitors through a chokepoint the guard was
# never wired to check (fixed separately, in legal_page.html itself).
#
# Scoped to the <style> block + the .legal-prose body, NOT the whole
# response: base.html's own <script> comment ("// ... Account Portal
# sign-in redirect (issue #149) ...") is a legitimate, unrelated hit that
# renders on every page a visitor loads while signed out of Clerk --
# Jinja's `{# ... #}` comments are stripped server-side, but a `<script>`
# block's `//` comment is not, so that text is genuinely part of the served
# HTML. Scoping the assertion here (rather than dropping "issue #"/"pr #"
# from FORBIDDEN_PHRASES) keeps that legitimate, pre-existing content out of
# scope without weakening what counts as a violation anywhere it actually
# matters -- namely, this template's own <style> block and body.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/privacy", "/terms"])
def test_served_legal_page_style_and_body_contain_no_forbidden_phrases(path):
    app = _app(verify=lambda req: None)
    client = app.test_client()

    resp = client.get(path)
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    soup = BeautifulSoup(html, "html.parser")

    style_tag = soup.find("style")
    assert style_tag, (path, "no <style> tag in the served page")
    scoped_text = (style_tag.get_text() + "\n" + _legal_body(html)).lower()

    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in scoped_text, (
            path,
            phrase,
            "drafting/review matter leaked through legal_page.html's "
            "<style> block or .legal-prose body -- check_published_text "
            "only inspects the committed .md source, not this template",
        )


def test_served_legal_page_forbidden_phrase_check_detects_a_reintroduced_comment():
    """Sabotage-verify the check above actually catches something, rather
    than passing vacuously because today's template happens to be clean:
    re-inject a stray 'PR #' review-note comment into a REAL served
    response (not a hand-built string) and confirm the SAME scoping logic
    fires on it."""
    app = _app(verify=lambda req: None)
    client = app.test_client()
    resp = client.get("/privacy")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")

    sabotaged = html.replace("</style>", "/* leftover PR #999 review note */</style>", 1)
    assert sabotaged != html, "no </style> tag found in the response -- fix this sabotage fixture"
    soup = BeautifulSoup(sabotaged, "html.parser")
    style_tag = soup.find("style")
    assert style_tag, "no <style> tag found in the sabotaged response"
    scoped_text = (style_tag.get_text() + "\n" + _legal_body(sabotaged)).lower()

    assert any(phrase in scoped_text for phrase in FORBIDDEN_PHRASES), (
        "sabotaging a 'PR #' comment into the served <style> block must "
        "make the forbidden-phrase check fail -- if this assertion fails, "
        "test_served_legal_page_style_and_body_contain_no_forbidden_phrases "
        "would silently pass a template that leaked review matter"
    )


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
