# PORTED from job_finder/sources/email_senders.py @ 2dc6c820aff0a3b213e4f6c4c535097f8dd0a816 (private job-cannon). Ledger L-0113.
# PORT-SEAM: private docstring named "Gmail API and IMAP sources" + "archival
# helpers" (design-aggregators-imap.md §1.10) — Gmail API ingestion is a
# separate HOLD group and the archive helpers are excised below; only the
# registry + override resolution carry.
"""Email sender registry — shared by every alert-parsing surface.

This module contains the shared email-sender logic that IMAP-style ingestion
paths use. It includes the sender registry (FROM address -> parser mapping)
and override resolution.

# PORT-SEAM: private module docstring also claimed "parse-failure archival
# helpers" and "Gmail API" sources — both dropped by this port (see the
# provenance-header comment above and the two module-level notes below).
"""

# PORT-SEAM: `import logging` / `import os` dropped — both were used only by
# the two archive helpers excised at the bottom of this module (§1.10).
from collections.abc import Callable

# PORT-SEAM: `from datetime import datetime` dropped — used only by
# `_archive_parse_failure`, excised (§1.10).
from typing import NamedTuple

# PORT-SEAM: `from jobcannon.engine.email_parsers import has_job_urls` dropped
# — used only by `_should_archive_failure`, excised (§1.10).
from jobcannon.engine.email_parsers.glassdoor_parser import parse_glassdoor_alert
from jobcannon.engine.email_parsers.greenhouse_parser import parse_greenhouse_alert
from jobcannon.engine.email_parsers.indeed_parser import parse_indeed_alert
from jobcannon.engine.email_parsers.indeed_parser import parse_indeed_match_alert
from jobcannon.engine.email_parsers.jobright_parser import parse_jobright_alert
from jobcannon.engine.email_parsers.linkedin_parser import parse_linkedin_alert
from jobcannon.engine.email_parsers.monster_parser import parse_monster_alert
from jobcannon.engine.email_parsers.trueup_parser import parse_trueup_alert
from jobcannon.engine.email_parsers.ziprecruiter_parser import parse_ziprecruiter_alert
# PORT-SEAM: `from jobcannon.engine.user_data_dirs import parse_failures_dir`
# dropped — the archive helpers that used it are excised (§1.10).

# PORT-SEAM: `logging`/`os`/`datetime`/`logger` and the `has_job_urls` /
# `parse_failures_dir` imports dropped with the two archive helpers below
# (design-aggregators-imap.md §1.10): filesystem parse-failure archival is
# replaced by the per-sender parse-log row (§1.7), and the autoheal
# recipe-fallback block it supported is HOLD (not ported). Nothing in this
# module logs, so `logger` is dropped rather than left dead.


# ---------------------------------------------------------------------------
# Email-sender registry — THE single source of truth for the alert senders we
# parse. One row per FROM address: its parser, its canonical health label, and
# (optionally) the Settings form key the address may be overridden under.
#
# The three lookup maps below (address→parser, address→label, override-key→
# address) are DERIVED from this table, so they can never drift out of sync the
# way three hand-maintained dicts could. Adding a new alert source is a single
# SenderSpec row — no separate edits to forget. test_autoheal_email_capture and
# test_gmail_sender_overrides pin the derived shapes; form keys mirror
# settings.py `_parse_form_to_config` and config.example.yaml's `senders:` block.
# ---------------------------------------------------------------------------


class SenderSpec(NamedTuple):
    """One alert sender: FROM address → parser + canonical label + override key.

    Attributes:
        address: The exact FROM address matched on (historical SENDER_PARSERS /
            SENDER_LABEL key).
        parser: The parser callable for this sender's email body.
        label: Canonical one-per-parser health label (both LinkedIn addresses
            collapse to "linkedin").
        override_key: Settings form key (sources.imap.senders.<key>) the user
            may override this FROM address under, or None if not overridable.
    """

    address: str
    parser: Callable
    label: str
    override_key: str | None = None


SENDERS: tuple[SenderSpec, ...] = (
    SenderSpec(
        "jobalerts-noreply@linkedin.com", parse_linkedin_alert, "linkedin", "linkedin_alerts"
    ),
    SenderSpec("jobs-noreply@linkedin.com", parse_linkedin_alert, "linkedin", "linkedin_jobs"),
    SenderSpec("noreply@glassdoor.com", parse_glassdoor_alert, "glassdoor", "glassdoor"),
    SenderSpec("alert@indeed.com", parse_indeed_alert, "indeed", "indeed"),
    SenderSpec("donotreply@match.indeed.com", parse_indeed_match_alert, "indeed_match"),
    SenderSpec(
        "no-reply@ziprecruiter.com", parse_ziprecruiter_alert, "ziprecruiter", "ziprecruiter"
    ),
    SenderSpec("no-reply@us.greenhouse-jobs.com", parse_greenhouse_alert, "greenhouse"),
    SenderSpec("hello@trueup.io", parse_trueup_alert, "trueup"),
    SenderSpec("monster@notifications.monster.com", parse_monster_alert, "monster"),
    SenderSpec("noreply@jobright.ai", parse_jobright_alert, "jobright", "jobright"),
)

# Derived lookup maps — insertion order preserved from SENDERS.
SENDER_PARSERS: dict[str, Callable] = {s.address: s.parser for s in SENDERS}
SENDER_LABEL: dict[str, str] = {s.address: s.label for s in SENDERS}
# Settings-overridable senders: form key → DEFAULT address it swaps out.
_OVERRIDABLE_SENDERS: dict[str, str] = {
    s.override_key: s.address for s in SENDERS if s.override_key is not None
}


def resolve_sender_parsers(config: dict | None = None) -> dict:
    """Return SENDER_PARSERS with any user-overridden FROM addresses swapped in.

    Wires ``sources.imap.senders.<key>`` (saved by the Settings page) into the
    address→parser map. For each overridable sender key, if the config supplies a
    non-empty address that differs from the default, the default key is *renamed*
    to the override (its parser function is preserved). Non-overridable senders
    (greenhouse, indeed-match, trueup, monster) are untouched.

    # PORT-SEAM: private docstring also said "This function calls
    # normalize_email_senders internally to heal legacy sources.gmail.senders
    # configs..." — that legacy-config heal is not ported (see the Args note
    # below).

    Safety invariant: ``resolve_sender_parsers(None)``, ``resolve_sender_parsers({})``,
    and any config with no senders overrides all return a dict equal to
    ``SENDER_PARSERS`` — the no-override path is identical to today's behaviour.

    Args:
        config: Per-user sender-override dict (host per-user config seam —
            design-aggregators-imap.md §1.5), or None. Reads
            ``sources.imap.senders``.
            # PORT-SEAM: the private `normalize_email_senders` legacy-config
            # heal (relocating a stale `sources.gmail.senders` shape) is not
            # ported — it belongs to `job_finder/config.py`, out of scope for
            # this pure-engine PR, and a fresh host schema has no legacy shape
            # to heal from. The host per-user config seam supplies
            # `sources.imap.senders` directly; see PR-3/PR-4.

    Returns:
        A new dict mapping sender address → parser function.
    """
    # PORT-SEAM: private body opened with `from job_finder.config import
    # normalize_email_senders` + a legacy-config heal call — not ported, see
    # the Args note above.
    parsers = dict(SENDER_PARSERS)
    senders = (config or {}).get("sources", {}).get("imap", {}).get("senders", {}) or {}
    for sender_key, default in _OVERRIDABLE_SENDERS.items():
        override = senders.get(sender_key)
        if (
            isinstance(override, str)
            and override.strip()
            and override != default
            and default in parsers
        ):
            parsers[override] = parsers.pop(default)
    return parsers


def resolve_sender_label(config: dict | None = None) -> dict:
    """Return SENDER_LABEL with overridden FROM addresses mapped to the canonical label.

    For each overridden sender, the new address is ADDED to the label map pointing
    at the same canonical label as the default (the default entry is kept too, so
    autoheal recipes that key on the canonical label keep resolving). This mirrors
    ``resolve_sender_parsers`` so the resolved address has both a parser and a label.

    # PORT-SEAM: private docstring also had the same "This function calls
    # normalize_email_senders internally..." legacy-config-heal paragraph as
    # resolve_sender_parsers — not ported, see resolve_sender_parsers' Args
    # note.

    Safety invariant: the no-override path (None / {} / no senders) returns a dict
    equal to ``SENDER_LABEL``.

    Args:
        config: Per-user sender-override dict, or None.
        # PORT-SEAM: private Args text read "Full config dict (or None). Reads
        # sources.imap.senders after normalization." — the normalization
        # (legacy-config heal) step is not ported, see resolve_sender_parsers.

    Returns:
        A new dict mapping sender address → canonical label.
    """
    # PORT-SEAM: private body opened with `from job_finder.config import
    # normalize_email_senders` + a legacy-config heal call — not ported, see
    # the Args note above.
    labels = dict(SENDER_LABEL)
    senders = (config or {}).get("sources", {}).get("imap", {}).get("senders", {}) or {}
    for sender_key, default in _OVERRIDABLE_SENDERS.items():
        override = senders.get(sender_key)
        if (
            isinstance(override, str)
            and override.strip()
            and override != default
            and default in labels
        ):
            labels[override] = labels[default]
    return labels


# PORT-SEAM: `_should_archive_failure` / `_archive_parse_failure` (filesystem
# parse-failure archival, keyed on the autoheal `parse_failures_dir` helper)
# are NOT ported — design-aggregators-imap.md §1.10. Archival is replaced by
# the per-sender parse-log row (error_count/last_error, §1.7, PR-3).
