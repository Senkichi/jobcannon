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
standing structural gate (both at import time in that script, and as a
committed-file check in tests/host/test_legal_pages.py) against
non-publication matter surviving into what ships. Never hand-edit either
.md file — the next import overwrites it.

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
from flask import Blueprint, render_template

legal_bp = Blueprint("legal", __name__)

_LEGAL_DIR = pathlib.Path(__file__).parent / "legal"
_MD_EXTENSIONS = ["tables", "sane_lists"]
_H1_LINE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _render(filename: str) -> tuple[str, str]:
    """Return (title, html) for one committed markdown file. `title` is the
    document's own H1 text, used for the browser-tab <title> — the H1 that
    actually appears on the page comes from `html` itself (markdown renders
    the source '# ...' line as a real <h1>), so there is no second,
    separately-authored heading to keep in sync with it."""
    raw = (_LEGAL_DIR / filename).read_text(encoding="utf-8")
    match = _H1_LINE.search(raw)
    title = match.group(1) if match else filename
    html = markdown.markdown(raw, extensions=_MD_EXTENSIONS)
    return title, html


_PRIVACY_TITLE, _PRIVACY_HTML = _render("privacy.md")
_TERMS_TITLE, _TERMS_HTML = _render("terms.md")


@legal_bp.get("/privacy", strict_slashes=False)
def privacy():
    return render_template("legal_page.html", title=_PRIVACY_TITLE, body_html=_PRIVACY_HTML)


@legal_bp.get("/terms", strict_slashes=False)
def terms():
    return render_template("legal_page.html", title=_TERMS_TITLE, body_html=_TERMS_HTML)
