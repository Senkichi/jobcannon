"""Read-only re-measurement of the ATS rectification metrics M1..M10.

Source: docs/plans/2026-08-22-ats-pipeline-holistic-review-PLAN.md section 0
and the REPORT section 7.1 queries. Opens the live DB with ``mode=ro`` and the
run-events journal read-only; never writes. Run before/after each wave and
paste the output into the wave's PR body.

Usage:
    python scripts/ats_rectification_metrics.py [--db PATH] [--run-events PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobcannon.engine.ats_registry import POSTING_ID_PLATFORMS

# M1's ``ats_direct`` cohort = platforms with a direct posting-id URL shape
# (``spec.posting_id_pattern is not None``). Derived from the registry so a
# future platform addition/removal flows through without a hand-copy desync
# (#1871). This is a strict subset of RECONCILABLE_PLATFORMS — successfactors
# and adp are reconcilable via batch set-diff but expose no single-posting URL
# shape, so they are correctly excluded from the latency cohort.
ATS_DIRECT = POSTING_ID_PLATFORMS

# Q-G1 fragments (REPORT 7.1); live thresholds 10 / 3 per the REPORT.
_BASE = (
    "((ats_probe_status='hit' AND scan_enabled=1) OR (ats_probe_status='error' "
    "AND scan_enabled=1 AND (retry_after IS NULL OR retry_after<datetime('now'))))"
)
_IDENT = "(ats_platform IS NOT NULL AND ats_slug IS NOT NULL)"
_DORM = (
    "(consecutive_empty_scans <= {empty} OR last_scanned_at IS NULL "
    "OR last_scanned_at < datetime('now','-{days} days'))"
)


def _latency(conn: sqlite3.Connection, gated: bool) -> dict:
    gate = "AND j.posted_date != j.first_seen" if gated else ""
    rows = conn.execute(
        f"""
        SELECT julianday(j.first_seen)-julianday(j.posted_date), j.sources
        FROM jobs j
        WHERE j.posted_date_precision='exact' AND j.posted_date IS NOT NULL
          AND j.first_seen IS NOT NULL {gate}
          AND julianday(j.first_seen)-julianday(j.posted_date) >= 0
          AND julianday('now')-julianday(j.posted_date) <= 60
        """
    ).fetchall()
    out: dict[str, dict] = {}
    for label, pred in (
        ("ats_direct", lambda s: any(p in s for p in ATS_DIRECT)),
        ("workday", lambda s: "workday" in s),
    ):
        lags = [lag for lag, src in rows if src and pred(src.lower())]
        n = len(lags)
        out[label] = {
            "n": n,
            "p50_days": round(statistics.median(lags), 2) if lags else None,
            "within_24h_pct": (round(100 * sum(1 for x in lags if x <= 1) / n, 1) if n else None),
        }
    return out


def _phase_a(conn: sqlite3.Connection, empty: int = 10, days: int = 3) -> dict:
    dorm = _DORM.format(empty=empty, days=days)

    def q(where: str) -> int:
        return conn.execute(f"select count(*) from companies where {where}").fetchone()[0]

    return {
        "base": q(_BASE),
        "base_ident": q(f"{_BASE} and {_IDENT}"),
        "base_ident_dorm": q(f"{_BASE} and {_IDENT} and {dorm}"),
    }


def _m3(conn: sqlite3.Connection) -> float | None:
    r = conn.execute(
        "select avg(last_scanned_at is null or last_scanned_at < datetime('now','-7 days'))"
        " from companies where scan_enabled=1 and ats_probe_status='hit'"
    ).fetchone()[0]
    return round(100 * r, 1) if r is not None else None


def _run_events(path: Path, since: datetime) -> dict:
    starts: dict[str, int] = defaultdict(int)
    ends: dict[str, int] = defaultdict(int)
    ok_ends: dict[str, int] = defaultdict(int)
    promote_nights: dict[str, int] = {}
    if not path.exists():
        return {"missing": str(path)}
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            ts = e.get("ts", "")
            try:
                when = datetime.fromisoformat(ts)
            except Exception:
                continue
            if when < since:
                continue
            job, ev = e.get("job"), e.get("event")
            if ev == "run_start":
                starts[job] += 1
            elif ev == "run_end":
                ends[job] += 1
                disp = (e.get("disposition") or "").lower()
                if disp in ("completed", "success", "degraded"):
                    ok_ends[job] += 1
                if job == "ATS source-URL promotion":
                    promoted = 0
                    res = e.get("result")
                    if isinstance(res, dict):
                        promoted = int(res.get("promoted", 0) or 0)
                    elif isinstance(res, str):
                        m = re.search(r"'promoted':\s*(\d+)", res)
                        promoted = int(m.group(1)) if m else 0
                    day = ts[:10]
                    promote_nights[day] = max(promote_nights.get(day, 0), promoted)
    return {
        "ats_scan_started": starts.get("ATS scan", 0),
        "ats_scan_completed_ok": ok_ends.get("ATS scan", 0),
        "ats_scan_run_end_total": ends.get("ATS scan", 0),
        "promote_nights_total": len(promote_nights),
        "promote_nights_with_promoted_gt0": sum(1 for v in promote_nights.values() if v > 0),
    }


def _m7(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "select count(*) from companies where ats_platform is null and ats_probe_status='miss'"
        " and scan_enabled=0 and careers_url is null"
    ).fetchone()[0]


def _m8(conn: sqlite3.Connection) -> dict:
    nonempty = "metadata like '%\"errors\": [%' and metadata not like '%\"errors\": []%'"
    total_err = conn.execute(
        f"select count(*) from user_activity where action='scheduled_ats_scan' and {nonempty}"
    ).fetchone()[0]
    err_success = conn.execute(
        "select count(*) from user_activity where action='scheduled_ats_scan'"
        f' and metadata like \'%"status": "success"%\' and {nonempty}'
    ).fetchone()[0]
    err_key_present = conn.execute(
        "select count(*) from user_activity where action='scheduled_ats_scan'"
        " and metadata like '%\"errors\"%'"
    ).fetchone()[0]
    return {
        "runs_with_errors_key": err_key_present,
        "runs_with_nonempty_errors": total_err,
        "of_which_status_success": err_success,
    }


def _m9(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "select count(*) from company_scan_log where source='ats_scanner' and error is not null"
        " and scanned_at>datetime('now','-14 days')"
    ).fetchone()[0]


def _m6(conn: sqlite3.Connection) -> int | str:
    try:
        from scripts.company_dedup_report import count_clusters  # type: ignore
    except Exception:
        return "n/a (scripts/company_dedup_report.py not yet shipped - WI-15)"
    return count_clusters(conn)


def _m10(conn: sqlite3.Connection) -> dict:
    tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    return {
        "scan_selection_log": "scan_selection_log" in tables,
        "scan_title_outcomes": "scan_title_outcomes" in tables,
    }


def measure(db: Path, run_events: Path) -> dict:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.execute("pragma temp_store=memory")
    since = datetime.now() - timedelta(days=14)
    return {
        "measured_at_local": datetime.now().isoformat(timespec="seconds"),
        "measured_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "db": str(db),
        "M1_gated": _latency(conn, gated=True),
        "M1_ungated_control": _latency(conn, gated=False),
        "M2_phase_a": _phase_a(conn),
        "M3_hit_enabled_stale_7d_pct": _m3(conn),
        "M4_M5_run_events_trailing_14d": _run_events(run_events, since),
        "M6_near_dup_clusters": _m6(conn),
        "M7_absorbing_state": _m7(conn),
        "M8_errors_vs_status": _m8(conn),
        "M9_scan_log_error_rows_14d": _m9(conn),
        "M10_answerability_tables": _m10(conn),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--run-events", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    db = Path(args.db) if args.db else root / "jobs.db"
    events = Path(args.run_events) if args.run_events else root / "logs" / "run_events.jsonl"
    result = measure(db, events)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    for k, v in result.items():
        print(f"{k}: {json.dumps(v) if not isinstance(v, str) else v}")


if __name__ == "__main__":
    main()
