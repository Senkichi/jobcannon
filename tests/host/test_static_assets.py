"""Anonymous visitors must be able to fetch self-hosted static assets.

The Living Journal adoption replaced CDN scripts with files served by
Flask's built-in `/static/<path>` route. That route is registered
unconditionally, is NOT in `jobcannon.web.PUBLIC_PATHS`, and — before the
endpoint exemption in clerk_auth's before_request gate — required a
session, so every signed-out visitor on the public pages (/demo, /start,
/preview, /privacy, /terms) got 401s for CSS/fonts/htmx and rendered
unstyled. No template-level test can catch that: it is a serving-layer
wiring gap, visible only by driving requests through the auth gate.

The asset list is DERIVED from the templates (every `url_for('static',
filename=...)` call site), never hardcoded, so a new asset added to
base.html is covered the moment it ships.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[2] / "jobcannon" / "web" / "templates"
_STATIC_REF_RE = re.compile(r"""url_for\(\s*['"]static['"]\s*,\s*filename\s*=\s*['"]([^'"]+)['"]""")


def _referenced_assets() -> list[str]:
    refs: set[str] = set()
    for path in sorted(TEMPLATES.glob("**/*.html")):
        refs.update(_STATIC_REF_RE.findall(path.read_text(encoding="utf-8")))
    return sorted(refs)


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


@pytest.mark.parametrize("filename", _referenced_assets())
def test_anonymous_visitor_can_fetch_referenced_asset(filename):
    client = _app().test_client()
    resp = client.get(f"/static/{filename}")
    assert resp.status_code == 200, f"/static/{filename} -> {resp.status_code}"
    # Asset fetches must never set cookies: clerk_auth's static-endpoint
    # branch skips ensure_session_ids()/capture_attribution() on purpose.
    assert "Set-Cookie" not in resp.headers


def test_missing_static_file_is_404_not_401():
    resp = _app().test_client().get("/static/does-not-exist.css")
    assert resp.status_code == 404


def test_non_public_routes_still_gated():
    # Control: the endpoint exemption must not widen beyond /static.
    resp = _app().test_client().get("/account/delete")
    assert resp.status_code == 401
