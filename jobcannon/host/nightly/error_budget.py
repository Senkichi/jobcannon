"""ADAPTED from job_finder/web/nightly_monitor/_error_budget.py
@ b34eb0b00a13c0bf0c9c95020ceddfeca71022b8 (private job-cannon). Ledger L-0387.

Nightly error-budget digest: overnight WARNING/ERROR-level scan_health_log
rows, aggregated per source and by structured kind -- the same "signature
pattern" concept private's signatures.py used, re-based on structured jsonb
fields instead of regex-matched free text (MEMORY
feedback_read_structured_dont_reparse_freetext).

# PORT-SEAM: private read a single un-rotated app.log (the #2020 bug this
# port does not need to re-fix: nothing here reads a file at all) plus
# run_events.jsonl, and aggregated multiple past nights' summary.json files
# under a monitor_root directory tree to build new_patterns/trend arrays.
# None of that filesystem substrate exists on this host, so this module is
# a much smaller rewrite than its 866-line private original: it queries
# scan_health_log directly for the review window plus, for new_patterns,
# the immediately preceding window of equal length -- a second date-range
# query, not a persisted trend table, since no new migration is in scope
# for this unit. Multi-night TREND arrays (private's _build_trend, walking
# N nights of summary.json) have no host equivalent yet and are dropped
# with disclosure (see the PR body's Modularity note) rather than invented
# against a table this unit does not create.
#
# PORT-SEAM: private's _source_names(config) matched log lines against a
# hardcoded config-driven allowlist of scanner/source names to attribute a
# line's origin via substring search over free text. There is nothing to
# substring-match here: every scan_health_log row already carries its own
# `source` field in the structured jsonb payload, queried directly via
# `payload->>'source'`. Carrying an allowlist forward would be exactly the
# hardcoded-list anti-pattern global rule #9 forbids, for a problem
# structured data already solves.
#
# The #2014/#2020 REPORT-QUALIFICATION PATTERN this port keeps: a report
# whose evidence is incomplete says so, instead of reading as a clean
# night. Private's rotation-aware log read produced a "% of window
# observed" coverage fraction and a "partial log read" qualifier; there is
# no file rotation to read here, so the analogous host-side incompleteness
# signal is whatever the caller's window_coverage says
# (jobcannon.host.nightly.morning_driver) -- zero sampler ticks or a
# coverage gap means this budget's counts are *also* incomplete, since it
# too is windowed by wall-clock time, not by "rows actually captured".
# ``observed`` mirrors private's same-named #1572 field: True whenever this
# call ran (a scan_health_log query never "fails to observe" the way a
# missing summary.json file did), so it is always True on the happy path
# here -- kept as a field, not dropped, so callers do not need a
# private-vs-host branch to read it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# The two scan_health_log payload["level"] values this digest counts.
# Rows with no "level" key at all (routine, non-adverse events -- e.g. a
# successful scan) are excluded by construction, not by a hardcoded source
# list: any caller that wants to show up here just sets level.
_LEVELS = ("WARNING", "ERROR")


def _raw(conn: Any):
    return conn.raw if hasattr(conn, "raw") else conn


def _level_counts(conn: Any, window_start: datetime, window_end: datetime) -> dict[str, int]:
    rows = (
        _raw(conn)
        .execute(
            """
            SELECT payload->>'level' AS level, count(*) AS n
            FROM scan_health_log
            WHERE recorded_at >= %s AND recorded_at < %s
              AND payload->>'level' = ANY(%s)
            GROUP BY payload->>'level'
            """,
            (window_start, window_end, list(_LEVELS)),
        )
        .fetchall()
    )
    return {r["level"]: r["n"] for r in rows}


def _per_source_counts(conn: Any, window_start: datetime, window_end: datetime) -> dict[str, int]:
    rows = (
        _raw(conn)
        .execute(
            """
            SELECT coalesce(payload->>'source', 'unknown') AS source, count(*) AS n
            FROM scan_health_log
            WHERE recorded_at >= %s AND recorded_at < %s
              AND payload->>'level' = ANY(%s)
            GROUP BY coalesce(payload->>'source', 'unknown')
            ORDER BY n DESC
            """,
            (window_start, window_end, list(_LEVELS)),
        )
        .fetchall()
    )
    return {r["source"]: r["n"] for r in rows}


def _signature_patterns(conn: Any, window_start: datetime, window_end: datetime) -> dict[str, int]:
    """Counts keyed "source:kind" -- the structured analog of private's
    log-line-text signature-pattern grouping (config-driven {pattern,
    severity} regexes matched over free text). ``kind`` is whatever string
    the recording call site passed (e.g. "nightly_checkpoint_fail",
    "nightly_audit_batch_failed") -- the same data-driven contract
    signatures.py used, just keyed on a structured field instead of a regex.
    """
    rows = (
        _raw(conn)
        .execute(
            """
            SELECT coalesce(payload->>'source', 'unknown') AS source,
                   coalesce(payload->>'kind', payload->>'level', 'unknown') AS kind,
                   count(*) AS n
            FROM scan_health_log
            WHERE recorded_at >= %s AND recorded_at < %s
              AND payload->>'level' = ANY(%s)
            GROUP BY coalesce(payload->>'source', 'unknown'),
                     coalesce(payload->>'kind', payload->>'level', 'unknown')
            ORDER BY n DESC
            """,
            (window_start, window_end, list(_LEVELS)),
        )
        .fetchall()
    )
    return {f"{r['source']}:{r['kind']}": r["n"] for r in rows}


def build_nightly_error_budget(
    conn: Any,
    monitor_cfg: dict,
    *,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> dict:
    """Overnight WARNING/ERROR scan_health_log digest for
    [window_start_utc, window_end_utc).

    Never raises: a query failure degrades to an empty, ``observed=False``
    summary rather than crashing the morning driver -- matching every other
    module in this unit's convention that a DB or provider outage produces
    a typed "we don't know" result, not an exception the caller must catch.
    ``monitor_cfg`` is accepted (unused today) to keep this signature
    consistent with every other stage function in this unit, which all take
    the full monitor_cfg even when they only read one sub-block of it.
    """
    del monitor_cfg
    try:
        level_counts = _level_counts(conn, window_start_utc, window_end_utc)
        per_source = _per_source_counts(conn, window_start_utc, window_end_utc)
        patterns = _signature_patterns(conn, window_start_utc, window_end_utc)

        prior_start = window_start_utc - (window_end_utc - window_start_utc)
        prior_patterns = _signature_patterns(conn, prior_start, window_start_utc)
        new_patterns = sorted(set(patterns) - set(prior_patterns))
        observed = True
    except Exception:
        logger.warning("nightly error_budget query failed", exc_info=True)
        level_counts, per_source, patterns, new_patterns = {}, {}, {}, []
        observed = False

    return {
        "window_start_utc": window_start_utc.isoformat(),
        "window_end_utc": window_end_utc.isoformat(),
        "warning_count": level_counts.get("WARNING", 0),
        "error_count": level_counts.get("ERROR", 0),
        "per_source": per_source,
        "signature_patterns": patterns,
        "new_patterns": new_patterns,
        "observed": observed,
    }


def markdown_section(budget: dict) -> str:
    """Render ``build_nightly_error_budget``'s return dict as a "## Error
    Budget" markdown section, mirroring private's _markdown_report
    qualifying-language convention (state incompleteness instead of reading
    as a clean night) rather than a byte-identical port of private's much
    larger _markdown_report (which also rendered the multi-night trend
    table this port drops -- see the module docstring).
    """
    if not budget.get("observed", False):
        return "\n\n## Error Budget\n\n*Error budget data unavailable this run.*\n"

    lines = [
        "\n\n## Error Budget",
        "",
        f"- window: {budget['window_start_utc']} -> {budget['window_end_utc']}",
        f"- warnings: {budget['warning_count']}, errors: {budget['error_count']}",
    ]
    if budget["per_source"]:
        top = ", ".join(f"{k}: {v}" for k, v in list(budget["per_source"].items())[:10])
        lines.append(f"- per source: {top}")
    else:
        lines.append("- per source: none")
    if budget["new_patterns"]:
        lines.append(f"- new patterns since the prior window: {', '.join(budget['new_patterns'])}")
    lines.append("")
    return "\n".join(lines)
