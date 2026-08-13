"""Shared concurrency bounds/clamps for ATS platform scanning (issues #1028-1030).

Workday, SmartRecruiters, and Oracle Cloud each fetch page 1 serially to
learn the total, then fan the remaining pages out across a bounded
``ThreadPoolExecutor``. All three read the same ``ats.page_fetch_concurrency``
config knob and clamp it to the same range, so a single shared implementation
is used instead of one copy per platform file — the clamp ceiling already had
to be corrected (8 -> 6) across all three copies in lockstep once during
review, which is exactly the drift risk a shared helper avoids.

``get_scan_concurrency`` (issue #1030) governs the outermost layer: how many
companies (boards) Phase A scans concurrently. ``HOST_PACING_LIMIT`` bounds
how many of those concurrent company-workers may hit the *same* API host at
once — see ``_http_session.py`` for where it's enforced.
"""

from __future__ import annotations

from typing import Final

from jobcannon.engine.runtime_config import get_runtime_config

DEFAULT_PAGE_FETCH_CONCURRENCY: Final = 4
_MIN_PAGE_FETCH_CONCURRENCY: Final = 1
_MAX_PAGE_FETCH_CONCURRENCY: Final = 6

# Board-level scan concurrency (issue #1030). Default 1 preserves today's
# strictly serial Phase A loop byte-for-byte (including the 0.5s inter-company
# sleep). Ceiling matches page_fetch_concurrency / detail_fetch_concurrency's
# range (1-6) for the same shared-resource-budget reason documented on
# get_page_fetch_concurrency below: these knobs all draw from the one pooled
# Session (pool_maxsize=16 in _http_session.py). See the PR description for
# this issue's stacking-math note on why 6 is a deliberately conservative
# ceiling for the outermost multiplier rather than a tight bin-pack of 16.
DEFAULT_SCAN_CONCURRENCY: Final = 1
_MIN_SCAN_CONCURRENCY: Final = 1
_MAX_SCAN_CONCURRENCY: Final = 6

# Per-host request pacing (issue #1030). A handful of platforms (Greenhouse's
# boards-api.greenhouse.io alone backs ~571 boards) share one API host across
# many companies; without a gate, raising scan_concurrency fans every one of
# those companies' requests out to the same host simultaneously. This is a
# small fixed safety constant (not a config knob — the issue doesn't ask for
# one, and unlike scan_concurrency it isn't a per-deployment throughput
# trade-off) enforced once at the shared Session in _http_session.py, keyed on
# the real outgoing request's host rather than a hand-maintained
# platform-name -> host table that would drift as platforms are added.
HOST_PACING_LIMIT: Final = 3


def get_page_fetch_concurrency() -> int:
    """Read page-fetch concurrency from config, clamped to a sane range.

    Returns:
        The configured ``ats.page_fetch_concurrency`` value, floored at 1
        (operators must be able to throttle to 1 during vendor rate-limit
        incidents) and capped at 6 (matching ``detail_fetch_concurrency`` in
        ``_registry.py`` and the shared pooled ``Session``'s ``maxsize=16``
        in ``_http_session.py``). Page-fetch and detail-fetch are sequential
        phases within a single company's scan, not concurrent with each
        other — the pooled Session is only stressed when multiple
        companies' scans run concurrently within the reconciler job, so the
        two knobs sharing a ceiling is a shared-resource budget, not a
        same-phase overlap.

        Falls back to :data:`DEFAULT_PAGE_FETCH_CONCURRENCY` when no host
        runtime-config provider is registered (e.g. called from a script or
        test) or the configured value is not a valid int.
    """
    try:
        concurrency = (
            get_runtime_config()
            .get("ats", {})
            .get("page_fetch_concurrency", DEFAULT_PAGE_FETCH_CONCURRENCY)
        )
        return max(
            _MIN_PAGE_FETCH_CONCURRENCY,
            min(_MAX_PAGE_FETCH_CONCURRENCY, int(concurrency)),
        )
    except (RuntimeError, AttributeError, TypeError, ValueError):
        # No provider registered or invalid config: use default
        return DEFAULT_PAGE_FETCH_CONCURRENCY


def get_scan_concurrency(config: dict) -> int:
    """Read Phase A board-level scan concurrency from config, clamped to range.

    Unlike :func:`get_page_fetch_concurrency` (which reads via the host's
    injected runtime-config provider because the platform-scanner modules
    that call it have no ``config`` dict threaded into their call chain),
    ``run_ats_scan`` already receives an explicit ``config`` dict as a
    parameter — so this takes one directly rather than adding a runtime-
    config-provider dependency to a code path that doesn't otherwise need
    one (it also runs under the scheduler outside any request context).

    Args:
        config: Application config dict (same one ``run_ats_scan`` receives).

    Returns:
        The configured ``ats.scan_concurrency`` value, floored at 1 (serial —
        operators must always be able to fall back to today's behavior) and
        capped at 6 (matching ``page_fetch_concurrency`` /
        ``detail_fetch_concurrency``'s ceiling; see the module docstring for
        the shared-pool rationale). Falls back to
        :data:`DEFAULT_SCAN_CONCURRENCY` when the configured value is not a
        valid int.
    """
    try:
        raw = config.get("ats", {}).get("scan_concurrency", DEFAULT_SCAN_CONCURRENCY)
        return max(_MIN_SCAN_CONCURRENCY, min(_MAX_SCAN_CONCURRENCY, int(raw)))
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_SCAN_CONCURRENCY
