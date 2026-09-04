"""ADAPTED from job_finder/web/nightly_monitor/_morning.py (the report-
assembly tail of run_nightly_morning_review: verdict-line construction,
the JD-quality-flags / JD-content-verdicts / missed-report-dates /
abort-reason / observer-offline postscript sections, and
_write_report/format_duration) @ b34eb0b00a13c0bf0c9c95020ceddfeca71022b8
(private job-cannon). Ledger L-0387.

Pure markdown/verdict-line formatters -- no I/O, no DB, no call_model. The
caller (morning_driver.py) does every read and write; this module only
turns already-assembled dicts into the final report_md string.

# PORT-SEAM: private's report was a local report.md file the morning
# session Read as part of its evidence and the OS could tail. There is no
# local filesystem here, so "the report" is just the string this module
# builds; the caller persists it via jobcannon.host.nightly.state
# (last_report_at/last_report_date) and surfaces it through
# record_scan_health (Q3: no email/toast infra), not a second artifact.
#
# The #1732/#1742 postscript-section convention this port keeps: JD-
# quality-flags and JD-content-verdicts sections are ALWAYS present in the
# rendered report, degrading to an explicit zero/none line rather than
# being omitted on a clean night -- an absent section reads as "nobody
# checked", not "nothing to report", and the two must stay visually
# distinguishable in the rendered output.
"""

from __future__ import annotations


def format_duration(total_s: float) -> str:
    """Render a non-negative second count as "Xh Ym" / "Ym Zs" / "Zs".

    Byte-for-byte behavior match with private's format_duration: hours are
    shown only when non-zero, minutes are shown whenever hours are shown or
    minutes are non-zero, and a value under 60s renders as seconds alone.
    """
    total_s = max(0, int(total_s))
    hours, rem = divmod(total_s, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def verdict_line(
    *,
    observer_offline: bool,
    audit_summary: dict,
    review_incomplete: bool,
) -> str:
    """The single leading verdict line, precedence matching private's own
    (offline observer beats an incomplete review, which beats an audit
    unavailability, which beats a coverage-failure alarm, which beats a
    plain "reviewed" line)."""
    if observer_offline:
        return "**VERDICT: NO DATA** -- the observer window was blind (no sampler/checkpoint coverage this run)."
    if review_incomplete:
        return "**VERDICT: INCOMPLETE** -- the review session did not produce a usable result."
    if audit_summary.get("unavailable"):
        return f"**VERDICT: AUDIT UNAVAILABLE** -- {audit_summary.get('unavailable_reason') or 'no reason recorded'}."
    if audit_summary.get("coverage_failure"):
        return "**VERDICT: COVERAGE ALARM** -- audit coverage or batch failure rate crossed its configured threshold."
    if audit_summary.get("disputes"):
        return f"**VERDICT: REVIEWED** -- {audit_summary['disputes']} dispute(s) out of {audit_summary.get('audited', 0)} audited."
    return "**VERDICT: PASS** -- no disputes this run."


def _abort_reason_section(audit_summary: dict) -> str:
    if not audit_summary.get("unavailable"):
        return ""
    reason = audit_summary.get("unavailable_reason") or "no reason recorded"
    return f"\n\n## Audit Aborted\n\n- reason: {reason}\n"


def _observer_offline_section(observer_offline: bool, window_coverage: dict) -> str:
    if not observer_offline:
        return ""
    ratio = window_coverage.get("coverage_ratio")
    ratio_str = f"{ratio:.0%}" if isinstance(ratio, (int, float)) else "unknown"
    return (
        "\n\n## Observer Offline\n\n"
        f"- coverage ratio: {ratio_str}\n"
        f"- observed ticks: {window_coverage.get('observed_ticks', 0)}"
        f" / expected: {window_coverage.get('expected_ticks', 0)}\n"
    )


def _missed_report_dates_section(missed_dates: list[str]) -> str:
    """Always present -- degrades to an explicit "none" line rather than
    being omitted, matching the #1732/#1742 convention (module docstring)."""
    if not missed_dates:
        return "\n\n## Missed Report Dates\n\n*none.*\n"
    return "\n\n## Missed Report Dates\n\n" + "\n".join(f"- {d}" for d in missed_dates) + "\n"


def _jd_quality_flags_section(audit_summary: dict) -> str:
    flags = audit_summary.get("jd_quality_flags") or {}
    flagged = audit_summary.get("jd_quality_flagged", 0)
    lines = ["\n\n## JD Quality Flags", ""]
    if not flagged:
        lines.append("*none this run.*")
    else:
        lines.append(f"- total flagged: {flagged}")
        for flag, count in sorted(flags.items(), key=lambda kv: -kv[1]):
            lines.append(f"  - {flag}: {count}")
    lines.append("")
    return "\n".join(lines)


def _jd_content_verdicts_section(audit_summary: dict) -> str:
    verdicts = audit_summary.get("jd_content_verdicts") or {}
    lines = ["\n\n## JD Content Verdicts", ""]
    if not verdicts:
        lines.append("*none this run.*")
    else:
        for verdict, count in sorted(verdicts.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {verdict}: {count}")
    lines.append("")
    return "\n".join(lines)


def build_report_md(
    *,
    date_str: str,
    observer_offline: bool,
    window_coverage: dict,
    audit_summary: dict,
    review_result: dict,
    error_budget_md: str,
    missed_report_dates: list[str],
) -> str:
    """Assemble the full report body: verdict line, the review session's
    own narrative (report_stage.run_review_stage's report_md), then the
    always-present postscript sections in a fixed order, then the error
    budget digest.

    Pure string assembly -- morning_driver.py is the only caller and the
    only place that reads/writes state or the DB.
    """
    review_incomplete = bool(review_result.get("incomplete"))
    parts = [
        f"# Nightly Monitor Report -- {date_str}\n\n",
        verdict_line(
            observer_offline=observer_offline,
            audit_summary=audit_summary,
            review_incomplete=review_incomplete,
        ),
        "\n\n",
        str(review_result.get("report_md") or ""),
        _abort_reason_section(audit_summary),
        _observer_offline_section(observer_offline, window_coverage),
        _jd_quality_flags_section(audit_summary),
        _jd_content_verdicts_section(audit_summary),
        _missed_report_dates_section(missed_report_dates),
        error_budget_md,
    ]
    return "".join(parts)
