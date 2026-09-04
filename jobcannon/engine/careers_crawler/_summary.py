# PORTED from job_finder/web/careers_crawler/__init__.py @ 46d5a2a1f27179d075efc5572efeded3ba2a0266 (private job-cannon). Ledger L-0461.
"""Summary-dict shape shared by the careers crawler's orchestrator and workers.

Split out of ``__init__.py`` (design note PR-4) as the single source of
truth for the ``careers_crawl`` summary schema, used by the TESTING-skip
return, the top-level orchestrator summary, and each per-worker
``local_summary`` in ``_escalation.py``.

# PORT-SEAM: the four ``ai_nav_*`` keys (``ai_navigated``, ``ai_replayed``,
# ``ai_nav_attempts``, ``ai_nav_failures``) and the ``ai_nav_failure_reasons``
# dict field are DROPPED. ``ai_career_navigator`` DIES (ledger L-0133) --
# there is no AI-navigation tier in this port, so nothing ever populates
# these fields. ``_merge_ai_nav_failure_reasons`` (the private helper that
# merged the per-reason dict across workers) is dropped entirely along with
# them -- it has no callers once the dict field it merges is gone.
"""

from __future__ import annotations

_SUMMARY_KEYS = [
    "companies_crawled",
    "jobs_found",
    "jobs_new",
    "scored",
    "classified_apply",
    "classified_consider",
    "classified_skip",
    "classified_reject",
    "playwright_rendered",
    "interactive",
    "api_cached",
    "url_param_hits",
    "sitemap_hits",
    "embedded_json_hits",
    "ats_link_promoted",
    "legitimacy_flagged",
]


def _new_summary() -> dict:
    """Return a zero-filled summary dict matching the careers_crawl schema."""
    return {**dict.fromkeys(_SUMMARY_KEYS, 0), "errors": []}
