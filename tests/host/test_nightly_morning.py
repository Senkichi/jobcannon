"""Tests for the nightly morning-review unit (ledger L-0387): report.py's
pure formatters, review_stage.run_review_stage, issue_filer's #1506
cross-check, morning_driver's window-coverage/checkpoint re-derivation, and
the D12 state-before-issue-filing-tail ordering guarantee.

Ported (with heavy adaptation -- see each module's own PORT-SEAM docstring
for what changed) from private's tests/test_nightly_morning.py
@ b34eb0b00a13c0bf0c9c95020ceddfeca71022b8. This is a representative,
budget-scoped subset of that 5480-line / ~159-test file, not a full 1:1
port -- private's file spawns and parses local `claude -p` subprocess
sessions and reads/writes local `.md`/`.json` artifact files, neither of
which exist on this host (see review_stage.py / issue_filer.py / report.py
/ morning_driver.py module docstrings for the seam each one replaced them
with). Chosen coverage targets this port's own adaptation decisions --
the places a silent behavioral drift from private would be easy to miss --
rather than re-deriving every private assertion. Dropped test classes,
with reasons, are listed in the landing PR body's test-accounting section.
Ledger L-0387's own "carried in FULL... only claude-p-spawn and
OS-scheduled-task deadman tests DROPPED" seam text is honored as: every
`claude -p` subprocess-spawn test inside TestRunAuditStage /
TestRunNightlyMorningReview is dropped (no subprocess seam exists on this
host -- see model_session.py's own PORT-SEAM), TestParseResetTime /
TestAuditBriefAsset / TestReviewBriefAsset are dropped as claude-p-adjacent
(no local artifact files, no reset-time parsing target -- see
review_stage.py's PORT-SEAM), and the OS-scheduled-task deadman tests are
dropped here and ported instead in test_nightly_deadman.py (private's
separate test_nightly_monitor_deadman.py source file, carried in full
minus its D12 local-report.md belt-and-suspenders class -- see that file's
own docstring). TestRegistrar and TestReviewSessionInjectionHardening are
NOT dropped outright: the enabled-flag-guard half of TestRegistrar is
covered below by the tasks.py periodic-gate tests (its cron-slot-parsing
half has no host analog -- procrastinate's cron string is a static
decorator argument read once from an env var, not built per-call from a
closure over a JF_CONFIG dict); TestReviewSessionInjectionHardening's
CLI-argv-escape mechanism no longer exists as an attack surface (no
subprocess, no `--repo` flag -- see review_stage.py's PORT-SEAM), but the
property it protects -- attacker-influenced issue title/body can never
redirect which repo an issue lands in -- is re-asserted below against the
real REST call construction in issue_filer.file_issue.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import procrastinate
import pytest

from tests.host.conftest import requires_postgres

# ---------------------------------------------------------------------------
# report.py -- pure formatters, no I/O
# ---------------------------------------------------------------------------


def test_format_duration_zero_seconds():
    from jobcannon.host.nightly.report import format_duration

    assert format_duration(0) == "0s"


def test_format_duration_under_minute():
    from jobcannon.host.nightly.report import format_duration

    assert format_duration(59) == "59s"


def test_format_duration_exact_minute():
    from jobcannon.host.nightly.report import format_duration

    assert format_duration(60) == "1m 0s"


def test_format_duration_minutes_and_seconds():
    from jobcannon.host.nightly.report import format_duration

    assert format_duration(125) == "2m 5s"


def test_format_duration_exact_hour():
    from jobcannon.host.nightly.report import format_duration

    assert format_duration(3600) == "1h 0m"


def test_format_duration_hours_and_minutes_drops_seconds():
    from jobcannon.host.nightly.report import format_duration

    assert format_duration(3725) == "1h 2m"


def test_verdict_line_observer_offline_beats_everything():
    from jobcannon.host.nightly.report import verdict_line

    line = verdict_line(
        observer_offline=True,
        audit_summary={"unavailable": True, "disputes": 3},
        review_incomplete=True,
    )
    assert line.startswith("**VERDICT: NO DATA**")


def test_verdict_line_incomplete_review_beats_audit_unavailable():
    from jobcannon.host.nightly.report import verdict_line

    line = verdict_line(
        observer_offline=False,
        audit_summary={"unavailable": True, "unavailable_reason": "x"},
        review_incomplete=True,
    )
    assert line.startswith("**VERDICT: INCOMPLETE**")


def test_verdict_line_audit_unavailable_beats_coverage_failure():
    from jobcannon.host.nightly.report import verdict_line

    line = verdict_line(
        observer_offline=False,
        audit_summary={
            "unavailable": True,
            "unavailable_reason": "cascade exhausted",
            "coverage_failure": True,
        },
        review_incomplete=False,
    )
    assert "AUDIT UNAVAILABLE" in line
    assert "cascade exhausted" in line


def test_verdict_line_coverage_failure_beats_disputes():
    from jobcannon.host.nightly.report import verdict_line

    line = verdict_line(
        observer_offline=False,
        audit_summary={"coverage_failure": True, "disputes": 2, "audited": 5},
        review_incomplete=False,
    )
    assert line.startswith("**VERDICT: COVERAGE ALARM**")


def test_verdict_line_disputes_present():
    from jobcannon.host.nightly.report import verdict_line

    line = verdict_line(
        observer_offline=False,
        audit_summary={"disputes": 2, "audited": 5},
        review_incomplete=False,
    )
    assert "REVIEWED" in line
    assert "2 dispute(s) out of 5" in line


def test_verdict_line_pass_when_clean():
    from jobcannon.host.nightly.report import verdict_line

    line = verdict_line(
        observer_offline=False, audit_summary={"disputes": 0, "audited": 5}, review_incomplete=False
    )
    assert line.startswith("**VERDICT: PASS**")


def test_jd_quality_flags_section_degrades_to_explicit_none():
    from jobcannon.host.nightly.report import _jd_quality_flags_section

    section = _jd_quality_flags_section({})
    assert "*none this run.*" in section
    assert "## JD Quality Flags" in section


def test_jd_quality_flags_section_lists_counts_descending():
    from jobcannon.host.nightly.report import _jd_quality_flags_section

    section = _jd_quality_flags_section(
        {"jd_quality_flagged": 3, "jd_quality_flags": {"truncated": 1, "boilerplate": 2}}
    )
    assert section.index("boilerplate") < section.index("truncated")


def test_jd_content_verdicts_section_degrades_to_explicit_none():
    from jobcannon.host.nightly.report import _jd_content_verdicts_section

    assert "*none this run.*" in _jd_content_verdicts_section({})


def test_missed_report_dates_section_degrades_to_explicit_none():
    from jobcannon.host.nightly.report import _missed_report_dates_section

    section = _missed_report_dates_section([])
    assert "*none.*" in section
    assert "## Missed Report Dates" in section


def test_missed_report_dates_section_lists_entries():
    from jobcannon.host.nightly.report import _missed_report_dates_section

    section = _missed_report_dates_section(["gap after 2026-07-18"])
    assert "- gap after 2026-07-18" in section


# ---------------------------------------------------------------------------
# review_stage.run_review_stage -- mocked run_structured_session
# ---------------------------------------------------------------------------

_REVIEW_KWARGS = dict(
    date_str="2026-07-19",
    conn=None,
    config={},
    call_model=None,
    audit_summary={"audited": 0, "disputes": 0},
    checkpoint_summary={"fail_count": 0, "rejected_reasons": None, "by_verdict": {}},
    window_coverage={},
    observer_offline=False,
    error_budget={},
    disagreement_alarm_rate=0.6,
    disagreement_alarm_min_sample=8,
    disagreement_rate_anomalous=False,
    open_issues={"status": "ok", "reason": None, "issues": []},
    prior_filed=[],
)


def test_run_review_stage_non_ok_session_is_incomplete_with_no_issues(monkeypatch):
    from jobcannon.host.nightly import review_stage
    from jobcannon.host.nightly.model_session import SessionResult

    monkeypatch.setattr(
        review_stage,
        "run_structured_session",
        lambda **kw: SessionResult(
            ok=False, data=None, error="cascade exhausted", unavailable=True
        ),
    )
    result = review_stage.run_review_stage(**_REVIEW_KWARGS)
    assert result["incomplete"] is True
    assert result["issues_to_file"] == []
    assert "INCOMPLETE" in result["report_md"]


def test_run_review_stage_empty_report_md_is_incomplete(monkeypatch):
    from jobcannon.host.nightly import review_stage
    from jobcannon.host.nightly.model_session import SessionResult

    monkeypatch.setattr(
        review_stage,
        "run_structured_session",
        lambda **kw: SessionResult(
            ok=True, data={"issues_to_file": [], "report_md": "   "}, error=None
        ),
    )
    result = review_stage.run_review_stage(**_REVIEW_KWARGS)
    assert result["incomplete"] is True
    assert result["issues_to_file"] == []


def test_run_review_stage_drops_malformed_issue_entries(monkeypatch):
    from jobcannon.host.nightly import review_stage
    from jobcannon.host.nightly.model_session import SessionResult

    raw_issues = [
        "not a dict",
        {"title": "", "body": "has no title"},
        {"title": "missing body"},
        {"title": "ok(title)", "body": "ok body", "labels": ["automated-ready"]},
    ]
    monkeypatch.setattr(
        review_stage,
        "run_structured_session",
        lambda **kw: SessionResult(
            ok=True, data={"issues_to_file": raw_issues, "report_md": "# report"}, error=None
        ),
    )
    result = review_stage.run_review_stage(**_REVIEW_KWARGS)
    assert result["incomplete"] is False
    assert len(result["issues_to_file"]) == 1
    assert result["issues_to_file"][0]["title"] == "ok(title)"


def test_run_review_stage_caps_issues_at_max_per_run(monkeypatch):
    from jobcannon.host.nightly import review_stage
    from jobcannon.host.nightly.model_session import SessionResult

    raw_issues = [{"title": f"issue {i}", "body": "b"} for i in range(20)]
    monkeypatch.setattr(
        review_stage,
        "run_structured_session",
        lambda **kw: SessionResult(
            ok=True, data={"issues_to_file": raw_issues, "report_md": "# report"}, error=None
        ),
    )
    result = review_stage.run_review_stage(**_REVIEW_KWARGS)
    assert len(result["issues_to_file"]) == review_stage._MAX_ISSUES_PER_RUN


def test_run_review_stage_happy_path(monkeypatch):
    from jobcannon.host.nightly import review_stage
    from jobcannon.host.nightly.model_session import SessionResult

    monkeypatch.setattr(
        review_stage,
        "run_structured_session",
        lambda **kw: SessionResult(
            ok=True,
            data={
                "issues_to_file": [{"title": "t", "body": "b", "labels": ["automated-ready"]}],
                "report_md": "# Nightly Monitor Report",
            },
            error=None,
        ),
    )
    result = review_stage.run_review_stage(**_REVIEW_KWARGS)
    assert result["incomplete"] is False
    assert result["report_md"].startswith("# Nightly Monitor Report")
    assert result["issues_to_file"] == [{"title": "t", "body": "b", "labels": ["automated-ready"]}]


# ---------------------------------------------------------------------------
# issue_filer.cross_check_prior_filings -- #1506 downgrade path
# ---------------------------------------------------------------------------


def test_cross_check_prior_filings_no_history_is_unchanged():
    from jobcannon.host.nightly.issue_filer import cross_check_prior_filings

    open_issues = {"status": "ok", "reason": None, "issues": []}
    assert cross_check_prior_filings(open_issues, []) == open_issues


def test_cross_check_prior_filings_already_unavailable_passthrough():
    from jobcannon.host.nightly.issue_filer import cross_check_prior_filings

    open_issues = {"status": "unavailable", "reason": "network", "issues": []}
    assert (
        cross_check_prior_filings(open_issues, [{"outcome": "created", "number": 1}]) == open_issues
    )


def test_cross_check_prior_filings_prior_visible_is_unchanged():
    from jobcannon.host.nightly.issue_filer import cross_check_prior_filings

    open_issues = {"status": "ok", "reason": None, "issues": [{"number": 42, "title": "x"}]}
    prior = [{"outcome": "created", "number": 42, "url": "https://x/issues/42"}]
    assert cross_check_prior_filings(open_issues, prior) == open_issues


def test_cross_check_prior_filings_prior_invisible_downgrades_to_unavailable():
    """#1506: a prior "created" filing absent from the freshly fetched open
    list means the fetch cannot be trusted as a dedup reference -- downgrade
    rather than silently disable dedup."""
    from jobcannon.host.nightly.issue_filer import cross_check_prior_filings

    open_issues = {"status": "ok", "reason": None, "issues": [{"number": 7, "title": "unrelated"}]}
    prior = [{"outcome": "created", "number": 99, "url": "https://x/issues/99"}]
    result = cross_check_prior_filings(open_issues, prior)
    assert result["status"] == "unavailable"
    assert "#99" in result["reason"]
    # Original issues list is preserved for manual inspection even though
    # dedup is disabled.
    assert result["issues"] == open_issues["issues"]


def test_cross_check_prior_filings_uses_url_when_number_absent():
    from jobcannon.host.nightly.issue_filer import cross_check_prior_filings

    open_issues = {"status": "ok", "reason": None, "issues": [{"number": 5, "title": "x"}]}
    prior = [{"outcome": "created", "url": "https://github.com/x/y/issues/5"}]
    assert cross_check_prior_filings(open_issues, prior) == open_issues


# ---------------------------------------------------------------------------
# morning_driver._SAMPLER_TASK_NAME -- positive control against the live
# procrastinate registration (see morning_driver.py's own comment: a bare
# function-name string would silently zero every tick this driver reads).
# ---------------------------------------------------------------------------


def test_sampler_task_name_constant_matches_live_procrastinate_registration():
    from jobcannon.host import tasks
    from jobcannon.host.nightly import morning_driver

    assert morning_driver._SAMPLER_TASK_NAME == tasks.nightly_sampler.name
    assert morning_driver._SAMPLER_HEALTH_SOURCE == "nightly_sampler"


# ---------------------------------------------------------------------------
# Real Postgres: compute_window_coverage / checkpoint_summary
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _nightly_procrastinate_schema(postgres_test_dsn):
    """Apply procrastinate's own queue schema once per throwaway test DB --
    duplicated from tests/host/test_nightly_sampler.py's own fixture (see
    that file's docstring for the "Two-Schema-Authorities" rationale); not
    shared via conftest.py to keep each nightly test file self-contained,
    matching the existing per-file convention.

    ``postgres_test_dsn`` is session-scoped and shared by every test file in
    the same pytest session/xdist worker (tests/host/conftest.py), so
    whichever of this fixture and test_nightly_sampler.py's own same-named
    fixture runs SECOND hits ``DuplicateObject`` on procrastinate's
    non-idempotent CREATE TYPE/TABLE statements -- swallow it here rather
    than touching that already-landed file's fixture.
    """
    from procrastinate.exceptions import ConnectorException

    from jobcannon.host import tasks

    with tasks.app.replace_connector(procrastinate.PsycopgConnector(conninfo=postgres_test_dsn)):
        with tasks.app.open():
            try:
                tasks.app.schema_manager.apply_schema()
            except ConnectorException:
                pass  # already applied by another nightly test file's fixture this session


def _insert_tick_at(conn, *, task_name: str, when: datetime, status: str = "succeeded") -> int:
    """Insert one terminal procrastinate_jobs row whose terminal event lands
    at exactly *when* -- unlike test_nightly_sampler.py's _insert_terminal_job
    (which always terminates at real now()), these tests need the tick
    placed at a specific offset inside a synthetic window.

    Pins the session to UTC first: tests/host/conftest.py's db_conn is a
    bare psycopg.connect(), unlike production's pooled connections (see
    jobcannon/db/pool.py's _configure, which pins every real connection to
    UTC) -- without this, a naive Python datetime bound into a timestamptz
    column is reinterpreted in whatever timezone the local Postgres server
    happens to default to (verified: America/Los_Angeles on this box),
    silently shifting every timestamp this helper writes.
    """
    conn.execute("SET TIME ZONE 'UTC'")
    row = conn.execute(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, status) "
        "VALUES ('maintenance', %s, %s::procrastinate_job_status) RETURNING id",
        (task_name, status),
    ).fetchone()
    job_id = row["id"]
    event_type = "succeeded" if status == "succeeded" else "failed"
    conn.execute(
        "INSERT INTO procrastinate_events (job_id, type, at) "
        "VALUES (%s, 'started'::procrastinate_job_event_type, %s)",
        (job_id, when - timedelta(seconds=5)),
    )
    conn.execute(
        f"INSERT INTO procrastinate_events (job_id, type, at) "
        f"VALUES (%s, '{event_type}'::procrastinate_job_event_type, %s)",
        (job_id, when),
    )
    return job_id


@requires_postgres
def test_compute_window_coverage_zero_ticks_is_fully_offline(
    db_conn, _nightly_procrastinate_schema
):
    from jobcannon.host.nightly.morning_driver import compute_window_coverage

    window_start = datetime(2026, 7, 19, 5, 0, 0)
    window_end = datetime(2026, 7, 19, 6, 0, 0)
    coverage, coverage_gap = compute_window_coverage(
        db_conn,
        {"coverage_gap_threshold_s": 900},
        window_start_utc=window_start,
        window_end_utc=window_end,
    )
    assert coverage["observed_ticks"] == 0
    assert coverage["longest_gap_s"] == 3600.0
    assert coverage_gap is True


@requires_postgres
def test_compute_window_coverage_one_tick_reports_larger_edge_gap(
    db_conn, _nightly_procrastinate_schema
):
    """One tick 10 minutes after window_start, in a 100-minute window: the
    longest gap is the 90-minute edge to window_end, not the 10-minute gap
    to window_start."""
    from jobcannon.host.nightly.morning_driver import _SAMPLER_TASK_NAME, compute_window_coverage

    window_start = datetime(2026, 7, 19, 5, 0, 0)
    window_end = window_start + timedelta(minutes=100)
    _insert_tick_at(
        db_conn, task_name=_SAMPLER_TASK_NAME, when=window_start + timedelta(minutes=10)
    )

    coverage, coverage_gap = compute_window_coverage(
        db_conn,
        {"coverage_gap_threshold_s": 900},
        window_start_utc=window_start,
        window_end_utc=window_end,
    )
    assert coverage["observed_ticks"] == 1
    assert coverage["longest_gap_s"] == pytest.approx(90 * 60)
    assert coverage_gap is True  # 90min gap > 900s threshold


@requires_postgres
def test_compute_window_coverage_gap_under_threshold_not_flagged(
    db_conn, _nightly_procrastinate_schema
):
    from jobcannon.host.nightly.morning_driver import _SAMPLER_TASK_NAME, compute_window_coverage

    window_start = datetime(2026, 7, 19, 5, 0, 0)
    window_end = window_start + timedelta(minutes=20)
    for offset in (5, 10, 15):
        _insert_tick_at(
            db_conn, task_name=_SAMPLER_TASK_NAME, when=window_start + timedelta(minutes=offset)
        )

    coverage, coverage_gap = compute_window_coverage(
        db_conn,
        {"coverage_gap_threshold_s": 900},
        window_start_utc=window_start,
        window_end_utc=window_end,
    )
    assert coverage["observed_ticks"] == 3
    assert coverage["longest_gap_s"] == pytest.approx(5 * 60)
    assert coverage_gap is False  # 5min < 900s threshold


@requires_postgres
def test_compute_window_coverage_wrong_task_name_would_zero_ticks(
    db_conn, _nightly_procrastinate_schema
):
    """Regression guard for the task_name/health-source split fix: inserting
    under the BARE function name (the pre-fix, wrong filter value) must
    yield zero observed ticks, proving the filter really is the dotted
    procrastinate registration name and not the bare string."""
    from jobcannon.host.nightly.morning_driver import compute_window_coverage

    window_start = datetime(2026, 7, 19, 5, 0, 0)
    window_end = window_start + timedelta(minutes=20)
    _insert_tick_at(db_conn, task_name="nightly_sampler", when=window_start + timedelta(minutes=5))

    coverage, _ = compute_window_coverage(
        db_conn,
        {"coverage_gap_threshold_s": 900},
        window_start_utc=window_start,
        window_end_utc=window_end,
    )
    assert coverage["observed_ticks"] == 0


@requires_postgres
def test_checkpoint_summary_rejected_reasons_is_none_not_zero(
    db_conn, _nightly_procrastinate_schema
):
    """checkpoint_summary's rejected_reasons must be the literal None
    (genuinely unavailable -- PASS/ANOMALY verdict detail is never
    persisted), never a fabricated 0."""
    from jobcannon.host.nightly.morning_driver import checkpoint_summary

    window_start = datetime(2026, 7, 19, 5, 0, 0)
    window_end = window_start + timedelta(hours=1)
    summary = checkpoint_summary(db_conn, window_start, window_end)
    assert summary["rejected_reasons"] is None
    assert summary["by_verdict"] == {}
    assert summary["fail_count"] == 0


@requires_postgres
def test_checkpoint_summary_fail_count_reflects_health_log_error_rows(
    db_conn, _nightly_procrastinate_schema
):
    from jobcannon.host.nightly.morning_driver import checkpoint_summary

    db_conn.execute("SET TIME ZONE 'UTC'")  # see _insert_tick_at's docstring
    window_start = datetime(2026, 7, 19, 5, 0, 0)
    window_end = window_start + timedelta(hours=1)
    recorded_at = window_start + timedelta(minutes=30)
    for _ in range(2):
        db_conn.execute(
            "INSERT INTO scan_health_log (recorded_at, payload) VALUES (%s, %s)",
            (recorded_at, '{"source": "nightly_sampler", "level": "ERROR"}'),
        )
    # A WARNING row and a row from a different source must not count.
    db_conn.execute(
        "INSERT INTO scan_health_log (recorded_at, payload) VALUES (%s, %s)",
        (recorded_at, '{"source": "nightly_sampler", "level": "WARNING"}'),
    )
    db_conn.execute(
        "INSERT INTO scan_health_log (recorded_at, payload) VALUES (%s, %s)",
        (recorded_at, '{"source": "db_storage_check", "level": "ERROR"}'),
    )

    summary = checkpoint_summary(db_conn, window_start, window_end)
    assert summary["fail_count"] == 2


# ---------------------------------------------------------------------------
# D12: state.save_state (report + last_report_date) must be durable BEFORE
# the exception-prone issue-filing tail runs, so a crash while filing
# issues never also produces a false deadman alarm for a report that was,
# in fact, produced.
# ---------------------------------------------------------------------------


@requires_postgres
def test_d12_state_persisted_before_issue_filing_tail_even_on_filing_crash(
    db_conn, monkeypatch, _nightly_procrastinate_schema
):
    from jobcannon.host.nightly import morning_driver, state as nightly_state

    monkeypatch.setenv("JC_NIGHTLY_ISSUE_REPO", "example-org/example-repo")
    monkeypatch.setenv("JC_NIGHTLY_GH_TOKEN", "ghp_dummy")

    monkeypatch.setattr(
        morning_driver, "run_audit_stage", lambda *a, **kw: {"audited": 0, "disputes": 0}
    )
    monkeypatch.setattr(morning_driver, "build_nightly_error_budget", lambda *a, **kw: {})
    monkeypatch.setattr(morning_driver, "markdown_section", lambda budget: "")
    monkeypatch.setattr(
        morning_driver,
        "list_open_issues",
        lambda repo, token: {"status": "ok", "reason": None, "issues": []},
    )
    monkeypatch.setattr(
        morning_driver,
        "run_review_stage",
        lambda **kw: {
            "incomplete": False,
            "report_md": "# stub report",
            "issues_to_file": [{"title": "t", "body": "b", "labels": []}],
        },
    )

    def _boom(*a, **kw):
        raise RuntimeError("GitHub API is down")

    monkeypatch.setattr(morning_driver, "file_issue", _boom)

    now = datetime(2026, 7, 19, 5, 30, 0)
    with pytest.raises(RuntimeError, match="GitHub API is down"):
        morning_driver._run(db_conn, _now=now)

    # The crash happened in the issue-filing tail, AFTER state.save_state
    # already committed the report fields -- read-your-own-writes on this
    # same connection (state.save_state's own commit_unless_nested no-ops
    # inside this test's ambient transaction, but the write itself already
    # happened) proves the D12 ordering held.
    state = nightly_state.load_state(db_conn)
    assert state["last_report_date"] == "2026-07-19"
    assert state["last_morning_status"] == "ok"
    # The crash means filing never completed, so last_filed_issues must
    # NOT have been advanced past whatever it was before this run.
    assert state.get("last_filed_issues") in (None, [])


@requires_postgres
def test_d12_state_records_filed_issues_on_success(
    db_conn, monkeypatch, _nightly_procrastinate_schema
):
    from jobcannon.host.nightly import morning_driver, state as nightly_state

    monkeypatch.setenv("JC_NIGHTLY_ISSUE_REPO", "example-org/example-repo")
    monkeypatch.setenv("JC_NIGHTLY_GH_TOKEN", "ghp_dummy")

    monkeypatch.setattr(
        morning_driver, "run_audit_stage", lambda *a, **kw: {"audited": 0, "disputes": 0}
    )
    monkeypatch.setattr(morning_driver, "build_nightly_error_budget", lambda *a, **kw: {})
    monkeypatch.setattr(morning_driver, "markdown_section", lambda budget: "")
    monkeypatch.setattr(
        morning_driver,
        "list_open_issues",
        lambda repo, token: {"status": "ok", "reason": None, "issues": []},
    )
    monkeypatch.setattr(
        morning_driver,
        "run_review_stage",
        lambda **kw: {
            "incomplete": False,
            "report_md": "# stub report",
            "issues_to_file": [{"title": "t", "body": "b", "labels": ["automated-ready"]}],
        },
    )
    monkeypatch.setattr(
        morning_driver,
        "file_issue",
        lambda repo, token, title, body, labels: {
            "title": title,
            "labels": labels,
            "outcome": "created",
            "url": "https://github.com/x/y/issues/1",
            "number": 1,
            "reason": None,
        },
    )

    now = datetime(2026, 7, 19, 5, 30, 0)
    result = morning_driver._run(db_conn, _now=now)

    assert result["issues_filed"] == 1
    state = nightly_state.load_state(db_conn)
    assert state["last_filed_issues"] == [
        {
            "title": "t",
            "labels": ["automated-ready"],
            "outcome": "created",
            "url": "https://github.com/x/y/issues/1",
            "number": 1,
            "reason": None,
        }
    ]


@requires_postgres
def test_run_records_per_issue_outcomes_when_review_proposes_multiple_issues(
    db_conn, monkeypatch, _nightly_procrastinate_schema
):
    """Host analog of private's TestFiledIssuesArtifact all-succeed/
    all-fail/partial-fail cases (#1568), collapsed into one test since the
    ``filed`` accumulation loop in morning_driver._run is the same code
    path regardless of outcome mix -- exercises multi-issue accumulation,
    which test_d12_state_records_filed_issues_on_success (a single-issue
    happy path) does not."""
    from jobcannon.host.nightly import morning_driver, state as nightly_state

    monkeypatch.setenv("JC_NIGHTLY_ISSUE_REPO", "example-org/example-repo")
    monkeypatch.setenv("JC_NIGHTLY_GH_TOKEN", "ghp_dummy")

    monkeypatch.setattr(
        morning_driver, "run_audit_stage", lambda *a, **kw: {"audited": 0, "disputes": 0}
    )
    monkeypatch.setattr(morning_driver, "build_nightly_error_budget", lambda *a, **kw: {})
    monkeypatch.setattr(morning_driver, "markdown_section", lambda budget: "")
    monkeypatch.setattr(
        morning_driver,
        "list_open_issues",
        lambda repo, token: {"status": "ok", "reason": None, "issues": []},
    )
    monkeypatch.setattr(
        morning_driver,
        "run_review_stage",
        lambda **kw: {
            "incomplete": False,
            "report_md": "# stub report",
            "issues_to_file": [
                {"title": "fix(x): a", "body": "body a", "labels": ["automated-ready"]},
                {"title": "fix(y): b", "body": "body b", "labels": []},
            ],
        },
    )

    def _file_issue(repo, token, title, body, labels):
        if title == "fix(x): a":
            return {
                "title": title,
                "labels": labels,
                "outcome": "created",
                "url": "https://github.com/x/y/issues/1",
                "number": 1,
                "reason": None,
            }
        return {
            "title": title,
            "labels": labels,
            "outcome": "failed",
            "url": None,
            "number": None,
            "reason": "GitHub issue create returned 422",
        }

    monkeypatch.setattr(morning_driver, "file_issue", _file_issue)

    now = datetime(2026, 7, 19, 5, 30, 0)
    result = morning_driver._run(db_conn, _now=now)

    # issues_filed is len(filed) -- every ATTEMPTED filing, success or
    # failure, not just the ones that actually created an issue (matches
    # morning_driver._run's own "filed.append(file_issue(...))" loop,
    # unconditional on outcome).
    assert result["issues_filed"] == 2
    state = nightly_state.load_state(db_conn)
    filed_by_title = {e["title"]: e for e in state["last_filed_issues"]}
    assert len(filed_by_title) == 2
    assert filed_by_title["fix(x): a"]["outcome"] == "created"
    assert filed_by_title["fix(x): a"]["url"]
    assert filed_by_title["fix(y): b"]["outcome"] == "failed"
    assert filed_by_title["fix(y): b"]["reason"]


@requires_postgres
def test_run_preserves_prior_filed_issues_when_filing_skipped_entirely(
    db_conn, monkeypatch, _nightly_procrastinate_schema
):
    """When repo/token/open_issues gating skips filing entirely (see
    morning_driver.py's ``if repo and token and open_issues... status ==
    "ok"`` guard), ``last_filed_issues`` must NOT be reset to [] -- doing so
    would erase the previous night's dedup reference that
    cross_check_prior_filings needs on the NEXT run. Deliberate behavioral
    difference from private's filed_issues.json (which always wrote a
    fresh artifact, including an empty one, every run -- see
    TestFiledIssuesArtifact::test_open_issues_unavailable_writes_skipped_
    entries in the private original); disclosed in the landing PR body."""
    from jobcannon.host.nightly import morning_driver, state as nightly_state

    monkeypatch.setenv("JC_NIGHTLY_ISSUE_REPO", "example-org/example-repo")
    monkeypatch.setenv("JC_NIGHTLY_GH_TOKEN", "ghp_dummy")

    monkeypatch.setattr(
        morning_driver, "run_audit_stage", lambda *a, **kw: {"audited": 0, "disputes": 0}
    )
    monkeypatch.setattr(morning_driver, "build_nightly_error_budget", lambda *a, **kw: {})
    monkeypatch.setattr(morning_driver, "markdown_section", lambda budget: "")

    seed_state = nightly_state.load_state(db_conn)
    nightly_state.save_state(
        db_conn,
        {
            **seed_state,
            "last_filed_issues": [
                {
                    "title": "prior night's issue",
                    "labels": [],
                    "outcome": "created",
                    "url": "https://github.com/x/y/issues/1",
                    "number": 1,
                    "reason": None,
                }
            ],
        },
        base=None,
    )

    monkeypatch.setattr(
        morning_driver,
        "list_open_issues",
        lambda repo, token: {"status": "unavailable", "reason": "network", "issues": []},
    )
    monkeypatch.setattr(
        morning_driver,
        "run_review_stage",
        lambda **kw: {
            "incomplete": False,
            "report_md": "# stub report",
            "issues_to_file": [{"title": "new proposal", "body": "b", "labels": []}],
        },
    )

    def _boom(*a, **kw):
        raise AssertionError("file_issue must not be called when open_issues fetch failed")

    monkeypatch.setattr(morning_driver, "file_issue", _boom)

    now = datetime(2026, 7, 19, 5, 30, 0)
    result = morning_driver._run(db_conn, _now=now)

    assert result["issues_filed"] == 0
    state = nightly_state.load_state(db_conn)
    assert state["last_filed_issues"] == [
        {
            "title": "prior night's issue",
            "labels": [],
            "outcome": "created",
            "url": "https://github.com/x/y/issues/1",
            "number": 1,
            "reason": None,
        }
    ]


@requires_postgres
def test_run_does_not_file_issues_when_open_issues_fetch_unavailable(
    db_conn, monkeypatch, _nightly_procrastinate_schema
):
    """repo+token resolved but the dedup fetch itself failed -- filing must
    be skipped entirely (never file blind), and a warning logged, not a
    silent drop."""
    from jobcannon.host.nightly import morning_driver, state as nightly_state

    monkeypatch.setenv("JC_NIGHTLY_ISSUE_REPO", "example-org/example-repo")
    monkeypatch.setenv("JC_NIGHTLY_GH_TOKEN", "ghp_dummy")

    monkeypatch.setattr(
        morning_driver, "run_audit_stage", lambda *a, **kw: {"audited": 0, "disputes": 0}
    )
    monkeypatch.setattr(morning_driver, "build_nightly_error_budget", lambda *a, **kw: {})
    monkeypatch.setattr(morning_driver, "markdown_section", lambda budget: "")
    monkeypatch.setattr(
        morning_driver,
        "list_open_issues",
        lambda repo, token: {"status": "unavailable", "reason": "network", "issues": []},
    )
    monkeypatch.setattr(
        morning_driver,
        "run_review_stage",
        lambda **kw: {
            "incomplete": False,
            "report_md": "# stub report",
            "issues_to_file": [{"title": "t", "body": "b", "labels": []}],
        },
    )

    def _boom(*a, **kw):
        raise AssertionError("file_issue must not be called when open_issues fetch failed")

    monkeypatch.setattr(morning_driver, "file_issue", _boom)

    now = datetime(2026, 7, 19, 5, 30, 0)
    result = morning_driver._run(db_conn, _now=now)

    assert result["issues_filed"] == 0
    state = nightly_state.load_state(db_conn)
    assert state["last_report_date"] == "2026-07-19"


# ---------------------------------------------------------------------------
# audit_stage.run_audit_stage -- random.sample under JC_NIGHTLY_AUDIT_MAX_JOBS
# (design note item 5). New relative to private, per ledger L-0387's own
# seam text ("NEW sampling + per-night-ceiling tests added for the reworked
# score audit") -- private sampled top-N by axis_sum descending, so it had
# no equivalent ceiling-vs-cohort-size test to port.
# ---------------------------------------------------------------------------


def _audit_cfg(*, max_jobs):
    return {
        "score_threshold": 20,
        "lookback_days": 3,
        "max_jobs": max_jobs,
        "batch_size": 5,
        "max_batch_input_chars": 40_000,
        "max_skip_attempts": 2,
        "max_batch_retries": 1,
        "coverage_alarm_threshold": 0.80,
        "failed_batch_fraction_alarm_threshold": 0.75,
    }


def _insert_eligible_posting(db_conn, dedup_key):
    """Minimal eligible-posting insert for audit_stage sampling tests --
    axis_sum 22 (above the default score_threshold=20), first_seen defaults
    to now() (inside the default 3-day lookback)."""
    from psycopg.types.json import Jsonb

    company = f"co-{dedup_key}"
    db_conn.execute("INSERT INTO companies (name) VALUES (%s)", (company,))
    cid = db_conn.execute("SELECT id FROM companies WHERE name = %s", (company,)).fetchone()["id"]
    db_conn.execute(
        "INSERT INTO postings "
        "(dedup_key, company_id, title, company, location, jd_full, sub_scores_json) "
        "VALUES (%s, %s, 'Data Scientist', %s, 'Remote', "
        "'A meaningful job description body.', %s)",
        (
            dedup_key,
            cid,
            company,
            Jsonb(
                {
                    "title_fit": 4,
                    "location_fit": 4,
                    "comp_fit": 3,
                    "domain_match": 4,
                    "seniority_match": 3,
                    "skills_match": 4,
                }
            ),
        ),
    )


@requires_postgres
def test_run_audit_stage_samples_exactly_ceiling_when_cohort_exceeds_it(
    db_conn, _nightly_procrastinate_schema
):
    from jobcannon.db.pool import EngineCompatConnection
    from jobcannon.host.nightly.audit_stage import run_audit_stage

    for i in range(5):
        _insert_eligible_posting(db_conn, f"cohort-big-{i}")

    summary = run_audit_stage(
        EngineCompatConnection(db_conn),
        {"audit": _audit_cfg(max_jobs=3)},
        call_model=None,
        config={},
        rng=random.Random(1),
    )
    assert summary["total_candidates"] == 3


@requires_postgres
def test_run_audit_stage_takes_whole_cohort_when_smaller_than_ceiling(
    db_conn, _nightly_procrastinate_schema
):
    """random.sample(pop, k) raises ValueError when k > len(pop) --
    run_audit_stage's min(ceiling, len(eligible)) guard must prevent that
    whenever the eligible pool is smaller than the configured ceiling."""
    from jobcannon.db.pool import EngineCompatConnection
    from jobcannon.host.nightly.audit_stage import run_audit_stage

    for i in range(2):
        _insert_eligible_posting(db_conn, f"cohort-small-{i}")

    summary = run_audit_stage(
        EngineCompatConnection(db_conn),
        {"audit": _audit_cfg(max_jobs=15)},
        call_model=None,
        config={},
        rng=random.Random(1),
    )
    assert summary["total_candidates"] == 2


@requires_postgres
def test_run_audit_stage_max_jobs_zero_samples_nothing_and_never_dispatches(
    db_conn, _nightly_procrastinate_schema
):
    """config.py clamps JC_NIGHTLY_AUDIT_MAX_JOBS at floor 0 (not 1) -- a
    zeroed ceiling must empty the cohort even when eligible postings exist,
    and must never reach the call_model dispatcher (asserted via a
    call_model stub that raises if invoked)."""
    from jobcannon.db.pool import EngineCompatConnection
    from jobcannon.host.nightly.audit_stage import run_audit_stage

    _insert_eligible_posting(db_conn, "cohort-zero-ceiling")

    def _boom(*a, **kw):
        raise AssertionError("call_model must not be invoked when max_jobs clamps to 0")

    summary = run_audit_stage(
        EngineCompatConnection(db_conn),
        {"audit": _audit_cfg(max_jobs=0)},
        call_model=_boom,
        config={},
        rng=random.Random(1),
    )
    assert summary["total_candidates"] == 0
    assert summary["audited"] == 0


@requires_postgres
def test_run_audit_stage_empty_eligible_pool_is_a_clean_noop(
    db_conn, _nightly_procrastinate_schema
):
    """No eligible postings at all (design note Q1) -- must return a clean
    zeroed summary, not raise, and never reach the model dispatcher."""
    from jobcannon.db.pool import EngineCompatConnection
    from jobcannon.host.nightly.audit_stage import run_audit_stage

    def _boom(*a, **kw):
        raise AssertionError("call_model must not be invoked over an empty eligible pool")

    summary = run_audit_stage(
        EngineCompatConnection(db_conn),
        {"audit": _audit_cfg(max_jobs=15)},
        call_model=_boom,
        config={},
        rng=random.Random(1),
    )
    assert summary["total_candidates"] == 0
    assert summary["unavailable"] is False


# ---------------------------------------------------------------------------
# tasks.py periodic wrappers -- JC_NIGHTLY_MONITOR_ENABLED gate fires before
# any import, mirroring test_nightly_sampler.py's identical-shape test for
# nightly_sampler (ledger L-0471). Host analog of private's TestRegistrar::
# test_guard_reads_enabled_flag (its cron-slot-parsing half has no host
# analog -- see this module's docstring).
# ---------------------------------------------------------------------------


def test_nightly_review_task_skips_when_disabled_and_touches_no_db(monkeypatch):
    monkeypatch.delenv("JC_NIGHTLY_MONITOR_ENABLED", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("connection_factory must not be called when disabled")

    monkeypatch.setattr("jobcannon.db.connection_factory", _boom)
    from jobcannon.host import tasks

    assert tasks.nightly_review(0) == {"skipped": "disabled"}


def test_nightly_deadman_task_skips_when_disabled_and_touches_no_db(monkeypatch):
    monkeypatch.delenv("JC_NIGHTLY_MONITOR_ENABLED", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("connection_factory must not be called when disabled")

    monkeypatch.setattr("jobcannon.db.connection_factory", _boom)
    from jobcannon.host import tasks

    assert tasks.nightly_deadman(0) == {"skipped": "disabled"}


# ---------------------------------------------------------------------------
# issue_filer.list_open_issues / file_issue -- GitHub REST client (design
# note item 6). Private's TestListOpenIssues / TestFileIssue drove these
# through a mocked `_gh` CLI subprocess seam that no longer exists (see
# issue_filer.py's own PORT-SEAM); these drive the real `requests` call
# construction instead, mocking only requests.get/requests.post.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text="", links=None, content=b"x"):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.links = links or {}
        self.content = content

    def json(self):
        return self._json_data


def test_list_open_issues_success_filters_pull_requests(monkeypatch):
    from jobcannon.host.nightly import issue_filer

    page = [
        {"number": 1, "title": "a bug", "body": "b", "html_url": "https://x/issues/1"},
        {"number": 2, "title": "a pr", "pull_request": {}, "html_url": "https://x/pull/2"},
    ]
    monkeypatch.setattr(
        issue_filer.requests, "get", lambda *a, **kw: _FakeResponse(200, json_data=page, links={})
    )
    result = issue_filer.list_open_issues("owner/repo", "tok")
    assert result["status"] == "ok"
    assert [i["number"] for i in result["issues"]] == [1]


def test_list_open_issues_non_200_returns_unavailable(monkeypatch):
    from jobcannon.host.nightly import issue_filer

    monkeypatch.setattr(
        issue_filer.requests, "get", lambda *a, **kw: _FakeResponse(403, text="rate limited")
    )
    result = issue_filer.list_open_issues("owner/repo", "tok")
    assert result["status"] == "unavailable"
    assert result["issues"] == []


def test_list_open_issues_network_exception_returns_unavailable(monkeypatch):
    import requests as _requests

    from jobcannon.host.nightly import issue_filer

    def _raise(*a, **kw):
        raise _requests.RequestException("connection reset")

    monkeypatch.setattr(issue_filer.requests, "get", _raise)
    result = issue_filer.list_open_issues("owner/repo", "tok")
    assert result["status"] == "unavailable"
    assert "connection reset" in result["reason"]


def test_file_issue_success_returns_created_record(monkeypatch):
    from jobcannon.host.nightly import issue_filer

    captured = {}

    def _post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(
            201, json_data={"html_url": "https://github.com/owner/repo/issues/9", "number": 9}
        )

    monkeypatch.setattr(issue_filer.requests, "post", _post)
    record = issue_filer.file_issue("owner/repo", "tok", "a title", "a body", ["automated-ready"])
    assert record["outcome"] == "created"
    assert record["url"] == "https://github.com/owner/repo/issues/9"
    assert record["number"] == 9
    assert record["reason"] is None
    assert captured["url"] == "https://api.github.com/repos/owner/repo/issues"


def test_file_issue_non_201_returns_failed_with_reason(monkeypatch):
    from jobcannon.host.nightly import issue_filer

    monkeypatch.setattr(
        issue_filer.requests, "post", lambda *a, **kw: _FakeResponse(422, text="Validation Failed")
    )
    record = issue_filer.file_issue("owner/repo", "tok", "t", "b", [])
    assert record["outcome"] == "failed"
    assert record["url"] is None
    assert "422" in record["reason"]


def test_file_issue_network_exception_returns_failed_with_reason(monkeypatch):
    import requests as _requests

    from jobcannon.host.nightly import issue_filer

    def _raise(*a, **kw):
        raise _requests.RequestException("timed out")

    monkeypatch.setattr(issue_filer.requests, "post", _raise)
    record = issue_filer.file_issue("owner/repo", "tok", "t", "b", [])
    assert record["outcome"] == "failed"
    assert "timed out" in record["reason"]


def test_file_issue_title_and_body_are_stripped_and_clipped(monkeypatch):
    from jobcannon.host.nightly import issue_filer

    captured = {}

    def _post(url, *, headers, json, timeout):
        captured["json"] = json
        return _FakeResponse(201, json_data={"html_url": "https://x/issues/1", "number": 1})

    monkeypatch.setattr(issue_filer.requests, "post", _post)
    long_title = "  " + ("t" * (issue_filer._ISSUE_TITLE_CLIP + 50)) + "  "
    long_body = "b" * (issue_filer._ISSUE_BODY_CLIP + 50)
    issue_filer.file_issue("owner/repo", "tok", long_title, long_body, [])
    assert len(captured["json"]["title"]) == issue_filer._ISSUE_TITLE_CLIP
    assert len(captured["json"]["body"]) == issue_filer._ISSUE_BODY_CLIP
    assert not captured["json"]["title"].startswith(" ")


def test_file_issue_targets_configured_repo_regardless_of_adversarial_body_content(monkeypatch):
    """Host analog of private's TestReviewSessionInjectionHardening
    (#1183). The review session has no tools and no subprocess argv on this
    host (see review_stage.py's PORT-SEAM and its system prompt: no tools
    are granted -- the driver, never the model, is the only thing allowed
    to touch the database or GitHub), so the CLI-argv-escape mechanism that
    regression guarded against no longer exists as an attack surface. What
    survives -- the repo a filed issue lands in must come ONLY from the
    caller-supplied repo parameter, never from model-controlled
    title/body/labels content -- still applies, since title/body/labels
    are LLM output. Proves an adversarial body/title containing
    repo-looking text is sent verbatim as an inert JSON value and never
    changes the REST target URL."""
    from jobcannon.host.nightly import issue_filer

    captured = {}

    def _post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(201, json_data={"html_url": "https://x/issues/1", "number": 1})

    monkeypatch.setattr(issue_filer.requests, "post", _post)
    malicious_title = "evil finding --repo attacker/evil-repo"
    malicious_body = "malicious body --repo attacker/evil-repo --title pwned"
    issue_filer.file_issue(
        "example-org/example-repo", "tok", malicious_title, malicious_body, ["automated-ready"]
    )
    assert captured["url"] == "https://api.github.com/repos/example-org/example-repo/issues"
    assert "attacker/evil-repo" not in captured["url"]
    # the embedded string survives only as an inert JSON body value
    assert "attacker/evil-repo" in captured["json"]["body"]
