# PORTED from job_finder/sources/_pii_scrub.py @ 28c03ab79e04e417741d77f8f181909acedf671f (private job-cannon). Ledger L-0043.
"""Reusable PII scrubbing for captured parser inputs.

Strips recipient headers, bare emails, sensitive URL query-string secrets, and
caller-supplied identifiers before any capture persists — see
``host/ingestion/capture.py`` (design-aggregators-imap.md §6) for the sole
INSERT chokepoint this module protects.

# PORT-SEAM: DEFAULT_DENYLIST ships empty here. The private denylist seeded
# the single owner's own name/handle as a hardcoded convenience default for a
# single-user desktop app; a hosted multi-tenant product has no one default
# identity to protect and must never ship one operator's identifiers baked
# into shared code (design-aggregators-imap.md §6 PII checklist). Callers
# pass each tenant's own identifiers via `scrub_text(..., identifiers=...)`,
# resolved per-request from that user's profile (§6: "NEVER a process-global").
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# PORT-SEAM: the private seed comment named a specific test file that isn't
# ported; DEFAULT_DENYLIST itself ships empty (see module docstring above).
DEFAULT_DENYLIST: tuple[str, ...] = ()

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_TO_HEADER_RE = re.compile(r"^\s*(to|cc|bcc|delivered-to|x-original-to)\s*:.*$", re.IGNORECASE)
_REDACTED = "[redacted]"

# Query-parameter names whose values should be redacted when they appear in URLs
# inside log records. Keep in sync with tests/test_pii_scrub.py.
SENSITIVE_QUERY_PARAMS: tuple[str, ...] = (
    "api_key",
    "app_key",
    "app_id",
    "key",
    "token",
    "access_token",
)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def scrub_text(text: str, identifiers: tuple[str, ...] | list[str] | None = None) -> str:
    """Return *text* with recipient headers dropped and PII redacted.

    Idempotent and never raises on str input. ``identifiers`` extends (does not
    replace) DEFAULT_DENYLIST — pass the current tenant's own name/email.
    # PORT-SEAM: private text read "the local user's name/email from config"
    # (a single-operator desktop app); hosted, callers resolve each tenant's
    # own identifiers per-request rather than one process-global config.
    """
    if not text:
        return text or ""
    deny = tuple(DEFAULT_DENYLIST) + tuple(identifiers or ())

    kept = [ln for ln in text.splitlines() if not _TO_HEADER_RE.match(ln)]
    out = "\n".join(kept)

    out = _EMAIL_RE.sub(_REDACTED, out)
    for ident in deny:
        if not ident:
            continue
        out = re.sub(re.escape(ident), _REDACTED, out, flags=re.IGNORECASE)
    return out


def redact_url_secrets(
    text: str,
    sensitive_params: Iterable[str] | None = None,
    replacement: str = "[REDACTED]",
) -> str:
    """Return *text* with sensitive query-parameter values redacted in every URL.

    Replaces the value of any query parameter whose name is in *sensitive_params*
    with *replacement* while leaving the rest of the URL intact. The default
    parameter list covers the credentials carried by the upstream sources
    (SerpAPI, Adzuna, etc.). Safe for non-URL text — only URL-like substrings
    starting with ``http://`` or ``https://`` are touched.
    """
    if not text:
        return text or ""
    params = set(sensitive_params or SENSITIVE_QUERY_PARAMS)
    if not params:
        return text

    def _redact_match(match: re.Match[str]) -> str:
        url = match.group(0)
        # ``urlparse`` raises ``ValueError`` on malformed URL-like substrings
        # (e.g. ``https://x']`` — an unmatched ``]`` triggers "Invalid IPv6
        # URL").  The regex ``\S+`` is deliberately greedy so it can capture
        # trailing punctuation from surrounding text; rather than tighten the
        # regex (which risks missing real credential URLs), we simply skip
        # un-parseable matches and leave them verbatim.
        try:
            parsed = urlparse(url)
        except ValueError:
            return url
        if not parsed.query:
            return url
        query = parse_qsl(parsed.query, keep_blank_values=True)
        redacted = [(name, replacement if name in params else value) for name, value in query]
        new_query = urlencode(redacted, safe="[]")
        return urlunparse(parsed._replace(query=new_query))

    return _URL_RE.sub(_redact_match, text)


class CredentialRedactionFilter(logging.Filter):
    """Logging filter that redacts credential-bearing URLs from log records.

    Mutates ``LogRecord.msg`` and ``LogRecord.args`` so the redacted message
    reaches every downstream handler (file, console, checkpoint log excerpts).
    """

    def __init__(
        self,
        sensitive_params: Iterable[str] | None = None,
        name: str = "",
    ) -> None:
        super().__init__(name)
        self.sensitive_params = tuple(sensitive_params or SENSITIVE_QUERY_PARAMS)

    def filter(self, record: logging.LogRecord) -> bool:
        # PORT-SEAM: prefix rewritten "job_finder" -> "jobcannon" (the public
        # package name); the private literal would silently match no logger.
        if record.name and not record.name.startswith("jobcannon"):
            return True
        # A logging filter must NEVER raise — an exception here aborts the
        # ``Logger.handle`` call chain and can mask the original error being
        # logged (observed in CI: ``ValueError: Invalid IPv6 URL`` from
        # ``urlparse`` replaced the real schema-validation warning).  Any
        # failure in redaction leaves the record untouched.
        try:
            message = record.getMessage() if record.args else str(record.msg)
            redacted = redact_url_secrets(message, sensitive_params=self.sensitive_params)
            if redacted != message:
                record.msg = redacted
                record.args = None
        except Exception:
            pass
        return True


# PORT-SEAM: `_JobFinderLogger` renamed to `_ScrubbedLogger` and every
# `job_finder`/`job_finder.*` literal below renamed to `jobcannon` — the
# private package name doesn't exist here and would silently protect zero
# loggers if left unrewritten.
class _ScrubbedLogger(logging.Logger):
    """Logger subclass that auto-installs CredentialRedactionFilter for jobcannon.* loggers.

    ``logging.setLoggerClass`` must be invoked with this class before any
    ``jobcannon.*`` logger is instantiated, so every source logger gets the
    filter without touching individual call sites.  # PORT-SEAM: see above.
    """

    def __init__(self, name: str, level: int = logging.NOTSET) -> None:
        super().__init__(name, level)
        if name.startswith("jobcannon"):  # PORT-SEAM: job_finder -> jobcannon
            _ensure_credential_filter(self)


def _ensure_credential_filter(logger: logging.Logger) -> None:
    if not any(isinstance(f, CredentialRedactionFilter) for f in logger.filters):
        logger.addFilter(CredentialRedactionFilter())


# PORT-SEAM: default `prefix` renamed "job_finder" -> "jobcannon" (public
# package name); every `_JobFinderLogger`/`.setLoggerClass` call below
# renamed to `_ScrubbedLogger` to match.
def install_credential_redaction_filter(prefix: str = "jobcannon") -> None:
    """Install the credential redaction filter across the ``jobcannon`` logger tree.

    Sets a custom logger class so every future ``jobcannon.*`` logger is born
    # PORT-SEAM: job_finder -> jobcannon (see module note above).
    with the filter, and retrofits any logger that already exists in the
    ``logging.Manager`` logger dictionary. This is the single enforcement point
    so any source that logs a ``requests.HTTPError`` (which embeds the full
    request URL) is scrubbed without per-call-site ``str(e)`` wrapping.
    """
    logging.setLoggerClass(_ScrubbedLogger)  # PORT-SEAM: see module note above
    logging.Logger.manager.setLoggerClass(_ScrubbedLogger)
    # Retrofit any loggers created before this function was first called.
    manager = logging.Logger.manager
    for name, obj in list(manager.loggerDict.items()):
        if name.startswith(prefix) and isinstance(obj, logging.Logger):
            _ensure_credential_filter(obj)
    # Ensure the prefix root itself has the filter (creates it if absent).
    _ensure_credential_filter(logging.getLogger(prefix))
