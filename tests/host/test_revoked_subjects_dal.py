"""jobcannon.db._revoked_subjects (issue #159) plus the periodic prune-task
wiring, mirroring tests/host/test_events_retention.py's split between real-
Postgres predicate coverage (revoke_subject / is_subject_revoked /
prune_expired_revocations) and connector-free wiring coverage
(reap_revoked_subjects, prune_expired_revocations mocked out)."""

from __future__ import annotations

import contextlib

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _backdate_expiry(conn, user_id: str, *, minutes_ago: int) -> None:
    conn.execute(
        "UPDATE revoked_subjects SET expires_at = now() - make_interval(mins => %s) "
        "WHERE clerk_user_id = %s",
        (minutes_ago, user_id),
    )


def test_revoke_subject_creates_an_unexpired_row(db_conn):
    from jobcannon.db._revoked_subjects import revoke_subject

    revoke_subject(db_conn, "user_dal_1")

    row = db_conn.execute(
        "SELECT revoked_at, expires_at FROM revoked_subjects WHERE clerk_user_id = %s",
        ("user_dal_1",),
    ).fetchone()
    assert row is not None
    assert row["expires_at"] > row["revoked_at"]


def test_is_subject_revoked_true_for_a_freshly_revoked_subject(db_conn):
    from jobcannon.db._revoked_subjects import is_subject_revoked, revoke_subject

    revoke_subject(db_conn, "user_dal_2")

    assert is_subject_revoked(db_conn, "user_dal_2") is True


def test_is_subject_revoked_false_for_a_subject_with_no_row(db_conn):
    from jobcannon.db._revoked_subjects import is_subject_revoked

    assert is_subject_revoked(db_conn, "user_dal_never_revoked") is False


def test_is_subject_revoked_false_once_the_row_has_expired(db_conn):
    """An expired-but-not-yet-pruned row must never keep denying access --
    pruning is prune_expired_revocations's job, not this read path's."""
    from jobcannon.db._revoked_subjects import is_subject_revoked, revoke_subject

    revoke_subject(db_conn, "user_dal_3")
    _backdate_expiry(db_conn, "user_dal_3", minutes_ago=1)

    assert is_subject_revoked(db_conn, "user_dal_3") is False


def test_is_subject_revoked_does_not_block_a_different_subject(db_conn):
    from jobcannon.db._revoked_subjects import is_subject_revoked, revoke_subject

    revoke_subject(db_conn, "user_dal_revoked")

    assert is_subject_revoked(db_conn, "user_dal_innocent_bystander") is False


def test_revoke_subject_called_twice_extends_the_window_rather_than_erroring(db_conn):
    """The upsert is deliberate (module docstring): a second revoke_subject
    call for the same subject -- e.g. account.py's synchronous write
    followed later by the user.deleted webhook's write -- must not raise on
    the primary-key conflict, and must move expires_at forward rather than
    leaving the first call's (earlier) value in place."""
    from jobcannon.db._revoked_subjects import revoke_subject

    revoke_subject(db_conn, "user_dal_4")
    first = db_conn.execute(
        "SELECT expires_at FROM revoked_subjects WHERE clerk_user_id = %s", ("user_dal_4",)
    ).fetchone()["expires_at"]

    # Force a strictly earlier first expiry so a second, later call is
    # unambiguously an extension rather than noise from two now()s a few
    # microseconds apart.
    _backdate_expiry(db_conn, "user_dal_4", minutes_ago=5)
    backdated = db_conn.execute(
        "SELECT expires_at FROM revoked_subjects WHERE clerk_user_id = %s", ("user_dal_4",)
    ).fetchone()["expires_at"]
    assert backdated < first

    revoke_subject(db_conn, "user_dal_4")
    second = db_conn.execute(
        "SELECT expires_at FROM revoked_subjects WHERE clerk_user_id = %s", ("user_dal_4",)
    ).fetchone()["expires_at"]

    assert second > backdated
    count = db_conn.execute(
        "SELECT count(*) AS n FROM revoked_subjects WHERE clerk_user_id = %s", ("user_dal_4",)
    ).fetchone()["n"]
    assert count == 1


def test_prune_expired_revocations_removes_only_expired_rows(db_conn):
    from jobcannon.db._revoked_subjects import prune_expired_revocations, revoke_subject

    revoke_subject(db_conn, "user_dal_expired")
    _backdate_expiry(db_conn, "user_dal_expired", minutes_ago=1)
    revoke_subject(db_conn, "user_dal_still_live")

    reaped = prune_expired_revocations(db_conn)

    assert reaped == ["user_dal_expired"]
    remaining = {
        row["clerk_user_id"]
        for row in db_conn.execute(
            "SELECT clerk_user_id FROM revoked_subjects WHERE clerk_user_id IN (%s, %s)",
            ("user_dal_expired", "user_dal_still_live"),
        ).fetchall()
    }
    assert remaining == {"user_dal_still_live"}


def test_prune_expired_revocations_is_a_no_op_when_nothing_is_expired(db_conn):
    from jobcannon.db._revoked_subjects import prune_expired_revocations, revoke_subject

    revoke_subject(db_conn, "user_dal_fresh_only")

    reaped = prune_expired_revocations(db_conn)

    assert reaped == []
    row = db_conn.execute(
        "SELECT 1 FROM revoked_subjects WHERE clerk_user_id = %s", ("user_dal_fresh_only",)
    ).fetchone()
    assert row is not None


# --- periodic task wiring (no Postgres needed: connection_factory and
# prune_expired_revocations are both seams) ---------------------------------


def test_reap_revoked_subjects_task_reports_a_count_not_ids(monkeypatch):
    from jobcannon.host import tasks

    monkeypatch.setattr(
        tasks, "prune_expired_revocations", lambda conn: ["user_a", "user_b"]
    )

    @contextlib.contextmanager
    def _fake_connection_factory():
        yield object()

    monkeypatch.setattr("jobcannon.db.connection_factory", _fake_connection_factory)

    result = tasks.reap_revoked_subjects(0)

    assert result == {"reaped": 2}


def test_reap_revoked_subjects_task_reports_zero_when_nothing_reaped(monkeypatch):
    from jobcannon.host import tasks

    monkeypatch.setattr(tasks, "prune_expired_revocations", lambda conn: [])

    @contextlib.contextmanager
    def _fake_connection_factory():
        yield object()

    monkeypatch.setattr("jobcannon.db.connection_factory", _fake_connection_factory)

    result = tasks.reap_revoked_subjects(0)

    assert result == {"reaped": 0}


def test_reap_revoked_subjects_is_registered_as_a_periodic_task():
    """Confirms the task is a peer on the SAME procrastinate App as
    reap_anon_users/reap_old_events, not a second scheduling mechanism --
    app.tasks is keyed by fully-qualified dotted name (module docstring)."""
    from jobcannon.host import tasks

    assert "jobcannon.host.tasks.reap_revoked_subjects" in tasks.app.tasks
