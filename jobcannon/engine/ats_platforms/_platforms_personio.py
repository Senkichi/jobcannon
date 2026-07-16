"""Personio platform scanner (registry form).

Public XML feed at ``https://{slug}.jobs.personio.{de,com}/xml``. We try
``.de`` first (canonical per Personio docs) and fall back to ``.com`` on
404 — some tenants migrated TLDs. The response is the workzag-jobs
document with ``<position>`` children: id, name, office, jobDescriptions,
employmentType, yearsOfExperience.

Postings are returned as flat dicts shaped like
``{"id", "name", "office", "description"}`` so the rest of the registry
machinery (title_of, posting_to_job) works on dicts without caring that
the source was XML.
"""

from __future__ import annotations

import logging

import defusedxml.ElementTree as ET

from jobcannon.engine.ats_platforms._http_session import get_session
from jobcannon.engine.ats_platforms._registry import PlatformScanner
from jobcannon.engine.ats_prober import _PROBE_TIMEOUT
from jobcannon.engine.description_formatter import strip_html_to_text

logger = logging.getLogger(__name__)

_PERSONIO_TLDS = ("de", "com")


def _fetch_xml(slug: str) -> bytes | None:
    """Fetch the Personio XML feed for a slug, trying .de then .com."""
    for tld in _PERSONIO_TLDS:
        url = f"https://{slug}.jobs.personio.{tld}/xml"
        try:
            resp = get_session().get(url, timeout=_PROBE_TIMEOUT)
        except Exception as exc:
            logger.debug("_personio_fetch_xml('%s', tld=%s) failed: %s", slug, tld, exc)
            continue
        if resp.status_code == 200 and resp.content:
            return resp.content
        if resp.status_code != 404:
            logger.debug(
                "_personio_fetch_xml('%s', tld=%s) returned HTTP %d",
                slug,
                tld,
                resp.status_code,
            )
    return None


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    content = _fetch_xml(slug)
    if content is None:
        return []

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.warning("scan_personio('%s') XML parse error: %s", slug, exc)
        return []

    positions: list[dict] = []
    for pos in root.iter("position"):
        title = (pos.findtext("name") or "").strip()
        location = (pos.findtext("office") or "").strip()
        position_id = (pos.findtext("id") or "").strip()

        # jobDescriptions is a wrapper holding one or more <jobDescription>
        # children, each with <name> + <value>. Flatten into plain text.
        descriptions: list[str] = []
        for desc in pos.iter("jobDescription"):
            value = desc.findtext("value") or ""
            if value:
                descriptions.append(value)

        positions.append(
            {
                "id": position_id,
                "name": title,
                "office": location,
                "__description_raw": "\n\n".join(descriptions),
            }
        )

    return positions


def _posting_to_job(posting: dict, slug: str) -> dict:
    joined = posting.get("__description_raw", "")
    description = strip_html_to_text(joined) if "<" in joined else joined

    position_id = posting.get("id") or ""
    # Canonical detail URL — uses the same .de host as the feed lookup
    # path. Tenants on .com still link out via .de in most cases, so this
    # is a best-effort fallback rather than an authoritative path.
    source_url = f"https://{slug}.jobs.personio.de/job/{position_id}" if position_id else ""

    return {
        "title": posting.get("name") or "",
        "company_source": "Personio",
        "location": posting.get("office") or "",
        "description": description,
        "source_url": source_url,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlatformScanner(
    name="personio",
    company_source="Personio",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("name") or "",
    posting_to_job=_posting_to_job,
)
