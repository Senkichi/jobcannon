"""GET / (authed feed shell) and GET /demo (public guest demo) — the day-1-
stranger prerequisites (1B Wave 3 PR 11): layout + designed empty states on
a minimal web shell. Hard 1C boundary: no posting list, no ranked feed, no
filters, no watchlist/pipeline UI, no `posting_impression` events — the
demo page shows corpus COUNTS, never postings. Picker-first onboarding
(GET/POST /start) lives in jobcannon/web/onboarding.py, not here.

`corpus_stats` / `get_profile` / `connection_factory` are imported at MODULE
level (unlike `jobcannon/web/__init__.py`'s `_resolve_consent`, which does an
inline `from jobcannon.db import _events` import inside the function body)
specifically so tests can monkeypatch `jobcannon.web.pages.corpus_stats`,
`.get_profile`, and `.connection_factory` directly as module attributes —
an inline import re-fetches the real function on every call and would not
be patchable that way.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, g, render_template

from jobcannon.db._profiles import GUEST_USER_ID, get_profile
from jobcannon.db._stats import corpus_stats
from jobcannon.db.pool import connection_factory

logger = logging.getLogger(__name__)

pages_bp = Blueprint("pages", __name__)

_EMPTY_STATS = {"postings": 0, "companies": 0, "freshest_last_seen": None}


def _read_page_data(user_id: str) -> tuple[dict, Any]:
    """Fail-closed page-data read, mirroring `_resolve_consent`'s shape
    (jobcannon/web/__init__.py): any DB error — an unopened connection pool
    (TESTING, the same state as tests/host/test_auth.py's identity-only
    tests) or a genuine outage — degrades to the corpus-empty / no-profile
    branch rather than surfacing as a 500."""
    try:
        with connection_factory() as conn:
            return corpus_stats(conn), get_profile(conn, user_id)
    except Exception:
        logger.warning(
            "page data read failed for user %s (defaulting to corpus-empty)",
            user_id,
            exc_info=True,
        )
        return dict(_EMPTY_STATS), None


@pages_bp.get("/", strict_slashes=False)
def feed():
    stats, profile = _read_page_data(g.clerk_user.user_id)
    return render_template("feed.html", stats=stats, profile=profile)


@pages_bp.get("/demo", strict_slashes=False)
def demo():
    stats, profile = _read_page_data(GUEST_USER_ID)
    return render_template("demo.html", stats=stats, profile=profile)
