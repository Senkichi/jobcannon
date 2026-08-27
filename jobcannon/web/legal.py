"""jobcannon/web/legal.py — GET /privacy and GET /terms: the ratified
privacy policy and terms of service (issue #94), replacing the prior
scaffold-only placeholder (jobcannon.web.privacy, removed).

Public — both routes are in jobcannon.web.PUBLIC_PATHS. The footer links in
base.html are unconditional (they render on error_401.html too, same as the
AGPL source link added for issue #22), and a visitor deciding whether to
sign up or grant analytics consent needs to be able to read these before
creating an account, not after. Making either route authed-only would 401
the very link a pre-signup visitor clicks.

The published text lives in two committed markdown files under
jobcannon/web/legal/ (privacy.md, terms.md) — NOT in this module and not in
any template. They are generated artifacts: scripts/import_legal_text.py
mechanically strips drafting/review matter out of a ratified source draft
and writes them; jobcannon.web.legal_guard.check_published_text is the
standing structural gate — at import time in that script, as a
committed-file check in tests/host/test_legal_pages.py, AND (below,
`_render`) at this module's own import time, so a hand-edited or corrupted
.md fails app boot instead of silently serving. Never hand-edit either .md
file — the next import overwrites it.

Markdown -> HTML happens ONCE at import time, not per request: the source
files are committed, ratified text, not per-request user input, so
re-rendering them on every GET buys nothing and only risks a slow render
touching request latency. Publishing a new version means re-running the
import script and restarting the process — there is no hot-reload seam
here, deliberately, for the same reason the text isn't templated: a
mid-request reload of legal text is a correctness hazard (a visitor could
read half-old, half-new text within one response) that a cold restart does
not have.

Resolves both .md files relative to THIS module's own directory
(pathlib.Path(__file__).parent / "legal"), not the process's current
working directory, so the route behaves identically whether started via
`python -m jobcannon`, gunicorn, or a test runner invoked from any cwd.
Note: jobcannon/web/legal.py (this file) and jobcannon/web/legal/ (the data
directory) coexisting as siblings relies on the data directory having no
__init__.py — if one is ever added, the directory becomes the `legal`
submodule and this file's import silently breaks. Don't add one.
"""

from __future__ import annotations

import pathlib
import re

import markdown
from flask import Blueprint, Response, make_response, render_template

from jobcannon.web.legal_guard import check_published_text

legal_bp = Blueprint("legal", __name__)

_LEGAL_DIR = pathlib.Path(__file__).parent / "legal"
_MD_EXTENSIONS = ["tables", "sane_lists"]
_H1_LINE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

# issue #182 item 4: `private`, not `public` — but NOT because the nav
# varies with auth state on these two routes specifically; it doesn't.
# /privacy and /terms are both in PUBLIC_PATHS, and clerk_auth's
# before_request hook (jobcannon/web/__init__.py) unconditionally sets
# `g.clerk_user = None` and returns before app.config["VERIFY_REQUEST"] is
# ever called for any PUBLIC_PATHS request — so base.html's auth-gated nav
# and footer links always render the signed-out variant here, regardless of
# the requester's real session (verified: an authed VERIFY_REQUEST stub is
# never invoked on these routes).
#
# `private` matters for a different, real reason: `ensure_session_ids()`
# (jobcannon/web/anon_session.py), called on every request including this
# public-path branch, mints a per-visitor anon_session_id into a signed
# Set-Cookie session cookie on first contact. A shared cache (Cloudflare in
# front of jobcannon.dev, a corporate proxy) that stored and replayed one
# visitor's response would hand a different, distinct first-time visitor
# that same Set-Cookie (or its absence, on a later cache hit), silently
# corrupting per-visitor session/attribution tracking. `private` gets the
# intended win (a visitor's own browser skips refetching identical bytes on
# repeat GETs within max-age) without authorizing that cross-visitor reuse.
# No Vary header is needed alongside it: `private` already forbids a shared
# cache from storing the response at all, and (per the paragraph above)
# there is no auth-state variance here for a Vary header to key on. 300s
# bounds how stale a re-publish (re-run the importer + restart) can look to
# a browser that already cached the previous version.
_LEGAL_CACHE_MAX_AGE_S = 300


def _legal_response(title: str, html: str) -> Response:
    response = make_response(render_template("legal_page.html", title=title, body_html=html))
    response.cache_control.private = True
    response.cache_control.max_age = _LEGAL_CACHE_MAX_AGE_S
    return response


def _render(filename: str) -> tuple[str, str]:
    """Return (title, html) for one committed markdown file. `title` is the
    document's own H1 text, used for the browser-tab <title> — the H1 that
    actually appears on the page comes from `html` itself (markdown renders
    the source '# ...' line as a real <h1>), so there is no second,
    separately-authored heading to keep in sync with it.

    Calls the SAME `check_published_text` guard that
    `scripts/import_legal_text.py` runs before it ever writes the file and
    that `tests/host/test_legal_pages.py` runs as a standing CI gate against
    the committed file — enforcement previously lived only in those two
    places, so a hand-edited .md that reintroduced drafting/review matter
    (or a corrupted commit) would boot the app and serve it. Raising here
    instead means a bad file fails app boot rather than serving."""
    raw = (_LEGAL_DIR / filename).read_text(encoding="utf-8")
    violations = check_published_text(raw)
    if violations:
        raise RuntimeError(f"legal_guard rejected {filename} at boot: " + "; ".join(violations))
    match = _H1_LINE.search(raw)
    title = match.group(1) if match else filename
    html = markdown.markdown(raw, extensions=_MD_EXTENSIONS)
    return title, html


_PRIVACY_TITLE, _PRIVACY_HTML = _render("privacy.md")
_TERMS_TITLE, _TERMS_HTML = _render("terms.md")


@legal_bp.get("/privacy", strict_slashes=False)
def privacy():
    return _legal_response(_PRIVACY_TITLE, _PRIVACY_HTML)


@legal_bp.get("/terms", strict_slashes=False)
def terms():
    return _legal_response(_TERMS_TITLE, _TERMS_HTML)
