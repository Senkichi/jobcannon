"""ADAPTED from job_finder/web/nightly_monitor/_state.py
@ e1f47695b07f928e6c91cc64767c97a99645d68f (private job-cannon).
Ledger L-0471.

nightly_monitor_state -- sole writer for the table (single-writer discipline
mirrors jobcannon/db/_score_audits.py / _companies.py / _jobs.py;
tests/host/test_nightly_state_single_writer.py AST-scans the repo for INSERT/
UPDATE literals against nightly_monitor_state and fails the build if any turn
up outside this file).

# PORT-SEAM: state.json becomes a single Postgres row (key='nightly_monitor',
# a jsonb ``value`` column holding the whole state blob, matching
# state.json's single-blob shape 1:1) in a new nightly_monitor_state table,
# so _merge_state's three-way-merge logic below carries over unchanged. The
# atomic os.replace write becomes a single INSERT ... ON CONFLICT DO UPDATE
# under a pooled connection. Win32 LOCK_EX (private's _state_lock) is
# replaced by a Postgres row-level SELECT ... FOR UPDATE inside save_state.
#
# _DEFAULT_STATE is a strict SUBSET of private's: night-dir/report-file
# fields (monitor_root/night_dir/report_file_exists/window_dirs/
# local_date_str/morning_deadline, and the state keys they read --
# last_report_at, last_report_date, last_morning_status,
# last_missed_report_dates) belong to the morning report writer, a later
# ledger unit -- there is no report.py on this branch to consume them yet.
# disagreement-rate history / pending_retry_after_reset belong to the audit
# stage, also a later unit. unmapped_streaks/monitored_ledger_names DIE
# outright: on this host the monitored set is the distinct set of
# procrastinate task names, which are already canonical, so there is no id
# ambiguity left to track streaks against. parse_jsonl/read_new_bytes
# (file-tail byte-offset helpers) DIE with the file-tail read model itself
# -- there is no app.log on this host to tail; scan_health_log/
# procrastinate_jobs rows are queried by a DB watermark cursor instead, so
# log-rotation reset logic has nothing left to reset.
#
# What DOES carry over for the sampler + checkpoint work landing dark in
# this unit: app_log_offset/run_events_offset become
# scan_health_watermark_id / procrastinate_watermark_id (DB cursor columns,
# not byte offsets); notified/already_notified/mark_notified (fire-once
# dedup, here guarding repeat scan_health_log ERROR rows the sampler writes
# on a FAIL verdict or fail-severity signature); _merge_state (pure dict
# logic, unchanged).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from jobcannon.db.pool import commit_unless_nested

logger = logging.getLogger(__name__)

# In-process lock: multiple threads in the SAME worker process (e.g. a
# concurrent web request reading state while a periodic tick writes it)
# still need to serialize, same rationale as private's _thread_lock --
# portalocker's Windows backend didn't block same-process threads there;
# here the analogous gap is two threads racing the same DB round trip.
# Cross-process serialization is the Postgres row lock in save_state below.
_thread_lock = threading.Lock()

_STATE_KEY = "nightly_monitor"
_NOTIFIED_CAP = 200

_DEFAULT_STATE: dict = {
    "scan_health_watermark_id": 0,
    "procrastinate_watermark_id": 0,
    "notified": [],
}


def _raw(conn: Any):
    return conn.raw if hasattr(conn, "raw") else conn


def _load_state_unsafe(conn: Any) -> dict:
    row = _raw(conn).execute(
        "SELECT value FROM nightly_monitor_state WHERE key = %s", (_STATE_KEY,)
    ).fetchone()
    raw = row["value"] if row else {}
    if not isinstance(raw, dict):
        raw = {}
    state = dict(_DEFAULT_STATE)
    state.update({k: raw[k] for k in _DEFAULT_STATE if k in raw})
    state["notified"] = list(state.get("notified") or [])
    return state


def _write_state_unsafe(conn: Any, state: dict) -> None:
    # PORT-SEAM: temp-file + os.replace (private) -> INSERT ... ON CONFLICT
    # DO UPDATE (single-row upsert; Postgres commits atomically, no partial-
    # write window to guard against the way a crashed os.replace could
    # leave a dangling .tmp file).
    from psycopg.types.json import Jsonb

    _raw(conn).execute(
        "INSERT INTO nightly_monitor_state (key, value, updated_at) "
        "VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (_STATE_KEY, Jsonb(state)),
    )


def _merge_state(base: dict, new: dict, fresh: dict) -> dict:
    """Three-way merge for concurrent state writers.

    For each top-level key:
      - If new[key] == base[key], this writer did not change it; keep fresh[key].
      - Otherwise, this writer changed it; use new[key].

    The ``notified`` list is merged additively because multiple writers each
    append fire-once keys (issue #1311).
    """
    merged = dict(fresh)
    base_notified = set(base.get("notified") or [])
    new_notified = list(new.get("notified") or [])
    fresh_notified = list(fresh.get("notified") or [])
    added = [k for k in new_notified if k not in base_notified]
    merged["notified"] = (fresh_notified + added)[-_NOTIFIED_CAP:]
    for key, new_val in new.items():
        if key == "notified":
            continue
        if new_val != base.get(key):
            merged[key] = new_val
    return merged


def load_state(conn: Any) -> dict:
    try:
        with _thread_lock:
            return _load_state_unsafe(conn)
    except Exception:
        logger.warning("nightly_monitor state load failed", exc_info=True)
        return dict(_DEFAULT_STATE)


def save_state(conn: Any, state: dict, base: dict | None = None) -> None:
    """Upsert; swallows errors (instrumentation must not break the host tick).

    If ``base`` is provided, the write is a three-way merge against the
    current row (locked ``FOR UPDATE`` for the duration of this call): only
    fields that changed from ``base`` to ``state`` overwrite the row,
    preserving a concurrent writer's update to an unrelated field (issue
    #1311).
    """
    try:
        with _thread_lock:
            raw = _raw(conn)
            if base is not None:
                with raw.transaction():
                    row = raw.execute(
                        "SELECT value FROM nightly_monitor_state WHERE key = %s FOR UPDATE",
                        (_STATE_KEY,),
                    ).fetchone()
                    current = row["value"] if row else {}
                    if not isinstance(current, dict):
                        current = {}
                    fresh = dict(_DEFAULT_STATE)
                    fresh.update({k: current[k] for k in _DEFAULT_STATE if k in current})
                    fresh["notified"] = list(fresh.get("notified") or [])
                    to_write = _merge_state(base, state, fresh)
                    _write_state_unsafe(conn, to_write)
            else:
                to_write = state
                _write_state_unsafe(conn, to_write)
            commit_unless_nested(raw)
    except Exception:
        logger.warning("nightly_monitor state write failed", exc_info=True)


def already_notified(state: dict, key: str) -> bool:
    return key in state.get("notified", [])


def mark_notified(state: dict, key: str) -> dict:
    """Fire-once dedup; returns a NEW state dict (immutability convention)."""
    if already_notified(state, key):
        return state
    notified = [*state.get("notified", []), key][-_NOTIFIED_CAP:]
    return {**state, "notified": notified}
