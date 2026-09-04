"""PORTED from job_finder/web/nightly_monitor/_deadman.py
@ b34eb0b00a13c0bf0c9c95020ceddfeca71022b8 (private job-cannon). Ledger L-0387.

Out-of-process morning-report deadman (issue #1267): fires once per UTC
calendar day when the morning report is missing past its configured slot
plus grace window.

# PORT-SEAM: private ran under an OS-native scheduled task
# (``JobCannon-Deadman``), independent of the ``serve`` process it
# watchdogs, because the sampler's same-day deadman lives IN that process
# and cannot fire if it dies. Host has no OS-scheduler concept outside
# procrastinate -- the equivalent independence comes from registering this
# as its own procrastinate periodic (jobcannon.host.tasks, wired by the
# other half of this port unit) rather than folding the check into the
# review/audit periodic: a stuck or crashed review periodic must not also
# silence the alarm that is supposed to report it missing.
#
# Wall clock: private used local time (``datetime.now()``, morning_hour/
# morning_minute interpreted locally). Render runs UTC and there is no
# "local" timezone for a hosted worker, so this and
# jobcannon.host.nightly.state.morning_deadline are all-UTC (state.py's Q2
# note) -- unlike private, there is no local-timezone conversion at all.
#
# D12 belt-and-suspenders (private's ``report_file_exists``, a local
# report.md existence check backstopping a state.json write that could lag
# or fail independently of the report itself) has no host equivalent and is
# dropped: state.save_state is a single atomic Postgres UPSERT, so there is
# no second artifact for it to fall out of sync with -- the state row IS
# the fact of whether a report happened. The four
# TestReportFileBeltAndSuspenders cases in private's
# tests/test_nightly_monitor_deadman.py are dropped for the same reason
# (see PR body).
#
# Alerting: private's ``notify(...)`` (local OS notification) becomes
# ``record_scan_health(source="nightly_monitor", kind="deadman_report_missing",
# ...)``, mirroring the pattern jobcannon.host.nightly.sampler already uses
# for its own fire-once alerts on a FAIL verdict.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from jobcannon.host.health_recorder import record_scan_health
from jobcannon.host.nightly import state as _state
from jobcannon.host.nightly.config import nightly_monitor_config, nightly_monitor_enabled

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Seam for tests; UTC wall clock (deadline math is UTC, like the cron slot)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _utc_date_str(now: datetime) -> str:
    """UTC calendar date string matching last_report_date's own format."""
    return now.strftime("%Y-%m-%d")


def run_deadman_check(*, _now: datetime | None = None) -> dict:
    """One out-of-process deadman check; never raises into the host periodic worker.

    Checks the kill switch and opens its own pooled connection -- callers
    should not pass one in, matching
    jobcannon.host.nightly.sampler.run_sampler_tick's convention
    (jobcannon.host.tasks owns the periodic wrapper).
    """
    if not nightly_monitor_enabled():
        return {
            "enabled": False,
            "alerted": False,
            "notified": False,
            "reason": "nightly_monitor disabled",
        }
    try:
        from jobcannon.db import connection_factory

        with connection_factory() as conn:
            return _check(conn, _now=_now)
    except Exception:
        logger.warning("nightly deadman check failed", exc_info=True)
        return {
            "enabled": True,
            "alerted": False,
            "notified": False,
            "reason": "deadman check failed",
        }


def _check(conn: Any, *, _now: datetime | None = None) -> dict:
    monitor_cfg = nightly_monitor_config()
    now = _now or _utcnow()
    deadline = _state.morning_deadline(monitor_cfg, now)
    if now < deadline:
        return {
            "enabled": True,
            "alerted": False,
            "notified": False,
            "reason": f"deadline not reached ({deadline.strftime('%H:%M')} UTC)",
        }

    date_str = _utc_date_str(now)
    state = _state.load_state(conn)
    base_state = state
    if state.get("last_report_date") == date_str:
        return {
            "enabled": True,
            "alerted": False,
            "notified": False,
            "reason": f"report already present for {date_str}",
        }

    key = f"{date_str}:deadman"
    if _state.already_notified(state, key):
        return {
            "enabled": True,
            "alerted": False,
            "notified": False,
            "reason": f"already notified for {key}",
        }

    reason = (
        f"No report for {date_str} by {deadline.strftime('%H:%M')} UTC "
        f"(last_report_date={state.get('last_report_date')})."
    )
    record_scan_health(
        source="nightly_monitor",
        kind="deadman_report_missing",
        level="ERROR",
        date=date_str,
        reason=reason,
    )
    _state.save_state(conn, _state.mark_notified(state, key), base_state)

    return {
        "enabled": True,
        "alerted": True,
        "notified": True,
        "reason": reason,
    }
