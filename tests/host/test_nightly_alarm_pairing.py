"""New file, no private equivalent: AST-enforces the nightly unit's
alarm-pairing convention declared in jobcannon/host/nightly/__init__.py's
docstring (issue #398's "deadman/degraded-key + escalation-test pairing"
modularity follow-up).

Convention: every ALARM-GRADE record_scan_health call in
jobcannon/host/nightly/ -- a call passing a literal level="ERROR" or
level="WARNING", which is what lands the row in error_budget.py's
WARNING/ERROR digest -- must pair with a tests/host/test_*.py file that
(a) exercises the record_scan_health seam, (b) names the call's
identifying literal (kind= preferred, else source=), and (c) asserts on
"level" with the same severity string. The reference pairings:
deadman.py's "deadman_report_missing" vs test_nightly_deadman.py, and
sampler.py's FAIL-escalation source="nightly_sampler" vs
test_nightly_sampler.py.

Best-effort STATIC lint over literals, same standard
test_nightly_state_single_writer.py sets: a record_scan_health call whose
level= is a NON-literal expression is flagged rather than silently
classified -- make it literal so the pairing stays checkable. Forensic
rows (no level= kwarg at all -- audit_stage.py's nightly_audit_*
batch-failure records) and heartbeats (level="INFO" -- morning_driver.py's
nightly_morning_report) are not alarm paths and are ignored by
construction. No DB needed: this is a pure source scan.
"""

from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_NIGHTLY_DIR = _REPO_ROOT / "jobcannon" / "host" / "nightly"
_TEST_DIR = _REPO_ROOT / "tests" / "host"
_ALARM_LEVELS = {"ERROR", "WARNING"}
_NON_ALARM_LEVELS = {"DEBUG", "INFO"}


def _literal(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _alarm_call_sites() -> list[dict]:
    """Every record_scan_health(...) call in jobcannon/host/nightly/*.py
    carrying a level= kwarg, classified alarm-grade (literal ERROR/WARNING)
    vs non-alarm (literal other). A non-literal level= is reported as
    "unverifiable" so the scan cannot silently skip a disguised alarm.
    Returns dicts: {module, line, level, kind, source, cls}."""
    sites: list[dict] = []
    for path in sorted(_NIGHTLY_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if called != "record_scan_health":
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
            if "level" not in kwargs:
                continue  # forensic row -- no level, not alarm-grade by definition
            level = _literal(kwargs["level"])
            if level is None:
                cls = "unverifiable"
            elif level in _ALARM_LEVELS:
                cls = "alarm"
            else:
                cls = "non_alarm"
            sites.append(
                {
                    "module": path.name,
                    "line": node.lineno,
                    "level": level,
                    "kind": _literal(kwargs["kind"]) if "kind" in kwargs else None,
                    "source": _literal(kwargs["source"]) if "source" in kwargs else None,
                    "cls": cls,
                }
            )
    return sites


def _pairing_test_sources() -> dict[str, str]:
    """tests/host/test_*.py sources that exercise the record_scan_health
    seam -- the only files in which a paired assertion can live. This
    guard file itself is excluded: it contains every alarm identifier in
    its own scan logic, so counting it would let the convention pair with
    itself."""
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted(_TEST_DIR.glob("test_*.py"))
        if p.name != pathlib.Path(__file__).name
        and "record_scan_health" in p.read_text(encoding="utf-8")
    }


def test_nightly_alarm_paths_have_paired_health_row_assertions():
    sites = _alarm_call_sites()
    assert sites, (
        f"scan found no level-bearing record_scan_health call under {_NIGHTLY_DIR} "
        "-- scan is vacuous, not clean"
    )

    unverifiable = [
        f"{s['module']}:{s['line']} (non-literal level=)"
        for s in sites
        if s["cls"] == "unverifiable"
    ]
    alarm_sites = [s for s in sites if s["cls"] == "alarm"]
    assert alarm_sites, (
        "scan found no alarm-grade record_scan_health call "
        "(level=ERROR/WARNING) under jobcannon/host/nightly/ "
        "-- scan is vacuous, not clean"
    )

    sources = _pairing_test_sources()
    unpaired: list[str] = []
    for site in alarm_sites:
        ident = site["kind"] or site["source"]
        if ident is None:
            unpaired.append(
                f"{site['module']}:{site['line']} level={site['level']!r} call has no "
                "literal kind=/source= to pair on"
            )
            continue
        if not any(
            ident in src and '"level"' in src and f'"{site["level"]}"' in src
            for src in sources.values()
        ):
            unpaired.append(
                f"{site['module']}:{site['line']} {site['level']} row ({ident!r}) has no "
                "tests/host/test_*.py asserting the emitted row's level -- see the "
                "alarm-pairing convention in jobcannon/host/nightly/__init__.py"
            )

    problems = unverifiable + unpaired
    assert not problems, "nightly alarm-pairing violations:\n" + "\n".join(problems)


def test_alarm_call_sites_scan_finds_known_reference_sites():
    """Positive control: the two reference alarm paths the convention cites
    must actually be found by the scan, classified alarm-grade, and carry
    the identifiers the pairing check keys on -- otherwise the guard above
    could pass while scanning nothing real."""
    sites = _alarm_call_sites()
    alarms = {
        (s["module"], s["kind"], s["source"], s["level"]) for s in sites if s["cls"] == "alarm"
    }
    assert ("deadman.py", "deadman_report_missing", "nightly_monitor", "ERROR") in alarms
    assert ("sampler.py", None, "nightly_sampler", "ERROR") in alarms
    # Non-alarm rows the convention names must NOT be classified alarm:
    # audit_stage's forensic rows carry no level; morning_driver's
    # heartbeat is level="INFO".
    assert not any(s["module"] == "audit_stage.py" and s["cls"] == "alarm" for s in sites)
    assert not any(s["module"] == "morning_driver.py" and s["cls"] == "alarm" for s in sites)
