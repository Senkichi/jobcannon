"""URL canonicalization at the parser boundary (Phase 49.01 / D-06 / F-05).

``canonicalize_url`` strips a fixed allowlist of tracking parameters and
normalizes query-param ordering + scheme/host case so two URLs that point at
the same posting collapse to one canonical string. The raw original is always
returned alongside the canonical form so callers can persist it for forensics
(``source_urls_raw``).

Design notes:
  - Canonicalization is intentionally DECOUPLED from dedup (NG-03): two logical
    jobs that genuinely live at different URLs (e.g. a Greenhouse posting and
    the company's careers page) must not be merged just because their tracking
    params were stripped. This helper only normalizes a single URL; using the
    canonical form as a dedup key is a separate, out-of-scope decision.
  - The helper NEVER raises. A malformed URL round-trips unchanged as
    ``(raw, raw)`` (logged at DEBUG) so a single bad URL can never abort an
    ingestion batch or a migration row-rewrite.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# Exact tracking-param keys (compared case-insensitively). The utm_* and mc_*
# families are handled by the prefix tuple below, so their individual members
# (utm_source, mc_cid, ...) are intentionally omitted here.
#
# NOTE: ``gh_jid`` is deliberately NOT in this set. It resembles a tracking param
# but is Greenhouse's STABLE POSTING IDENTIFIER: when a company fronts its
# Greenhouse board with its own careers domain (careers.airbnb.com/…?gh_jid=<id>,
# sofi.com/careers/job/<id>?gh_jid=<id>) the ``gh_jid`` param is the ONLY signal
# that the URL is a Greenhouse posting and what its id is. Stripping it here
# discarded that id before it ever reached the ATS layer that needs it
# (``ats_registry.extract_greenhouse_posting_id`` and its consumers: the
# reconciler, expiry_checker, and the postings backfill migration). The keys below
# carry no posting identity and are safe to drop.
_TRACKING_EXACT: frozenset[str] = frozenset(
    {
        "refid",  # refId, lowercased for comparison
        "trk",
        "lipi",
        "ref",
        "fbclid",
        "_hsenc",
        "_hsmi",
    }
)

# Prefix wildcard families: utm_* (utm_source, utm_medium, ...) and mc_*
# (mc_cid, mc_eid, ...). Compared against the lowercased key.
_TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "mc_")


def _is_tracking_param(key: str) -> bool:
    """True if a query-param key is a known tracking parameter."""
    k = key.lower()
    if k in _TRACKING_EXACT:
        return True
    return any(k.startswith(prefix) for prefix in _TRACKING_PREFIXES)


def canonicalize_url(raw: str) -> tuple[str, str]:
    """Return ``(canonical, raw)`` for a single URL.

    Canonicalization:
      - lowercase scheme + host (netloc),
      - drop every tracking parameter (``_TRACKING_EXACT`` + ``_TRACKING_PREFIXES``),
      - sort the remaining query params alphabetically (key, then value),
      - preserve path + fragment verbatim.

    On any parse error the input is returned unchanged as ``(raw, raw)`` and the
    failure is logged at DEBUG — this function never raises.
    """
    if not raw:
        return raw, raw
    try:
        parts = urlsplit(raw)
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking_param(k)
        ]
        kept.sort()
        canonical = urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path,
                urlencode(kept),
                parts.fragment,
            )
        )
        return canonical, raw
    except (ValueError, UnicodeError) as exc:  # pragma: no cover - defensive
        logger.debug("canonicalize_url: returning raw for unparseable %r (%s)", raw, exc)
        return raw, raw
