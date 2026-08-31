"""Anonymous visitors must be able to fetch self-hosted static assets.

The Living Journal adoption replaced CDN scripts with files served by
Flask's built-in `/static/<path>` route. That route is registered
unconditionally, is NOT in `jobcannon.web.PUBLIC_PATHS`, and — before the
endpoint exemption in clerk_auth's before_request gate — required a
session, so every signed-out visitor on the public pages (/demo, /start,
/preview, /privacy, /terms) got 401s for CSS/fonts/htmx and rendered
unstyled. No template-level test can catch that: it is a serving-layer
wiring gap, visible only by driving requests through the auth gate.

The asset list is DERIVED from the templates (every `static_url(...)` or
`url_for('static', filename=...)` call site), never hardcoded, so a new
asset added to base.html is covered the moment it ships.

Issue #258 adds two more things this module must catch without a
hardcoded list: every `/static/*` response must carry the new
`public, max-age=31536000, immutable` Cache-Control (else a stale asset
would ship for up to a year), and `fonts.css`'s served body must carry a
`?v=` on each of its OWN internal font `url(...)` refs — those refs are a
level of indirection below every `static_url()`/`url_for` call site, so
the regex above cannot see them; they're derived instead by scanning
`fonts.css` itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[2] / "jobcannon" / "web" / "templates"
STATIC = Path(__file__).resolve().parents[2] / "jobcannon" / "web" / "static"
_STATIC_REF_RE = re.compile(
    r"""static_url\(\s*['"]([^'"]+)['"]"""
    r"""|url_for\(\s*['"]static['"]\s*,\s*filename\s*=\s*['"]([^'"]+)['"]"""
)
_FONTS_CSS_URL_RE = re.compile(r"url\('(fonts/[^']+)'\)")


def _referenced_assets() -> list[str]:
    refs: set[str] = set()
    for path in sorted(TEMPLATES.glob("**/*.html")):
        for static_url_match, url_for_match in _STATIC_REF_RE.findall(
            path.read_text(encoding="utf-8")
        ):
            refs.add(static_url_match or url_for_match)
    return sorted(refs)


def _fonts_css_font_refs() -> list[str]:
    return sorted(
        set(_FONTS_CSS_URL_RE.findall((STATIC / "fonts.css").read_text(encoding="utf-8")))
    )


def _app():
    from jobcannon.web import create_app

    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": lambda req: None,  # every request is signed out
            "WEBHOOK_SECRET": "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q=",
        }
    )


def test_template_asset_scan_is_not_vacuous():
    # Positive control: base.html links 3 stylesheets + htmx. If the regex
    # rots, this fails before the parametrized test silently passes on [].
    assert len(_referenced_assets()) >= 4, _referenced_assets()


def test_fonts_css_ref_scan_is_not_vacuous():
    # Positive control for _fonts_css_font_refs(): fonts.css defines 3
    # @font-face rules today. If the regex rots, this fails before
    # test_fonts_css_response_versions_its_font_refs silently passes on [].
    assert len(_fonts_css_font_refs()) == 3, _fonts_css_font_refs()


@pytest.mark.parametrize("filename", _referenced_assets())
def test_anonymous_visitor_can_fetch_referenced_asset(filename):
    client = _app().test_client()
    resp = client.get(f"/static/{filename}")
    assert resp.status_code == 200, f"/static/{filename} -> {resp.status_code}"
    # Asset fetches must never set cookies: clerk_auth's static-endpoint
    # branch skips ensure_session_ids()/capture_attribution() on purpose.
    assert "Set-Cookie" not in resp.headers
    # Issue #258: every static asset gets a uniform, year-long, immutable
    # cache policy — safe only because static_url() busts the URL whenever
    # the file's bytes change (asserted separately below for fonts.css's
    # own internal refs, which no url_for/static_url call site can see).
    cache_control = resp.cache_control
    assert cache_control.public is True, resp.headers.get("Cache-Control")
    assert cache_control.max_age == 31536000, resp.headers.get("Cache-Control")
    assert cache_control.immutable is True, resp.headers.get("Cache-Control")
    assert not cache_control.no_cache, resp.headers.get("Cache-Control")


def test_fonts_css_response_versions_its_font_refs():
    # fonts.css's own internal url('fonts/X.woff2') refs are a level of
    # indirection below every static_url() call site, so nothing above
    # exercises them. Without this, a deliberate font re-subset would
    # silently ship under the SAME url() the immutable, year-long cache
    # already pinned to the old bytes.
    client = _app().test_client()
    resp = client.get("/static/fonts.css")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    for ref in _fonts_css_font_refs():
        assert f"{ref}?v=" in body, body


def test_missing_static_file_is_404_not_401():
    resp = _app().test_client().get("/static/does-not-exist.css")
    assert resp.status_code == 404
    # Issue #258 regression guard: request.endpoint is still "static" on a
    # 404 (the URL rule matched; send_static_file just found nothing), so
    # an unguarded _cache_static_assets hook would stamp a year-long
    # immutable Cache-Control onto a "missing" response.
    assert not resp.cache_control.immutable, resp.headers.get("Cache-Control")
    assert not resp.cache_control.public, resp.headers.get("Cache-Control")


def test_non_public_routes_still_gated():
    # Control: the endpoint exemption must not widen beyond /static.
    resp = _app().test_client().get("/account/delete")
    assert resp.status_code == 401
