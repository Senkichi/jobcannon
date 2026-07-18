"""Unit tests for jobcannon.host.events.log_event and events_schema.validate_payload
(1B Wave 2 PR 8).

No Postgres needed for any test in this file. log_event has no g.db-shaped
seam to fake (this codebase has none — see jobcannon/host/events.py's module
docstring): it opens its own connection via jobcannon.db.pool.
connection_factory(), same as jobcannon/web/webhooks.py and jobcannon/host/
health_recorder.py. These tests patch that seam with a no-op context manager
and mock jobcannon.db._events.insert_event, so no real DB round-trip happens.

PostHog failure isolation is exercised against the REAL posthog_client.capture
wrapper (via set_posthog_client with a raising fake client), proving the
actual swallow-and-log code path in jobcannon/host/posthog_client.py rather
than merely that log_event calls some mock.

Postgres-backed integration coverage (interleave_team CHECK, anonymous
inserts, record_consent's one-transaction contract, set_posthog_client(None)
as a pure no-op) lives in tests/host/test_events_integration.py.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest

from jobcannon.db import events_schema
from jobcannon.host import posthog_client


@pytest.fixture(autouse=True)
def _clean_posthog_client():
    """posthog_client._client is a module-level global — reset it after every
    test so a client wired by one test can never leak into the next."""
    yield
    posthog_client.set_posthog_client(None)


@pytest.fixture()
def fake_connection_factory(monkeypatch):
    """Patch jobcannon.host.events.connection_factory to a no-op context
    manager yielding a MagicMock connection, so log_event's `with
    connection_factory() as conn:` block runs without a live Postgres pool.
    The actual DB write is separately mocked per-test via mock_insert_event —
    this fixture only needs to make `conn.raw` exist for that call's `conn`
    argument and for commit_unless_nested(conn.raw) to be harmless."""
    conn = MagicMock()

    @contextlib.contextmanager
    def _factory(**kwargs):
        yield conn

    import jobcannon.host.events as events_mod

    monkeypatch.setattr(events_mod, "connection_factory", _factory)
    return conn


@pytest.fixture()
def mock_insert_event(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr("jobcannon.db._events.insert_event", mock)
    return mock


@pytest.fixture()
def mock_capture(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr("jobcannon.host.posthog_client.capture", mock)
    return mock


# ---- events_schema.validate_payload -----------------------------------


@pytest.mark.parametrize(
    "event_type,payload",
    [
        ("posting_impression", {"bogus_key": "x"}),
        ("user_signed_up", {"channel": "x" * 201}),
        ("posting_saved", {"email": "someone@example.com"}),
        ("user_exit_surveyed", {"exit_reason": "quit-in-a-huff"}),
    ],
    ids=["unknown_key", "oversized_string", "illegal_key_email_shaped", "out_of_enum"],
)
def test_validate_payload_rejects(event_type, payload):
    with pytest.raises(ValueError):
        events_schema.validate_payload(event_type, payload)


def test_validate_payload_rejects_unknown_event_type():
    with pytest.raises(ValueError, match="unknown event_type"):
        events_schema.validate_payload("not_a_real_event_type", None)


@pytest.mark.parametrize(
    "event_type,payload",
    [
        ("posting_impression", {"surface": "feed"}),
        ("posting_saved", {}),
        ("posting_saved", None),
        ("user_exit_surveyed", {"exit_reason": "hired"}),
        ("user_exit_surveyed", {"exit_reason": "gave-up"}),
        ("user_exit_surveyed", {"exit_reason": "still-searching"}),
    ],
)
def test_validate_payload_accepts(event_type, payload):
    events_schema.validate_payload(event_type, payload)  # must not raise


# ---- log_event: consent gate -------------------------------------------


def test_no_consent_blocks_postgres_write_and_posthog_fanout(
    fake_connection_factory, mock_insert_event, mock_capture
):
    from jobcannon.host.events import log_event

    log_event("posting_saved", user_id="user_1", consent_granted=False)

    assert mock_insert_event.call_count == 0
    assert mock_capture.call_count == 0


def test_consent_granted_allows_postgres_write_and_posthog_fanout(
    fake_connection_factory, mock_insert_event, mock_capture
):
    from jobcannon.host.events import log_event

    log_event("posting_saved", user_id="user_1", consent_granted=True)

    assert mock_insert_event.call_count == 1
    assert mock_capture.call_count == 1


@pytest.mark.parametrize("granted", [True, False])
def test_consent_recorded_writes_postgres_regardless_of_grant(
    granted, fake_connection_factory, mock_insert_event, mock_capture
):
    """consent_recorded IS the audit trail of a consent decision (including a
    decline), so it must always reach Postgres — but only fans out to
    PostHog when the decision itself was a grant."""
    from jobcannon.host.events import log_event

    log_event(
        "consent_recorded",
        user_id="user_1",
        consent_granted=False,  # ambient consent state must be irrelevant here
        payload={
            "consent_type": "analytics",
            "granted": granted,
            "consent_version": "v1",
            "consented_at": "2026-07-17T00:00:00Z",
        },
    )

    assert mock_insert_event.call_count == 1
    assert mock_capture.call_count == (1 if granted else 0)


# ---- log_event: PostHog failure isolation ------------------------------


def test_posthog_failure_does_not_propagate_and_insert_already_happened(
    fake_connection_factory, mock_insert_event
):
    """Exercises the REAL posthog_client.capture wrapper (not a mock of
    capture itself) so this pins the actual swallow-and-log behavior."""
    from jobcannon.host.events import log_event

    order: list[str] = []
    mock_insert_event.side_effect = lambda *a, **kw: order.append("insert")

    class _RaisingClient:
        def capture(self, **kwargs):
            order.append("capture_attempted")
            raise RuntimeError("posthog is down")

    posthog_client.set_posthog_client(_RaisingClient())

    log_event("posting_saved", user_id="user_1", consent_granted=True)  # must not raise

    assert order == ["insert", "capture_attempted"]


# ---- record_consent: payload validation --------------------------------


def test_record_consent_rejects_oversized_value():
    """record_consent must run the payload through the SAME events_schema
    validator log_event uses, and it must do so BEFORE issuing any write —
    an oversized/illegal value aborts the whole write, not just the event
    insert."""
    from jobcannon.db import _events

    class _FakeConn:
        def execute(self, *a, **k):
            raise AssertionError("must not write when validation fails")

    with pytest.raises(ValueError):
        _events.record_consent(
            _FakeConn(),
            user_id="u1",
            consent_type="analytics",
            granted=True,
            consent_version="v" * 5000,  # exceeds the 200-char cap
            consented_at="2026-07-17T00:00:00Z",
        )
