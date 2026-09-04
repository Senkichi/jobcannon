"""jobcannon.host.ingestion.imap_intake.run_imap_intake (L-0115).

Fake MailboxConnectionFactory/IMAP client only -- no real mailbox, no
`imapclient` import needed in this test process at all (§6 PII checklist:
synthetic fixtures only). Covers the seams this port introduced over the
private ImapSource: the consent-gate no-op, the \\Seen-removal safety
contract (readonly select, add_flags never called), the UIDVALIDITY-epoch
reset, the UID-range Python-side re-filter, sender-parser dispatch +
parse-failure handling, and the capture.record_run / advance_uid_highwater
wiring.
"""

from __future__ import annotations

from contextlib import contextmanager
from email.message import EmailMessage

from jobcannon.db import _mailbox_credentials
from jobcannon.engine.model_types import MailboxCredential
from jobcannon.host.ingestion import imap_intake
from jobcannon.host.ingestion.imap_intake import (
    _build_uid_search_criteria,
    run_imap_intake,
)

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


# --- _build_uid_search_criteria (pure function, no DB) ---


def test_build_uid_search_criteria_single_sender():
    criteria = _build_uid_search_criteria(["a@x.com"], 5)
    assert criteria == ["UID", "5:*", ["FROM", "a@x.com"]]


def test_build_uid_search_criteria_folds_multiple_senders_with_nested_or():
    criteria = _build_uid_search_criteria(["a@x.com", "b@x.com", "c@x.com"], 1)
    assert criteria == [
        "UID",
        "1:*",
        ["OR", ["FROM", "a@x.com"], ["OR", ["FROM", "b@x.com"], ["FROM", "c@x.com"]]],
    ]


def test_build_uid_search_criteria_rejects_empty_senders():
    import pytest

    with pytest.raises(ValueError):
        _build_uid_search_criteria([], 1)


# --- fake IMAP client / connection factory ---


class _FakeImapClient:
    def __init__(self, uid_validity: int, messages: dict[int, bytes]):
        self.uid_validity = uid_validity
        self.messages = messages
        self.select_folder_calls: list[tuple[str, bool]] = []
        self.search_calls: list[object] = []
        self.fetch_calls: list[tuple[tuple, tuple]] = []
        self.add_flags_calls: list[tuple] = []

    def select_folder(self, folder: str, readonly: bool = False):
        self.select_folder_calls.append((folder, readonly))
        return {b"UIDVALIDITY": self.uid_validity}

    def search(self, criteria):
        self.search_calls.append(criteria)
        # Simulate a real IMAP server: return every UID we hold, regardless
        # of the criteria's numeric range -- exercises the Python-side
        # re-filter (`uid > effective_highwater`) that the port relies on
        # instead of trusting the server-side range alone.
        return sorted(self.messages.keys())

    def fetch(self, uids, parts):
        self.fetch_calls.append((tuple(uids), tuple(parts)))
        return {uid: {b"BODY[]": self.messages[uid]} for uid in uids if uid in self.messages}

    def add_flags(self, *args, **kwargs):
        self.add_flags_calls.append((args, kwargs))


def _factory_for(client: _FakeImapClient):
    @contextmanager
    def factory(credential):
        yield client

    return factory


def _make_raw_email(*, from_addr: str, body: str = "job alert body") -> bytes:
    msg = EmailMessage()
    msg["From"] = f"Sender <{from_addr}>"
    msg["Date"] = "Fri, 17 Jul 2026 12:00:00 +0000"
    msg.set_content(body)
    return msg.as_bytes()


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))


_CREDENTIAL = MailboxCredential(
    address="tenant@gmail.com",
    secret="app-password",
    imap_host="imap.example.org",
    imap_port=993,
    folder="INBOX",
)


def _seed_progress(conn, user_id, *, uid_highwater=0, uid_validity=0):
    _mailbox_credentials.set_mailbox_credential(
        conn,
        user_id,
        imap_host="imap.example.org",
        imap_port=993,
        auth_type="app_password",
        folder="INBOX",
        encrypted_secret=b"unused-in-this-test",
        username_hint="t***@gmail.com",
    )
    if uid_highwater or uid_validity:
        _mailbox_credentials.advance_uid_highwater(
            conn, user_id, uid_highwater=uid_highwater, uid_validity=uid_validity
        )


# --- consent-gate / no-active-row no-op ---


def test_resolver_returning_none_is_a_pure_no_op(db_conn):
    _seed_user(db_conn, "imap-u1")
    factory_calls = []

    def resolver():
        return None

    def spy_factory(credential):
        factory_calls.append(credential)
        raise AssertionError("connection_factory must never be called when resolver() is None")

    result = run_imap_intake(db_conn, "imap-u1", resolver=resolver, connection_factory=spy_factory)

    assert result.jobs == []
    assert result.processed_uids == []
    assert factory_calls == []


def test_no_active_mailbox_credentials_row_is_a_no_op(db_conn):
    """resolver() succeeded (e.g. a race with a since-deactivated row) but
    _mailbox_credentials.get_active_for_user finds nothing -- treated as
    empty, never an exception."""
    _seed_user(db_conn, "imap-u2")

    def resolver():
        return _CREDENTIAL

    result = run_imap_intake(db_conn, "imap-u2", resolver=lambda: _CREDENTIAL)

    assert result.jobs == []
    assert result.processed_uids == []


# --- \\Seen-removal safety contract ---


def test_never_calls_add_flags_and_selects_readonly_exactly_once(db_conn, monkeypatch):
    _seed_user(db_conn, "imap-u3")
    _seed_progress(db_conn, "imap-u3")
    client = _FakeImapClient(
        uid_validity=1,
        messages={5: _make_raw_email(from_addr="jobalerts-noreply@linkedin.com")},
    )
    monkeypatch.setattr(imap_intake, "extract_with_fallback", lambda fn, body, date: [])

    run_imap_intake(
        db_conn, "imap-u3", resolver=lambda: _CREDENTIAL, connection_factory=_factory_for(client)
    )

    assert client.select_folder_calls == [("INBOX", True)]
    assert client.add_flags_calls == []


# --- UIDVALIDITY epoch reset ---


def test_uid_validity_mismatch_resets_effective_highwater_to_zero(db_conn, monkeypatch):
    _seed_user(db_conn, "imap-u4")
    _seed_progress(db_conn, "imap-u4", uid_highwater=1000, uid_validity=5)
    client = _FakeImapClient(
        uid_validity=6,  # folder was recreated -- new epoch
        messages={3: _make_raw_email(from_addr="jobalerts-noreply@linkedin.com")},
    )
    monkeypatch.setattr(imap_intake, "extract_with_fallback", lambda fn, body, date: [])

    result = run_imap_intake(
        db_conn, "imap-u4", resolver=lambda: _CREDENTIAL, connection_factory=_factory_for(client)
    )

    # Search range starts from 1 (highwater reset to 0), not 1001 -- else
    # UID 3 (well below the stale 1000 watermark) would never be a
    # candidate and this run would silently skip it.
    assert client.search_calls[0][1] == "1:*"
    assert "3" in result.processed_uids

    row = _mailbox_credentials.get_active_for_user(db_conn, "imap-u4")
    assert row["uid_validity"] == 6
    assert row["uid_highwater"] == 3


def test_uid_validity_match_continues_from_stored_highwater(db_conn, monkeypatch):
    _seed_user(db_conn, "imap-u5")
    _seed_progress(db_conn, "imap-u5", uid_highwater=10, uid_validity=1)
    client = _FakeImapClient(uid_validity=1, messages={})
    monkeypatch.setattr(imap_intake, "extract_with_fallback", lambda fn, body, date: [])

    run_imap_intake(
        db_conn, "imap-u5", resolver=lambda: _CREDENTIAL, connection_factory=_factory_for(client)
    )

    assert client.search_calls[0][1] == "11:*"


# --- server-side range clamping: Python-side re-filter ---


def test_stale_uid_below_watermark_is_filtered_out_in_python(db_conn, monkeypatch):
    """The fake client's search() ignores the criteria's numeric range and
    returns every UID it holds (simulating IMAP's `*`-clamping quirk) --
    run_imap_intake must still exclude UID 5 (<= the watermark of 10)."""
    _seed_user(db_conn, "imap-u6")
    _seed_progress(db_conn, "imap-u6", uid_highwater=10, uid_validity=1)
    client = _FakeImapClient(
        uid_validity=1,
        messages={
            5: _make_raw_email(from_addr="jobalerts-noreply@linkedin.com", body="stale"),
            15: _make_raw_email(from_addr="jobalerts-noreply@linkedin.com", body="fresh"),
        },
    )
    monkeypatch.setattr(imap_intake, "extract_with_fallback", lambda fn, body, date: [])

    result = run_imap_intake(
        db_conn, "imap-u6", resolver=lambda: _CREDENTIAL, connection_factory=_factory_for(client)
    )

    assert result.processed_uids == ["15"]
    assert client.fetch_calls[0][0] == (15,)

    row = _mailbox_credentials.get_active_for_user(db_conn, "imap-u6")
    assert row["uid_highwater"] == 15


# --- sender-parser dispatch, parse-failure handling, capture wiring ---


def test_parser_dispatch_and_failure_are_recorded_via_capture(db_conn, monkeypatch):
    _seed_user(db_conn, "imap-u7")
    _seed_progress(db_conn, "imap-u7")
    client = _FakeImapClient(
        uid_validity=1,
        messages={
            1: _make_raw_email(from_addr="jobalerts-noreply@linkedin.com", body="ok"),
            2: _make_raw_email(from_addr="no-reply@ziprecruiter.com", body="boom"),
        },
    )

    def fake_extract(parser_fn, body, email_date):
        # `body` carries EmailMessage.set_content's trailing "\n" -- compare
        # with a substring check, not equality.
        if "boom" in body:
            raise ValueError("synthetic parse failure")
        return [{"title": "Fake Job"}]

    monkeypatch.setattr(imap_intake, "extract_with_fallback", fake_extract)

    result = run_imap_intake(
        db_conn,
        "imap-u7",
        resolver=lambda: _CREDENTIAL,
        connection_factory=_factory_for(client),
        run_id="run-parser-test",
    )

    assert result.jobs == [{"title": "Fake Job"}]
    assert set(result.processed_uids) == {"1", "2"}

    rows = {
        r["sender_label"]: r
        for r in db_conn.execute(
            "SELECT sender_label, emails_seen, jobs_parsed, error_count "
            "FROM email_parse_log_sender WHERE user_id = %s AND run_id = 'run-parser-test'",
            ("imap-u7",),
        ).fetchall()
    }
    assert rows["linkedin"]["emails_seen"] == 1
    assert rows["linkedin"]["jobs_parsed"] == 1
    assert rows["linkedin"]["error_count"] == 0
    assert rows["ziprecruiter"]["emails_seen"] == 1
    assert rows["ziprecruiter"]["jobs_parsed"] == 0
    assert rows["ziprecruiter"]["error_count"] == 1


def test_unmatched_sender_is_skipped_without_a_parse_failure(db_conn, monkeypatch):
    _seed_user(db_conn, "imap-u8")
    _seed_progress(db_conn, "imap-u8")
    client = _FakeImapClient(
        uid_validity=1,
        messages={1: _make_raw_email(from_addr="stranger@unknown-domain.example")},
    )
    monkeypatch.setattr(imap_intake, "extract_with_fallback", lambda fn, body, date: [])

    result = run_imap_intake(
        db_conn, "imap-u8", resolver=lambda: _CREDENTIAL, connection_factory=_factory_for(client)
    )

    assert result.jobs == []
    # No parser matched -- the UID still advances the watermark (fetched
    # once, never re-fetched) but is not counted as "processed" content.
    row = _mailbox_credentials.get_active_for_user(db_conn, "imap-u8")
    assert row["uid_highwater"] == 1


# --- sender_config threading (per-user overrides, design note §1.5) ---


def test_sender_config_override_is_threaded_through(db_conn, monkeypatch):
    _seed_user(db_conn, "imap-u9")
    _seed_progress(db_conn, "imap-u9")
    client = _FakeImapClient(
        uid_validity=1,
        messages={1: _make_raw_email(from_addr="custom-alerts@mycompany.example")},
    )
    monkeypatch.setattr(imap_intake, "extract_with_fallback", lambda fn, body, date: [{"j": 1}])
    sender_config = {
        "sources": {"imap": {"senders": {"linkedin_alerts": "custom-alerts@mycompany.example"}}}
    }

    result = run_imap_intake(
        db_conn,
        "imap-u9",
        resolver=lambda: _CREDENTIAL,
        connection_factory=_factory_for(client),
        sender_config=sender_config,
        run_id="run-override-test",
    )

    assert result.jobs == [{"j": 1}]
    row = db_conn.execute(
        "SELECT sender_label FROM email_parse_log_sender "
        "WHERE user_id = %s AND run_id = 'run-override-test' AND emails_seen > 0",
        ("imap-u9",),
    ).fetchone()
    assert row["sender_label"] == "linkedin"  # canonical label preserved under the override
