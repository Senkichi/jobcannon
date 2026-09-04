# PORTED from tests/test_nightly_state.py @ a7f0f38a85dfa0af4d305c04da833785f723d649 (private job-cannon). Ledger L-0594.
"""jobcannon.host.nightly.state -- watermark cursors, fire-once notify dedup,
three-way concurrent-write merge.

# PORT-SEAM: (L-0471, the ADAPTED module itself -- see its own module
# docstring for the full rationale) state.json's single-file-blob model
became a single ``nightly_monitor_state`` Postgres row (jsonb ``value``
column), so ``load_state``/``save_state`` now take a ``conn`` as their
first argument. The default-state key set shrank to a strict subset:
``app_log_offset``/``run_events_offset`` (byte-offset cursors) became
``scan_health_watermark_id``/``procrastinate_watermark_id`` (DB-cursor
columns); ``notified``/``already_notified``/``mark_notified``/
``_merge_state`` carry over unchanged (pure dict logic). Night-dir/
report-file fields (``last_report_at``, ``last_report_date``,
``last_morning_status``, ``last_audit_summary``, disagreement-rate
history) belong to the not-yet-ported morning-report/audit-stage units;
``monitor_root``/``night_dir``/``window_dirs``/``report_file_exists``/
``parse_jsonl``/``read_new_bytes`` (file-tail helpers) DIE outright -- no
app.log or night-dir filesystem model exists on this host.

Per-test coverage decision:

KEPT/ADAPTED:
- ``TestState`` -> defaults + round-trip, against the real
  ``nightly_monitor_state`` table via ``db_conn``.
- ``TestFireOnce`` -> unchanged 1:1 (``mark_notified``/``already_notified``
  are pure dict functions, no DB involved at all).
- ``TestConcurrentMerge`` -> the #1311 regression (a stale read-modify-write
  must not clobber a concurrent writer's unrelated field) is re-targeted at
  the two watermark-id fields + ``notified`` (the only fields the current
  default-state schema exposes), preserving the exact race shape (stale
  base, two independent writers) the private tests guarded.

DROPPED (private-only surface, listed in the PR body):
- ``TestReadNewBytes`` (``read_new_bytes`` -- file-tail byte-offset reader,
  DIES with the file-tail model; no app.log on this host).
- ``TestState.test_corrupt_file_yields_defaults`` (state.json parse-error
  fallback -- no filesystem-corruption analogue for a jsonb column, which
  Postgres itself guarantees is valid JSON; ``_load_state_unsafe``'s
  defensive ``if not isinstance(raw, dict)`` branch guards a shape that
  cannot arise through ``save_state``'s own write path).
- ``TestPaths`` (``night_dir``/``monitor_root``/``window_dirs`` -- DIE,
  no per-night filesystem directory model on this host).
- ``TestReportFileExists`` (``report_file_exists`` -- DIES with the
  report.md file-check fallback; belongs to the not-yet-ported morning
  report unit).
- ``TestParseJsonl`` (``parse_jsonl`` -- DIES with the file-tail model).
- ``TestConcurrentMerge.test_intentional_none_scalar_is_written`` -- the
  private regression exercised ``last_disagreement_rate`` (a nullable
  float, DIES field). The current default-state schema's only scalar
  fields are non-nullable watermark ids with no legitimate None value to
  round-trip, so there is no field left to exercise this specific
  regression against; ``_merge_state``'s comparison (``new_val !=
  base.get(key)``) is untyped and value-agnostic, so the underlying merge
  logic is still exercised by every other ``TestConcurrentMerge`` test.
"""

from __future__ import annotations

import threading

from jobcannon.host.nightly import (
    state,
)  # PORT-SEAM: TestReadNewBytes (read_new_bytes -- file-tail model, DIES) dropped here, see module docstring


class TestState:
    def test_defaults_when_missing(
        self, db_conn
    ):  # PORT-SEAM: watermark-id fields replace app_log_offset/run_events_offset (L-0471)
        loaded = state.load_state(db_conn)
        assert loaded["scan_health_watermark_id"] == 0
        assert loaded["procrastinate_watermark_id"] == 0
        assert loaded["notified"] == []

    def test_round_trip(
        self, db_conn
    ):  # PORT-SEAM: db_conn replaces state_path()/tmp-file atomic-write check
        base = state.load_state(db_conn)
        new = {**base, "scan_health_watermark_id": 123}
        state.save_state(db_conn, new)

        loaded = state.load_state(db_conn)
        assert (
            loaded["scan_health_watermark_id"] == 123
        )  # PORT-SEAM: test_corrupt_file_yields_defaults dropped here (jsonb column, no filesystem-corruption analogue; see module docstring)


class TestFireOnce:
    def test_mark_notified_returns_new_dict(self):
        base = dict(
            state._DEFAULT_STATE
        )  # PORT-SEAM: _DEFAULT_STATE replaces load_state() (pure dict logic, no DB round-trip needed)
        marked = state.mark_notified(base, "2026-07-15:ATS scan")
        assert marked is not base
        assert not state.already_notified(base, "2026-07-15:ATS scan")
        assert state.already_notified(marked, "2026-07-15:ATS scan")

    def test_idempotent(self):
        base = dict(state._DEFAULT_STATE)  # PORT-SEAM: same _DEFAULT_STATE substitution as above
        marked = state.mark_notified(base, "k")
        again = state.mark_notified(marked, "k")
        assert again["notified"].count("k") == 1


# PORT-SEAM: TestPaths, TestReportFileExists, TestParseJsonl dropped here -- see module docstring DROPPED note (file-tail/night-dir filesystem model DIES)
class TestConcurrentMerge:
    """Regression for issue #1311: a stale read-modify-write must not clobber
    fields set by another writer.

    # PORT-SEAM: (L-0471) re-targeted at the fields the current default-state
    # schema actually exposes (watermark-id fields) -- see the module
    # docstring for the full rationale.
    """

    def test_stale_writer_preserves_concurrent_writers_field(
        self, db_conn
    ):  # PORT-SEAM: renamed <- test_stale_sampler_write_preserves_morning_report_fields
        """The exact 2026-07-20-shaped race: one writer saves a change to
        scan_health_watermark_id from a stale base; a second, still-stale
        writer then saves a change to procrastinate_watermark_id from the
        SAME stale base. Neither write may clobber the other's field."""
        base = state.load_state(db_conn)

        writer_a = {
            **base,
            "scan_health_watermark_id": 50,
        }  # PORT-SEAM: watermark-id field replaces last_report_at/last_disagreement_rate/last_morning_status/last_audit_summary dict (DIES fields)
        state.save_state(db_conn, writer_a, base)

        # PORT-SEAM: Writer B still holds the *stale* base and only wants to
        # advance its own watermark (replaces the private "Sampler" writer).
        writer_b = {**base, "procrastinate_watermark_id": 456}
        state.save_state(db_conn, writer_b, base)

        loaded = state.load_state(db_conn)
        # PORT-SEAM: replaces the private last_report_date/last_report_at/
        # last_morning_status/last_disagreement_rate/last_audit_summary/
        # app_log_offset/run_events_offset assertion block (DIES fields).
        assert loaded["scan_health_watermark_id"] == 50
        assert loaded["procrastinate_watermark_id"] == 456

    def test_notified_merges_additively(
        self, db_conn
    ):  # PORT-SEAM: db_conn param added throughout TestConcurrentMerge (L-0471)
        """Two writers each append a fire-once key from a stale base; both stay."""
        base = state.load_state(db_conn)  # PORT-SEAM: db_conn param (L-0471)
        writer_a = state.mark_notified(base, "2026-07-20:deadman")
        state.save_state(db_conn, writer_a, base)

        # Writer B starts from the same stale base and appends a different key.
        writer_b = state.mark_notified(base, "2026-07-20:ATS scan")
        state.save_state(db_conn, writer_b, base)  # PORT-SEAM: db_conn param (L-0471)

        loaded = state.load_state(db_conn)  # PORT-SEAM: db_conn param (L-0471)
        assert "2026-07-20:deadman" in loaded["notified"]
        assert "2026-07-20:ATS scan" in loaded["notified"]

    def test_threaded_save_state_serializes(
        self, db_conn
    ):  # PORT-SEAM: test_intentional_none_scalar_is_written dropped here (see module docstring DROPPED note); db_conn param (L-0471)
        """Concurrent save_state calls from different threads (the SAME
        pooled connection, mirroring real single-worker-process usage --
        see state.py's ``_thread_lock`` docstring) must not lose each
        other's distinct field updates."""
        errors = []

        def writer(key, value):
            try:
                base = state.load_state(db_conn)  # PORT-SEAM: db_conn param (L-0471)
                new = {**base, key: value}
                state.save_state(db_conn, new, base)  # PORT-SEAM: db_conn param (L-0471)
            except Exception as exc:
                errors.append(exc)

        def notify_writer(
            key,
        ):  # PORT-SEAM: new nested writer (not in private) -- exercises mark_notified under the SAME threaded race
            try:
                base = state.load_state(db_conn)
                new = state.mark_notified(base, key)
                state.save_state(db_conn, new, base)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=("scan_health_watermark_id", 111)),
            threading.Thread(target=writer, args=("procrastinate_watermark_id", 222)),
            threading.Thread(
                target=notify_writer, args=("2026-07-20:threaded",)
            ),  # PORT-SEAM: watermark-id fields + notify_writer replace app_log_offset/run_events_offset/last_report_date
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        loaded = state.load_state(db_conn)  # PORT-SEAM: db_conn param (L-0471); watermark-id fields
        assert loaded["scan_health_watermark_id"] == 111
        assert loaded["procrastinate_watermark_id"] == 222
        assert "2026-07-20:threaded" in loaded["notified"]
