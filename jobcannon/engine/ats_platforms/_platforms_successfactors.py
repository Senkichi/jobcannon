"""SuccessFactors ATS platform scanner (registry form).

SuccessFactors exposes a public XML feed at
``https://{host}/career?company={company_id}&career_ns=job_listing_summary&resultType=XML``
returning a ``<Job-Listing>`` document with ``<Job>`` children.

The slug format is ``"{host}|{company_id}"`` (e.g. ``"career2.successfactors.eu|SwissRe"``).
Both halves are extracted from a SuccessFactors-domain URL.

Facets are tenant-variable — ``filterN``/``mfieldN`` indices differ per company.
We resolve metadata by the ``<label>`` TEXT, never by element index:
- location ← facet labeled ``Posting Location``; fall back to ``Country``.
- department ← facet labeled ``Job Family / Functional Area``.
- employment_type ← facet labeled ``Type of Employment``.

The feed is single-shot (no pagination). An empty ``<Job-Listing></Job-Listing>``
returns a clean ``[]``.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime

import defusedxml.ElementTree as ET

from jobcannon.engine.ats_platforms._http_session import get_session
from jobcannon.engine.ats_platforms._registry import (
    BOARD_GONE_STATUSES,
    BoardGoneError,
    PlatformScanner,
)
from jobcannon.engine.ats_prober import _PROBE_TIMEOUT
from jobcannon.engine.location_parser import parse_locations

logger = logging.getLogger(__name__)


def _fetch_xml(slug: str) -> bytes | None:
    """Fetch the SuccessFactors XML feed for a slug.

    Slug format: "{host}|{company_id}" (e.g. "career2.successfactors.eu|SwissRe").
    """
    try:
        host, company_id = slug.split("|")
    except ValueError:
        logger.warning("_fetch_xml('%s'): invalid slug format (expected 'host|company_id')", slug)
        return None

    url = f"https://{host}/career?company={company_id}&career_ns=job_listing_summary&resultType=XML"
    try:
        resp = get_session().get(url, timeout=_PROBE_TIMEOUT)
    except Exception as exc:
        logger.debug("_fetch_xml('%s') failed: %s", slug, exc)
        return None

    if resp.status_code == 200 and resp.content:
        return resp.content

    if resp.status_code in BOARD_GONE_STATUSES:
        raise BoardGoneError(resp.status_code, slug)
    logger.warning("_fetch_xml('%s') returned HTTP %d", slug, resp.status_code)
    return None


def _resolve_facet(job_elem: ET.Element, target_label: str) -> str | None:
    """Find a facet value by its label text.

    SuccessFactors facets are tenant-variable: filterN/mfieldN indices differ.
    We search all filter*/mfield* children for a matching <label> and return
    the corresponding <value>.
    """
    for facet in job_elem:
        if facet.tag.startswith("filter") or facet.tag.startswith("mfield"):
            label = facet.findtext("label")
            if label == target_label:
                value = facet.findtext("value")
                if value:
                    return html.unescape(value).strip()
    return None


def _parse_posted_date(date_str: str) -> str | None:
    """Convert MM/DD/YYYY to ISO YYYY-MM-DD (naive UTC date string)."""
    try:
        dt = datetime.strptime(date_str, "%m/%d/%Y")
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    content = _fetch_xml(slug)
    if content is None:
        return []

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.warning("scan_successfactors('%s') XML parse error: %s", slug, exc)
        return []

    # Root must be <Job-Listing>
    if root.tag != "Job-Listing":
        logger.debug("scan_successfactors('%s'): root element is not <Job-Listing>", slug)
        return []

    postings: list[dict] = []
    for job in root.iter("Job"):
        title_elem = job.find("JobTitle")
        title = html.unescape(title_elem.text if title_elem is not None else "").strip()

        desc_elem = job.find("Job-Description")
        description_raw = (desc_elem.text if desc_elem is not None else "").strip()

        req_id_elem = job.find("ReqId")
        req_id = (req_id_elem.text if req_id_elem is not None else "").strip()

        posted_date_elem = job.find("Posted-Date")
        posted_date_raw = (posted_date_elem.text if posted_date_elem is not None else "").strip()
        posted_date = _parse_posted_date(posted_date_raw) if posted_date_raw else None

        # Resolve facets by label
        location = _resolve_facet(job, "Posting Location") or _resolve_facet(job, "Country") or ""
        department = _resolve_facet(job, "Job Family / Functional Area") or ""
        employment_type = _resolve_facet(job, "Type of Employment") or ""

        if not title or not req_id:
            # Skip malformed entries without required fields
            continue

        postings.append(
            {
                "title": title,
                "__description_raw": description_raw,
                "req_id": req_id,
                "posted_date": posted_date,
                "location": location,
                "department": department,
                "employment_type": employment_type,
            }
        )

    return postings


def _posting_to_job(posting: dict, slug: str) -> dict:
    """Convert a posting dict to the canonical job dict format."""
    title = posting.get("title") or ""
    description_raw = posting.get("__description_raw", "")
    req_id = posting.get("req_id") or ""
    posted_date = posting.get("posted_date")
    location = posting.get("location") or ""
    department = posting.get("department") or ""
    employment_type = posting.get("employment_type") or ""

    # Keep raw HTML in jd_full (downstream cleans it)
    jd_full = description_raw if description_raw else ""

    # Parse location into structured form
    locations_structured = parse_locations(location) if location else []

    # Build source_url from slug (landing page)
    try:
        host, company_id = slug.split("|")
        source_url = f"https://{host}/career?company={company_id}"
    except ValueError:
        source_url = ""

    return {
        "title": title,
        "description": jd_full,  # Raw HTML kept for jd_full
        "jd_full": jd_full,
        "source_id": req_id,
        "posted_date": posted_date,
        "location": location,
        "locations_structured": locations_structured,
        "department": department,
        "employment_type": employment_type,
        "is_remote": None,  # Tri-state from location text (filled by downstream)
        "source_url": source_url,
        "company_source": "SuccessFactors",
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlatformScanner(
    name="successfactors",
    company_source="SuccessFactors",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title") or "",
    posting_to_job=_posting_to_job,
)
