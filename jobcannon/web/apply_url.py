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
event payloads may never carry a path or query string (no PII, no free
text), and tests/host/test_feed_events.py asserts the stored value contains
no `://` and no `?`. A URL with no parseable host (relative, malformed, or
otherwise unusable) is treated the same as "no usable URL" — the caller gets
None either way and must degrade the same way.

Row access is STRING-KEY only via `_get`, matching every other row-reading
module in this package (jobcannon/web/why.py, jobcannon/db/_feed.py).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


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
    no usable URL or the URL has no parseable host."""
    url = pick_apply_url(row)
    if url is None:
        return None
    netloc = urlsplit(url).netloc
    return netloc or None
