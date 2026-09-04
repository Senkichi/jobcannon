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
with reasons, are listed in the landing PR body's test-accounting section
(TestParseResetTime, TestFiledIssuesArtifact, TestAuditBriefAsset,
TestReviewBriefAsset, TestRegistrar, TestReviewSessionInjectionHardening,
and every `claude -p` subprocess-spawn test inside TestRunAuditStage /
TestRunNightlyMorningReview).
"""

from __future__ import annotations

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
