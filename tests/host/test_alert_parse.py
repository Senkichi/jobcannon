"""jobcannon.host.ingestion._alert_parse.parse_alert_batch (issue #358, FU-D).

DB-free: the shared per-message parse-dispatch loop every email-intake
lane calls -- the IMAP lane (imap_intake.run_imap_intake) today, a future
forwarded-alert lane later. Covers the dispatch contract directly, without
the IMAP machinery tests/host/test_imap_intake.py wraps it in: sender/body
extraction, sender-substring parser dispatch, the processed_ids semantics
(structurally unparseable counts as processed; unmatched sender does not),
and the extraction_records / parse_failures shapes
_parse_log.record_run consumes. `extract_with_fallback` is patched to call
the injected parser fakes directly, so no real sender parser runs.
"""

from __future__ import annotations

from email.message import EmailMessage

from jobcannon.host.ingestion import _alert_parse
from jobcannon.host.ingestion._alert_parse import parse_alert_batch

_PARSERS = {
    "jobalerts-noreply@linkedin.com": lambda body: [{"title": "L"}],
    "no-reply@ziprecruiter.com": lambda body: [{"title": "Z1"}, {"title": "Z2"}],
}
_LABELS = {
    "jobalerts-noreply@linkedin.com": "linkedin",
    "no-reply@ziprecruiter.com": "ziprecruiter",
}


def _raw_email(*, from_addr: str, body: str | None = "job alert body") -> bytes:
    msg = EmailMessage()
    if from_addr:
        msg["From"] = f"Sender <{from_addr}>"
    msg["Date"] = "Fri, 17 Jul 2026 12:00:00 +0000"
    if body is not None:
        msg.set_content(body)
    return msg.as_bytes()


def _call_through(monkeypatch):
    """extract_with_fallback(parser_fn, body, date) -> parser_fn(body): the
    batch's own dispatch is under test, not the fallback wrapper."""
    monkeypatch.setattr(
        _alert_parse, "extract_with_fallback", lambda parser_fn, body, date: parser_fn(body)
    )


def test_empty_batch_returns_empty_result():
    result = parse_alert_batch([], sender_parsers=_PARSERS, sender_label=_LABELS)

    assert result.jobs == []
    assert result.processed_ids == []
    assert result.extraction_records == []
    assert result.parse_failures == []


def test_matched_sender_parses_and_is_processed(monkeypatch):
    _call_through(monkeypatch)
    batch = [("7", _raw_email(from_addr="jobalerts-noreply@linkedin.com"))]

    result = parse_alert_batch(batch, sender_parsers=_PARSERS, sender_label=_LABELS)

    assert result.jobs == [{"title": "L"}]
    assert result.processed_ids == ["7"]
    assert result.extraction_records == [{"label": "linkedin", "job_count": 1}]
    assert result.parse_failures == []


def test_sender_matching_is_substring_and_case_insensitive(monkeypatch):
    _call_through(monkeypatch)
    # Uppercase From address: the parser map is keyed on the lowercase bare
    # address, and dispatch is a case-insensitive substring match.
    parsers = dict(_PARSERS)
    parsers["jobs-noreply@linkedin.com"] = lambda body: [{"title": "L2"}]
    labels = dict(_LABELS)
    labels["jobs-noreply@linkedin.com"] = "linkedin"
    batch = [("9", _raw_email(from_addr="JOBS-NOREPLY@LINKEDIN.COM"))]

    result = parse_alert_batch(batch, sender_parsers=parsers, sender_label=labels)

    assert result.jobs == [{"title": "L2"}]
    assert result.extraction_records == [{"label": "linkedin", "job_count": 1}]


def test_missing_sender_or_body_is_processed_but_not_parsed(monkeypatch):
    _call_through(monkeypatch)
    batch = [
        ("1", _raw_email(from_addr="jobalerts-noreply@linkedin.com", body=None)),
        ("2", _raw_email(from_addr="")),  # no From header at all
    ]

    result = parse_alert_batch(batch, sender_parsers=_PARSERS, sender_label=_LABELS)

    assert result.jobs == []
    assert result.processed_ids == ["1", "2"]
    assert result.extraction_records == []
    assert result.parse_failures == []


def test_unmatched_sender_is_neither_processed_nor_failed(monkeypatch):
    _call_through(monkeypatch)
    batch = [("4", _raw_email(from_addr="stranger@unknown-domain.example"))]

    result = parse_alert_batch(batch, sender_parsers=_PARSERS, sender_label=_LABELS)

    assert result.jobs == []
    assert result.processed_ids == []  # examined and skipped, not handled
    assert result.extraction_records == []
    assert result.parse_failures == []


def test_parser_exception_records_failure_and_zero_count_record(monkeypatch):
    def boom(body):
        raise ValueError("synthetic parse failure")

    monkeypatch.setattr(
        _alert_parse, "extract_with_fallback", lambda parser_fn, body, date: parser_fn(body)
    )
    parsers = dict(_PARSERS)
    parsers["no-reply@ziprecruiter.com"] = boom
    batch = [("12", _raw_email(from_addr="no-reply@ziprecruiter.com", body="boom"))]

    result = parse_alert_batch(batch, sender_parsers=parsers, sender_label=_LABELS)

    assert result.jobs == []
    assert result.processed_ids == ["12"]
    assert result.extraction_records == [{"label": "ziprecruiter", "job_count": 0}]
    assert result.parse_failures == [
        {
            "sender": "no-reply@ziprecruiter.com",
            "label": "ziprecruiter",
            "message_id": "12",
            "error": "synthetic parse failure",
        }
    ]


def test_mixed_batch_aggregates_in_order(monkeypatch):
    _call_through(monkeypatch)
    batch = [
        ("1", _raw_email(from_addr="jobalerts-noreply@linkedin.com")),
        ("2", _raw_email(from_addr="stranger@unknown.example")),  # skipped entirely
        ("3", _raw_email(from_addr="no-reply@ziprecruiter.com")),
    ]

    result = parse_alert_batch(batch, sender_parsers=_PARSERS, sender_label=_LABELS)

    assert result.jobs == [{"title": "L"}, {"title": "Z1"}, {"title": "Z2"}]
    assert result.processed_ids == ["1", "3"]
    assert result.extraction_records == [
        {"label": "linkedin", "job_count": 1},
        {"label": "ziprecruiter", "job_count": 2},
    ]
    assert result.parse_failures == []


def test_label_falls_back_to_sender_key_when_unmapped(monkeypatch):
    _call_through(monkeypatch)
    # A parser map entry with no corresponding label entry -- the dispatch
    # still works and the record keys on the sender address itself.
    batch = [("5", _raw_email(from_addr="jobalerts-noreply@linkedin.com"))]

    result = parse_alert_batch(batch, sender_parsers=_PARSERS, sender_label={})

    assert result.extraction_records == [
        {"label": "jobalerts-noreply@linkedin.com", "job_count": 1}
    ]
