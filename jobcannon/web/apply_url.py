"""pick_apply_url / apply_destination_for_row — pick a usable outbound link
for a posting from stored source data, plus the hostname-only token that
gets logged as the `posting_apply_clicked` event's `apply_destination`.

`postings.direct_url` is permanently NULL (jobcannon/db/_jobs.py's INSERT
never populates it, and no UPDATE anywhere refills it), so the only
provenance this module reads is `source_urls` (a flat jsonb array of URL
strings — the primary listing link(s), first-wins) and `sightings` (a jsonb
array of `{"source", "source_url", "first_seen", "last_seen"}` dicts, kept
as a fallback for a row whose `source_urls` is empty but whose sightings
still carry a URL). A posting with neither yields no usable URL at all —
callers (jobcannon/web/pages.py, jobcannon/web/actions.py) must degrade the
apply control rather than render a dead link.

`apply_destination_for_row` returns a hostname-only token, never a full URL:
event payloads may never carry a path, query string, port, or userinfo (no
PII, no free text). It reads `urlsplit(url).hostname`, not `.netloc` —
`.netloc` still contains a port (`host:443`) and any userinfo
(`user:pw@host`) scraped straight from an uncurated `source_urls` /
`sightings` value, either of which would land verbatim in the event
payload. `jobcannon/web/anon_session.py::_referrer_host` already establishes
this exact pattern (hostname, wrapped `try/except ValueError`, bounded to
the payload string cap) for the same reason; this function mirrors it. A
malformed URL (`urlsplit` itself can raise `ValueError`, e.g. an unparseable
bracketed-IPv6 host) or a URL with no parseable host is treated the same as
"no usable URL" — the caller gets None either way and must degrade the same
way, never a 500. The returned hostname is bounded to
`jobcannon.db.events_schema._MAX_STR` so it can never itself be the reason
`validate_payload` rejects the event.

Row access is STRING-KEY only via `_get`, matching every other row-reading
module in this package (jobcannon/web/why.py, jobcannon/db/_feed.py).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from jobcannon.db.events_schema import _MAX_STR


def _get(mapping: Any, key: str, default: Any = None) -> Any:
    try:
        return mapping[key]
    except (KeyError, IndexError):
        return default


def pick_apply_url(row: Any) -> str | None:
    """First usable URL: the first non-empty entry of `source_urls`, else
    the first non-empty `source_url` among `sightings`. Returns None when
    neither yields anything — the honest "no usable URL" case, not an
    error."""
    for url in _get(row, "source_urls") or []:
        if url:
            return url
    for entry in _get(row, "sightings") or []:
        url = entry.get("source_url") if isinstance(entry, dict) else None
        if url:
            return url
    return None


def apply_destination_for_row(row: Any) -> str | None:
    """Hostname-only token for `pick_apply_url(row)`, or None when there is
    no usable URL, the URL has no parseable host, or `urlsplit` itself
    raises `ValueError` on a malformed URL."""
    url = pick_apply_url(row)
    if url is None:
        return None
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return None
    return hostname[:_MAX_STR] if hostname else None
