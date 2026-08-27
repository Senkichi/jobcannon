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

# issue #182 item 4, corrected by issue #205: `private`, not `public`.
# /privacy and /terms are both in PUBLIC_PATHS, and clerk_auth's
# before_request hook (jobcannon/web/__init__.py) unconditionally sets
# `g.clerk_user = None` before the view runs — so THIS FUNCTION's own
# render (title, html, the document body) is identity-independent and
# byte-identical for every visitor, verified by
# tests/host/test_legal_pages.py's byte-identity test. That is still true
# and still the reason the route itself needs no per-visitor branch.
#
# What is NOT true anymore (issue #205): the FULL response base.html
# produces — nav, "My postings", footer Export/Delete — is not identity-
# independent. `_visitor_is_anonymous()`'s PUBLIC_PATHS fallback now calls
# app.config["VERIFY_REQUEST"] for real (jobcannon/web/__init__.py), so a
# signed-in visitor gets the authed nav on these routes while the document
# body underneath stays the same. A future maintainer relaxing `private`
# to `public` on the strength of the old "no auth variance" claim would
# leak one visitor's authed nav into a shared cache's copy for another.
#
# `private` forecloses that: it forbids ANY shared cache (Cloudflare in
# front of jobcannon.dev, a corporate proxy) from storing or replaying this
# response across visitors at all, nav included. `Vary: Cookie` is
# defense-in-depth on top of that for any cache that ignores `private` --
# guaranteed explicitly by the shared after_request hook in
# jobcannon/web/__init__.py (keyed off the same PUBLIC_PATHS membership via
# `_is_public_request_path()`), though Flask's own session machinery
# already emits it here too (ensure_session_ids() reads the session on
# every request, which trips save_session()'s own `Vary: Cookie` add) --
# the hook exists so that guarantee doesn't depend on that incidental
# behavior continuing.
# Together — not nav invariance — is what makes cross-visitor leakage here
# impossible. `private` also still serves its original, unrelated reason:
# `ensure_session_ids()` (jobcannon/web/anon_session.py) mints a per-visitor
# anon_session_id Set-Cookie on first contact, and a shared cache replaying
# one visitor's Set-Cookie to another would corrupt per-visitor
# session/attribution tracking regardless of nav content. 300s bounds how
# stale a re-publish (re-run the importer + restart) can look to a browser
# that already cached the previous version.
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
