# PORTED from job_finder/parsers/trueup_parser.py @ 7405a93c8003fdf8a74f8be383cac457c0056803 (private job-cannon). Ledger L-0460.
"""Parse TrueUp weekly job digest emails into Job objects.

TrueUp sends HTML emails from hello@trueup.io with job cards containing
title, company, and location. Two layouts have existed:

- Legacy (pre-2026-05): tracking redirects ``url{N}.trueup.io/ls/click/...``
  served by their email vendor. Title and company were both wrapped in
  these redirects.
- Current (since ~2026-05-18): direct links — title goes to the actual
  ATS posting (Greenhouse, Workday, native careers page) and company
  goes to ``https://www.trueup.io/co/<slug>``. No more click-tracker
  intermediary. Cards live inside a ``<div style="...border:1px solid
  #ddd...">`` container.

The parser tries the current layout first (``/co/`` markers) and falls
back to the legacy layout if no current-layout cards are found, so
historical emails still parse if Gmail re-fetches them. Only jobs shown
in the email body (~7-8 per digest) are parsed — the "View all open
jobs" link requires HTTP requests which are out of scope.
"""

import logging
import re
from datetime import datetime

from bs4 import BeautifulSoup

from jobcannon.engine.models import Job

logger = logging.getLogger(__name__)

# Legacy tracking redirect URLs (varying subdomains).
TRUEUP_LEGACY_LINK_RE = re.compile(r"url\d+\.trueup\.io/ls/click", re.IGNORECASE)

# Current company-page links — one per card, in the form
# ``https://www.trueup.io/co/<slug>``. The slug is the company identifier
# on TrueUp; the link text is the human-readable company name.
TRUEUP_COMPANY_LINK_RE = re.compile(r"^https?://(?:www\.)?trueup\.io/co/", re.IGNORECASE)

# Border style fragment that distinguishes a job-card container div from
# every other div in the email. Stable across the current layout iterations.
_CARD_CONTAINER_STYLE_FRAGMENT = "border:1px solid"

# Navigation/footer links to exclude from card detection in the legacy path.
_EXCLUDE_TEXTS = frozenset(
    {
        "view all open jobs",
        "view all open jobs  →",
        "trueup",
        "update preferences",
        "unsubscribe",
        "my trueup",
    }
)

# Location heuristic: TrueUp uses ALL-CAPS city names with comma separators
# (e.g., "MOUNTAIN VIEW, CA, USA; SAN FRANCISCO, CA, USA" or "REMOTE, US")
# inside the card text. We pull the segment that looks most location-like.
_LOCATION_HINT_RE = re.compile(r"^[A-Z][A-Z\s,/;\-\.]{3,150}$")


def parse_trueup_alert(body: str, email_date: datetime | None = None) -> list[Job]:
    """Parse a TrueUp weekly digest email into Job objects.

    Args:
        body: Email body from Gmail API (HTML).
        email_date: When the email was sent.

    Returns:
        List of parsed Job objects (may be empty).
    """
    if not body or not body.strip():
        return []

    soup = BeautifulSoup(body, "html.parser")

    # Current layout first: each card has exactly one /co/<slug> link.
    jobs = _parse_current_layout(soup, email_date)
    if jobs:
        return jobs

    # Fallback for archived/legacy emails.
    jobs = _parse_legacy_layout(soup, email_date)

    if len(body) > 500 and not jobs:
        logger.warning(
            "TrueUp parser: non-empty body yielded 0 jobs — email format may have changed."
        )

    return jobs


# ---------------------------------------------------------------------------
# Current layout (since ~2026-05-18)
# ---------------------------------------------------------------------------


def _parse_current_layout(soup: BeautifulSoup, email_date: datetime | None) -> list[Job]:
    """Parse the post-2026-05 layout where cards have /co/<slug> markers."""
    company_links = soup.find_all("a", href=TRUEUP_COMPANY_LINK_RE)
    if not company_links:
        return []

    jobs: list[Job] = []
    seen: set[tuple[str, str]] = set()
    processed_containers: set[int] = set()

    for co_link in company_links:
        container = _find_bordered_card(co_link)
        if container is None:
            continue
        if id(container) in processed_containers:
            continue
        processed_containers.add(id(container))

        card = _extract_current_card(container, co_link)
        if not card:
            continue

        key = (card["title"].lower(), card["company"].lower())
        if key in seen:
            continue
        seen.add(key)

        jobs.append(
            Job(
                title=card["title"],
                company=card["company"],
                location=card["location"],
                source="trueup",
                source_url=card["url"],
                # No source_id: TrueUp email links expose only the SendGrid `upn`
                # tracking token (per-recipient, not per-job), so it cannot serve
                # as a stable platform ID. Left empty per I-11 contract. ATS
                # scanners supply the real per-job source_id downstream.
                posted_date=email_date,
            )
        )

    return jobs


def _find_bordered_card(element):
    """Walk up to the nearest ``<div>`` whose style includes a 1px solid border.

    TrueUp wraps each job card in this distinctive container. Bounded depth
    so a malformed email can't cause an O(tree) walk.
    """
    current = element.parent
    for _ in range(10):
        if current is None or current.name == "body":
            return None
        if current.name == "div":
            style = current.get("style") or ""
            if _CARD_CONTAINER_STYLE_FRAGMENT in style:
                return current
        current = current.parent
    return None


def _extract_current_card(container, co_link) -> dict | None:
    """Pull title/company/location/url out of one current-layout card.

    The card contains exactly two job-relevant <a> tags: the title link
    (direct to the actual ATS posting) and the company link (the /co/
    marker we already located). The title link is the first non-company
    link in document order within the card.
    """
    company = co_link.get_text(strip=True)
    if not company or len(company) > 80:
        return None

    title_link = None
    for a in container.find_all("a", href=True):
        href = a.get("href", "")
        if TRUEUP_COMPANY_LINK_RE.match(href):
            continue
        text = a.get_text(strip=True)
        if not text or text.lower() in _EXCLUDE_TEXTS:
            continue
        title_link = a
        break

    if title_link is None:
        return None

    title = title_link.get_text(strip=True)
    url = title_link.get("href", "")
    if not title or len(title) < 3 or len(title) > 200 or not url:
        return None

    location = _extract_location_from_text(container, company)

    return {
        "title": title,
        "company": company,
        "location": location,
        "url": url,
    }


def _extract_location_from_text(container, company: str) -> str:
    """Best-effort location pull from the card text.

    The card text is rendered as pipe-separated fields by the email
    template. The location is the ALL-CAPS segment with commas (e.g.,
    ``MOUNTAIN VIEW, CA, USA``, ``REMOTE, US``) that isn't the company
    or title.
    """
    text = container.get_text(separator="|", strip=True)
    company_lower = company.lower()
    for segment in text.split("|"):
        seg = segment.strip()
        if not seg or seg.lower() == company_lower:
            continue
        if _LOCATION_HINT_RE.match(seg):
            return seg
    return "Unknown"


# ---------------------------------------------------------------------------
# Legacy layout (pre-2026-05)
# ---------------------------------------------------------------------------


def _parse_legacy_layout(soup: BeautifulSoup, email_date: datetime | None) -> list[Job]:
    all_links = soup.find_all("a", href=TRUEUP_LEGACY_LINK_RE)
    if not all_links:
        return []

    cards = _legacy_find_job_cards(all_links)

    jobs: list[Job] = []
    seen: set[tuple[str, str]] = set()
    for card in cards:
        title = card.get("title")
        company = card.get("company")
        if not title or not company:
            continue
        key = (title.lower(), company.lower())
        if key in seen:
            continue
        seen.add(key)
        source_url = card.get("url", "")
        jobs.append(
            Job(
                title=title,
                company=company,
                location=card.get("location", "Unknown"),
                source="trueup",
                source_url=source_url,
                # No source_id (see above): TrueUp `upn` token is not per-job.
                posted_date=email_date,
            )
        )
    return jobs


def _legacy_find_job_cards(links) -> list[dict]:
    processed_containers = set()
    cards = []
    for link in links:
        if link.get_text(strip=True).lower() in _EXCLUDE_TEXTS:
            continue
        container = _legacy_find_card_container(link)
        if container is None:
            continue
        cid = id(container)
        if cid in processed_containers:
            continue
        processed_containers.add(cid)
        card = _legacy_extract_card_fields(container)
        if card:
            cards.append(card)
    return cards


def _legacy_find_card_container(element):
    current = element.parent
    for _ in range(10):
        if current is None or current.name == "body":
            break
        if current.name == "div" and current.find("table", recursive=False):
            return current
        current = current.parent
    return None


def _legacy_extract_card_fields(container) -> dict | None:
    card_links = container.find_all("a", href=TRUEUP_LEGACY_LINK_RE)
    job_links = [
        a
        for a in card_links
        if a.get_text(strip=True).lower() not in _EXCLUDE_TEXTS and len(a.get_text(strip=True)) > 1
    ]
    if len(job_links) < 2:
        return None

    title = job_links[0].get_text(strip=True)
    company = job_links[1].get_text(strip=True)
    url = job_links[0].get("href", "")

    if not title or len(title) > 150 or len(title) < 3:
        return None
    if not company or len(company) > 80:
        return None

    location = _legacy_extract_location(container, title, company)
    return {"title": title, "company": company, "location": location, "url": url}


def _legacy_extract_location(container, title: str, company: str) -> str:
    title_lower = title.lower()
    company_lower = company.lower()
    for div in container.find_all("div"):
        style = div.get("style", "")
        if "font-weight:500" in style or "font-weight: 500" in style:
            text = div.get_text(strip=True)
            if text and 2 < len(text) < 150:
                return text
    for div in container.find_all("div"):
        text = div.get_text(strip=True)
        if not text or len(text) < 3 or len(text) > 150:
            continue
        if text.lower() == title_lower or text.lower() == company_lower:
            continue
        if re.match(r"^[A-Z\s,/]+$", text) and "," in text:
            return text
    return "Unknown"
