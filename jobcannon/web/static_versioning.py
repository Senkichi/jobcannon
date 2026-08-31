"""jobcannon/web/static_versioning.py — boot-time content hashing for
`/static/*` cache-busting (issue #258).

No build step exists anywhere in this repo (no `package.json`, no asset
pipeline — confirmed by grep in the issue #258 recon) — every file under
`jobcannon/web/static/` is committed source, served as-is. Pairing that
with the new `public, max-age=31536000, immutable` `Cache-Control` policy
(`jobcannon/web/__init__.py`'s `_cache_static_assets` hook) means a browser
would otherwise cache a stale asset for up to a year past any edit, since
nothing in the URL changes when the file does. This module computes a
short content hash per file, once, at `create_app()` boot, so
`?v=<hash>` on each asset URL invalidates the browser cache exactly when
the bytes actually change — the same content-addressed-URL pattern
Django's `ManifestStaticFilesStorage`, webpack, and Next.js's
`_next/static` all use, adapted to a query param (not a renamed file)
because there's no build step to produce the renamed copies.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_HASH_LEN = 10

# Matches fonts.css's own internal `url('fonts/X.woff2')` references — the
# one static file whose bytes reference other static files. See
# `versioned_fonts_css`'s docstring for why these need their own busting.
_FONTS_CSS_URL_RE = re.compile(r"url\('(fonts/[^']+)'\)")


def compute_static_hashes(static_folder: str | Path) -> dict[str, str]:
    """Return `{relative_posix_path: sha256(bytes)[:10]}` for every regular
    file under `static_folder`, keyed exactly as `url_for('static',
    filename=...)` expects (forward-slash-separated, relative to the
    static root) — so the same dict serves both `static_url()`'s `?v=`
    lookups and `versioned_fonts_css`'s internal-ref rewriting below.

    Computed fresh on every call (once per `create_app()` boot) rather
    than cached at import time: tests call `create_app()` many times
    against the same on-disk files, and nothing here is expensive enough
    (a few dozen small files) to warrant cross-instance caching.
    """
    root = Path(static_folder)
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()[:_HASH_LEN]
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def versioned_fonts_css(fonts_css_path: str | Path, hashes: dict[str, str]) -> bytes:
    """Return `fonts.css`'s bytes with each internal `url('fonts/X.woff2')`
    rewritten to `url('fonts/X.woff2?v=<hash-of-X>')`.

    `fonts.css` is the only static file whose OWN bytes reference other
    static files, via relative `url()` paths the 6 `url_for('static', ...)`
    template call sites never see. Versioning only those 6 call sites
    (`static_url()` in `base.html`) busts `fonts.css`'s own URL when
    *fonts.css* changes, but never busts the 3 woff2 URLs *inside* it — so
    a font re-subset would silently keep serving old cached bytes for up to
    a year, because the URL identifying them never changed. This closes
    that gap by hashing the referenced font files themselves and folding
    the result into the CSS text served for `fonts.css`.

    Never writes back to `fonts_css_path` — the committed file on disk is
    untouched; only the in-memory bytes handed to the client are rewritten
    (see `_JCFlask.send_static_file` in `jobcannon/web/__init__.py`, which
    special-cases `fonts.css` to serve this function's output instead of
    delegating to Werkzeug's `send_from_directory`).
    """
    text = Path(fonts_css_path).read_text(encoding="utf-8")

    def _sub(match: re.Match[str]) -> str:
        rel = match.group(1)
        digest = hashes.get(rel)
        if digest is None:
            # A ref fonts.css carries to a file that isn't (or is no longer)
            # on disk under static_folder -- leave it exactly as written
            # rather than emit a "?v=None" URL. compute_static_hashes walks
            # the real filesystem, so this only fires if fonts.css and the
            # static tree have drifted out of sync with each other.
            return match.group(0)
        return f"url('{rel}?v={digest}')"

    return _FONTS_CSS_URL_RE.sub(_sub, text).encode("utf-8")
