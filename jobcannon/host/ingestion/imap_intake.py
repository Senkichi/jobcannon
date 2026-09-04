"""PORTED from job_finder/sources/imap_source.py @ bc30befa311b5c78868ece3dddd60b44d018f444
(private job-cannon). Ledger L-0115.
# PORT-SEAM: header inserted above; the private module's safety contract,
# ``_build_from_search_criteria`` fold structure, and per-message extraction
# loop follow the source below with the seam edits noted inline. This is a
# host module (not engine) because it owns a real network connection and
# per-tenant DB reads/writes -- jobcannon/host/scan_tasks.py is this port's
# precedent for a host module with direct DB access and no IngestionServices
# DI seam (design-aggregators-imap.md §1.8 explicitly declines inventing one).

Safety contract (UNCHANGED from private -- this is the whole point of the
port):
- The folder is opened **readonly** so no IMAP command can implicitly set
  \\Seen. select_folder is called exactly ONCE, with readonly=True, and is
  never re-selected writable -- see the PORT-SEAM note below on \\Seen
  removal for why there is no second select_folder call at all.
- Messages are searched with a scoped FROM OR-chain so only job-alert
  senders are ever touched; personal mail is never fetched or examined.
- Bodies are fetched with ``BODY.PEEK[]`` which is explicitly non-mutating
  even on writable folders.

# PORT-SEAM: \\Seen flag write-back REMOVED (design note §1.6). Private
# marked processed messages \\Seen after fetching (a second, writable
# select_folder + add_flags call) to dedupe across runs. This port's
# mailbox access is read-only end to end -- it never sends a single mutating
# IMAP command to the user's real mailbox -- so dedup instead uses a
# per-credential UID watermark (mailbox_credentials.uid_highwater, m0025)
# tracked entirely server-side-free. _build_uid_search_criteria below
# replaces private's ``["UNSEEN", from_tree]`` with
# ``["UID", "<highwater+1>:*", from_tree]``.

# PORT-SEAM: UIDVALIDITY anchor (NOT in the design note; added while
# authoring m0025 -- see that migration's docstring). UIDs are only
# monotonic *within* one UIDVALIDITY epoch for a folder (RFC 3501
# §2.3.1.1). If the provider recreates the folder, a stored highwater
# compared against the new epoch's UIDs could silently skip mail. This
# module resets the effective highwater to 0 whenever the folder's live
# UIDVALIDITY (from select_folder's response) differs from the stored
# value, and always persists the live UIDVALIDITY alongside whatever
# highwater this run actually observed -- see run_imap_intake below.

# PORT-SEAM: overrides + archival excised (design note §1.10). Private
# imported ``job_finder.web.autoheal.override_loader`` /
# ``recipe_extractor`` (the Phase C/D recipe-fallback + shadow-guard block)
# and ``_archive_parse_failure`` / ``_should_archive_failure`` (filesystem
# parse-failure archival) -- both dropped: autoheal is HOLD, and archival is
# replaced by the per-sender parse-log row (host/ingestion/capture.py,
# L-0279). extraction_records / parse_failures are still accumulated in the
# same shape private used (label/job_count and label/error respectively) so
# capture.record_run's aggregation logic -- itself ported from private's
# ``_log_per_sender_email_parse`` -- needs no reshaping at the call site.

# PORT-SEAM: postings persistence NOT ported here. Private's
# ``fetch_jobs`` never called a DB upsert either (it returns
# ``(list[Job], list[str])`` and leaves persistence to its caller,
# ``ingestion_runner.py``). ``run_imap_intake`` below preserves that exact
# boundary -- it returns parsed jobs, never calls
# jobcannon.db._jobs.upsert_job. Turning returned Jobs into `postings` rows
# (company resolution, ParsedJob/UnresolvedParsedJob conversion) is
# ingestion_runner's job in private and is ledger row L-0188 here, a
# separate unit not in this PR's scope.

# PORT-SEAM: L-0111 (_error_envelope / VendorAccountError) is INERT for this
# lane. ``ImapSource`` never imports or raises VendorAccountError in
# private -- that reference lives entirely in ingestion_runner.py's shared
# ``_run_simple_source`` driver (its ``already_recorded_by_source`` /
# ``self_reports_source_health`` branch), which IMAP never opts into. There
# is no ScanServices-shaped handle to gate here because design note §1.8
# explicitly declines an IngestionServices DI object for this lane -- the
# gate this PR was asked to wire through ``ScanServices.vendor_account_error``
# has no call site inside this unit's file set; it belongs to L-0188's port
# of ``_run_simple_source``, not L-0115's port of the source itself.
"""

from __future__ import annotations

import email
import email.policy
import logging
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, NamedTuple

from jobcannon.db import _mailbox_credentials
from jobcannon.engine.email_parsers import extract_with_fallback
from jobcannon.engine.email_senders import resolve_sender_label, resolve_sender_parsers
from jobcannon.engine.model_types import (
    MailboxConnectionFactory,
    MailboxCredential,
    MailboxCredentialResolver,
)
from jobcannon.host.ingestion import capture

logger = logging.getLogger(__name__)


def _build_uid_search_criteria(senders: list[str], uid_start: int) -> list[Any]:
    """Build an IMAP search criteria list matching messages with UID >=
    `uid_start` from any of the given sender addresses.

    # PORT-SEAM: this is private's ``_build_from_search_criteria`` with
    # ``"UNSEEN"`` replaced by ``["UID", "<uid_start>:*"]`` -- the OR-fold
    # structure below (N senders need N-1 nested ORs) is otherwise
    # byte-identical logic to the private helper.

    IMAP OR is binary, so N senders require N-1 nested ORs:

    * 1 sender  -> ["UID", "<uid_start>:*", "FROM", addr]
    * 2 senders -> ["UID", "<uid_start>:*", "OR", ["FROM", a], ["FROM", b]]
    * N senders -> right-fold over the list

    The UID range is a SERVER-SIDE pre-filter only, for efficiency -- IMAP's
    "*" wildcard in a range can still match the mailbox's single highest-UID
    message even when `uid_start` exceeds every real UID present (RFC 3501
    range-clamping), so callers MUST also filter the returned UIDs in
    Python (`uid > uid_start - 1`) rather than trust this criteria alone.

    Raises:
        ValueError: If senders is empty.
    """
    if not senders:
        raise ValueError("senders must be non-empty")

    from_clauses: list[Any] = [["FROM", addr] for addr in senders]

    if len(from_clauses) == 1:
        from_tree: Any = from_clauses[0]
    else:
        from_tree = from_clauses[-1]
        for clause in reversed(from_clauses[:-1]):
            from_tree = ["OR", clause, from_tree]

    return ["UID", f"{uid_start}:*", from_tree]


@contextmanager
def _default_connection_factory(credential: MailboxCredential):
    """Default MailboxConnectionFactory: opens and logs into a real IMAP
    connection. `imapclient` is imported HERE, inside the function body, not
    at module scope -- jobcannon/engine/model_types.py's MailboxConnectionFactory
    docstring and this PR's Modularity note explain why: it keeps `imapclient`
    out of the web import graph (jobcannon/web's Sync-Now route, if one is
    ever wired, must never pull in an IMAP client library just by importing
    this module), and lets tests inject a fake factory with imapclient absent
    from the test process entirely.
    """
    from imapclient import IMAPClient

    with IMAPClient(credential.imap_host, port=credential.imap_port, ssl=True) as client:
        client.login(credential.address, credential.secret)
        yield client


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


class ImapIntakeResult(NamedTuple):
    """Return shape of run_imap_intake -- mirrors private's
    ``fetch_jobs() -> tuple[list[Job], list[str]]`` (jobs, processed uid
    strings), as a NamedTuple for readable field access at call sites
    rather than positional tuple unpacking."""

    jobs: list[Any]
    processed_uids: list[str]


def run_imap_intake(
    conn: Any,
    user_id: str,
    *,
    resolver: MailboxCredentialResolver,
    connection_factory: MailboxConnectionFactory | None = None,
    sender_config: dict | None = None,
    run_id: str | None = None,
) -> ImapIntakeResult:
    """Fetch and parse this tenant's job-alert emails via IMAP.

    `resolver`: a MailboxCredentialResolver bound to `user_id`
    (jobcannon.host.credentials.build_mailbox_resolver). Returning None
    (no consent, no active credential, or a decrypt failure) is a
    NO-OP -- this function returns an empty result without touching the
    network or any table.

    `sender_config`: per-user sender-FROM-address overrides (design note
    §1.5's ``sources.imap.senders`` seam), threaded straight through to
    resolve_sender_parsers/resolve_sender_label exactly as the private
    source's ``config`` parameter did -- no per-user override storage is
    invented by this port; None (the default) resolves to the built-in
    SENDERS registry unchanged, same safety invariant those two functions
    already guarantee.

    `run_id`: shared identifier attributed to every email_parse_log_sender
    row this run writes (capture.record_run). Defaults to a fresh uuid4
    hex when omitted.
    """
    credential = resolver()
    if credential is None:
        return ImapIntakeResult(jobs=[], processed_uids=[])

    progress = _mailbox_credentials.get_active_for_user(conn, user_id)
    if progress is None:
        logger.warning(
            "run_imap_intake: resolver succeeded but no active mailbox_credentials "
            "row for user_id=%s (race between consent/credential reads?) -- skipping",
            user_id,
        )
        return ImapIntakeResult(jobs=[], processed_uids=[])

    factory = connection_factory or _default_connection_factory
    run_id = run_id or uuid.uuid4().hex

    sender_parsers = resolve_sender_parsers(sender_config)
    sender_label = resolve_sender_label(sender_config)
    known_senders = list(sender_parsers.keys())

    all_jobs: list[Any] = []
    processed_uids: list[str] = []
    extraction_records: list[dict] = []
    parse_failures: list[dict] = []
    max_uid_seen = progress["uid_highwater"]
    live_uid_validity = progress["uid_validity"]

    try:
        with factory(credential) as client:
            # Select exactly ONCE, readonly -- never re-selected writable,
            # never followed by add_flags. See this module's Safety contract.
            select_response = client.select_folder(credential.folder, readonly=True)
            live_uid_validity = select_response[b"UIDVALIDITY"]

            effective_highwater = (
                progress["uid_highwater"] if progress["uid_validity"] == live_uid_validity else 0
            )
            max_uid_seen = effective_highwater

            search_criteria = _build_uid_search_criteria(known_senders, effective_highwater + 1)
            raw_uids = client.search(search_criteria)
            # Server-side "*" range clamping can return a UID <= our
            # watermark even when the range technically excludes it --
            # always re-filter in Python rather than trust the server.
            candidate_uids = sorted(u for u in raw_uids if u > effective_highwater)

            if candidate_uids:
                messages = client.fetch(candidate_uids, ["BODY.PEEK[]"])

                for uid in candidate_uids:
                    msg_data = messages.get(uid)
                    if msg_data is None:
                        continue
                    max_uid_seen = max(max_uid_seen, uid)

                    raw_bytes = msg_data[b"BODY[]"]
                    message = email.message_from_bytes(raw_bytes, policy=email.policy.default)

                    sender = _extract_sender(message)
                    body = _extract_body(message)
                    email_date = _extract_date(message)

                    if not sender or not body:
                        logger.warning(
                            "run_imap_intake: skipping message with missing sender or body: UID %s",
                            uid,
                        )
                        processed_uids.append(str(uid))
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
                        logger.info(
                            "run_imap_intake: no parser found for sender: %s (skipping)", sender
                        )
                        continue

                    label = sender_label.get(sender_key, sender_key)
                    try:
                        jobs = extract_with_fallback(parser_fn, body, email_date)
                        all_jobs.extend(jobs)
                        extraction_records.append({"label": label, "job_count": len(jobs)})
                    except Exception as e:
                        logger.error(
                            "run_imap_intake: parser error for sender %s (UID %s): %s",
                            sender,
                            uid,
                            e,
                            exc_info=True,
                        )
                        parse_failures.append(
                            {
                                "sender": sender,
                                "label": label,
                                "message_id": str(uid),
                                "error": str(e),
                            }
                        )
                        extraction_records.append({"label": label, "job_count": 0})

                    processed_uids.append(str(uid))
    except Exception as e:
        logger.error(
            "run_imap_intake: IMAP fetch error for user_id=%s: %s", user_id, e, exc_info=True
        )
        raise

    capture.record_run(
        conn,
        user_id,
        run_id=run_id,
        processed_at=datetime.now(UTC),
        extraction_records=extraction_records,
        parse_failures=parse_failures,
    )
    _mailbox_credentials.advance_uid_highwater(
        conn, user_id, uid_highwater=max_uid_seen, uid_validity=live_uid_validity
    )

    return ImapIntakeResult(jobs=all_jobs, processed_uids=processed_uids)
