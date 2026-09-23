"""PORTED from job_finder/sources/imap_source.py @ bc30befa311b5c78868ece3dddd60b44d018f444
(private job-cannon). Ledger L-0115.
# PORT-SEAM: this module holds the PER-MESSAGE parse-dispatch half of
# private's ImapSource.fetch_jobs loop -- the From-header/body/date
# extraction helpers and the sender->parser dispatch -- extracted out of
# imap_intake.py in issue #358 (FU-D) so every email-intake lane shares one
# entry point: run_imap_intake calls parse_alert_batch on its fetched UID
# batch today; a future forwarded-alert intake lane calls the same function
# on its own message batch. The IMAP-specific half -- readonly folder
# selection, UID search criteria, the uid_highwater/UIDVALIDITY watermark --
# stays in imap_intake.py; this module sees only (message_key, raw RFC-822
# bytes) pairs and knows nothing about connections or watermarks.
"""

from __future__ import annotations

import email
import email.policy
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, NamedTuple

from jobcannon.engine.email_parsers import extract_with_fallback

logger = logging.getLogger(__name__)


def _extract_sender(message: email.message.Message) -> str:
    from_header = message.get("From", "")
    if "<" in from_header and ">" in from_header:
        return from_header.split("<")[1].split(">")[0].strip()
    return from_header.strip()


def _extract_body(message: email.message.Message) -> str | None:
    body = None
    for part in message.walk():
        content_type = part.get_content_type()
        content_disposition = str(part.get("Content-Disposition", ""))
        if "attachment" in content_disposition:
            continue
        if content_type == "text/plain" and body is None:
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
            except Exception:
                continue
        elif content_type == "text/html" and body is None:
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
            except Exception:
                continue
    return body


def _extract_date(message: email.message.Message) -> datetime | None:
    date_header = message.get("Date")
    if not date_header:
        return None
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(date_header)
        return dt.astimezone(UTC).replace(tzinfo=None)
    except Exception:
        return None


class AlertBatchResult(NamedTuple):
    """Outcome of parse_alert_batch -- the pieces every intake lane needs.

    Attributes:
        jobs: Parsed jobs, in batch order.
        processed_ids: Keys of messages the lane considers handled -- every
            message with extractable content whose sender matched a parser
            (parse attempted, success or failure) PLUS structurally
            unparseable ones (missing sender or body), so the lane never
            re-fetches them. A fetched message whose sender matches NO
            parser is neither processed nor a failure -- it was examined
            and skipped.
        extraction_records: One ``{"label", "job_count"}`` dict per
            dispatched message -- the input shape
            host/ingestion/_parse_log.py's record_run consumes.
        parse_failures: One ``{"sender", "label", "message_id", "error"}``
            dict per parser exception -- same writer's input shape.
    """

    jobs: list[Any]
    processed_ids: list[str]
    extraction_records: list[dict]
    parse_failures: list[dict]


def parse_alert_batch(
    messages: Iterable[tuple[str, bytes]],
    *,
    sender_parsers: dict,
    sender_label: dict,
) -> AlertBatchResult:
    """Parse one batch of raw job-alert emails -- the shared per-message
    parse-dispatch entry point every email-intake lane calls (FU-D).

    `messages`: ``(message_key, raw RFC-822 bytes)`` pairs, processed in
    iteration order. The key is opaque to this module -- an IMAP UID string
    for the imap_intake lane, whatever identifier a future lane carries --
    used for log lines and as parse_failures' ``message_id``.

    `sender_parsers` / `sender_label`: the resolved sender maps -- each lane
    obtains them from jobcannon.engine.email_senders.resolve_sender_parsers /
    resolve_sender_label under its own per-user sender_config (None for the
    built-in SENDERS registry). Both are keyed on sender address;
    sender_label maps address -> canonical label.
    """
    jobs: list[Any] = []
    processed_ids: list[str] = []
    extraction_records: list[dict] = []
    parse_failures: list[dict] = []

    for key, raw_bytes in messages:
        message = email.message_from_bytes(raw_bytes, policy=email.policy.default)

        sender = _extract_sender(message)
        body = _extract_body(message)
        email_date = _extract_date(message)

        if not sender or not body:
            logger.warning(
                "parse_alert_batch: skipping message with missing sender or body: %s", key
            )
            processed_ids.append(key)
            continue

        sender_lower = sender.lower()
        parser_fn = None
        sender_key = None
        for candidate_key, parser in sender_parsers.items():
            if candidate_key in sender_lower:
                parser_fn = parser
                sender_key = candidate_key
                break

        if parser_fn is None:
            logger.info("parse_alert_batch: no parser found for sender: %s (skipping)", sender)
            continue

        label = sender_label.get(sender_key, sender_key)
        try:
            parsed = extract_with_fallback(parser_fn, body, email_date)
            jobs.extend(parsed)
            extraction_records.append({"label": label, "job_count": len(parsed)})
        except Exception as e:
            logger.error(
                "parse_alert_batch: parser error for sender %s (message %s): %s",
                sender,
                key,
                e,
                exc_info=True,
            )
            parse_failures.append(
                {
                    "sender": sender,
                    "label": label,
                    "message_id": key,
                    "error": str(e),
                }
            )
            extraction_records.append({"label": label, "job_count": 0})

        processed_ids.append(key)

    return AlertBatchResult(
        jobs=jobs,
        processed_ids=processed_ids,
        extraction_records=extraction_records,
        parse_failures=parse_failures,
    )
