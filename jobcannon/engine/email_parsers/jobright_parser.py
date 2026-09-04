# PORTED from job_finder/parsers/jobright_parser.py @ 7405a93c8003fdf8a74f8be383cac457c0056803 (private job-cannon). Ledger L-0460.
"""Parse JobRight (jobright.ai) job-match alert emails into Job objects.

JobRight is an AI job-search copilot that emails a periodic digest of ranked job
matches from ``noreply@jobright.ai`` (display name "Jobright Job Alert"). Each
match links to a job detail page ``jobright.ai/jobs/info/<id>`` where ``<id>`` is
a 24-char hex object id.

Structure (grounded on a real captured alert, ``tests/fixtures/emails/jobright.eml``)
--------------------------------------------------------------------------------
The email is a single-part ``text/html`` message. Each job card is a nested
table wrapped in ONE outer ``<a href=".../jobs/info/<id>">`` (its visible text is
a "blob": ``<Company> <Industry> · <Stage> <NN> % <Title> [<Salary>]``). Inside
that wrapper are additional anchors with the SAME href — a clean **title-only**
anchor and an ``APPLY NOW`` CTA — plus the card fields as styled leaf elements:

* company   — bold ``<p>`` (``font-weight:600``)
* industry · funding-stage — muted ``<p>`` (``font-weight:400``, gray)
* match score — ``<span>`` ``NN %``
* title     — inner leaf ``<a>`` (no ``%`` in its text)
* salary    — ``<p>`` ``$138K/yr - $198K/yr`` (absent on some cards)
* location  — ``<p>`` ``City, ST`` / ``City, US`` / ``Remote``
* chrome    — ``N+ referrals``, ``16 minutes ago``, ``Be an early applicant``

Extraction therefore groups anchors by canonical job id (the ``/jobs/info/<hex>``
segment), scopes each card to the smallest single-job subtree, and pulls the
title from the clean inner anchor and the remaining fields from the card's leaf
elements. A ``jobright.ai/jobs/recommend`` "View More Opportunities" footer link
is NOT a posting (no hex id) and is ignored.

Meta-email handling
-------------------
The shared ``is_meta_email`` BASE patterns reject preambles like "you have N new
jobs" — but for JobRight *that digest IS the job-bearing email*, so gating on it
at the top would silently drop real matches. Instead we parse job links first and
never reject a job-bearing email on a preamble false-positive. The zero-yield
WARNING (issue #259 convention) fires only for a non-trivial body that yielded no
jobs and is not a narrow account preamble (verify/confirm/reset/welcome) — an
honest "the format changed, capture a fresh sample" signal.

This parser is HTML-only. ImapSource ``_extract_body`` prefers
the ``text/plain`` alternative when a multipart email carries one; the captured
real alert has no text/plain part, but a future variant might. JobRight is
intentionally excluded from the generic positional URL-fallback
(``_positional_fallback._JOB_URL_RE``) because that line-based heuristic
mis-attributes JobRight's layout — ingesting nothing (plus the zero-yield
WARNING) beats ingesting garbage rows.

If the structure is unrecognized, an empty list is returned (graceful
degradation) rather than raising.
"""

import logging
import re
from datetime import datetime

from bs4 import BeautifulSoup

from jobcannon.engine.models import Job
from jobcannon.engine.email_parsers._common import _PLACEHOLDER_STRINGS, parse_salary_range

logger = logging.getLogger(__name__)


# Canonical JobRight posting id: the hex object-id after ``/jobs/info/`` (also
# tolerates a hypothetical ``/jobs/<hex>`` without the ``info/`` marker). The
# 16+ hex-char requirement is what distinguishes a real posting from listing/CTA
# links like ``/jobs/recommend`` or a bare ``/jobs``. Also captures the inner id
# out of an unencoded click-tracker (``click.jobright.ai/CL0/https://jobright.ai/
# jobs/info/<hex>/1/x``).
_JOB_ID_RE = re.compile(r"jobright\.ai/jobs/(?:info/)?([0-9a-f]{16,})", re.IGNORECASE)

# Anchor-href matcher for "this links to a JobRight posting" (used by
# ``find_all``). Mirrors ``_JOB_ID_RE`` but anchored at the scheme so it matches
# the href attribute value; requires the hex id so listing/CTA links do not
# match.
JOBRIGHT_JOB_URL_RE = re.compile(
    r"https?://\S*jobright\.ai/jobs/(?:info/)?[0-9a-f]{16,}",
    re.IGNORECASE,
)

# Preambles (first 200 chars) that mark a genuinely-non-job JobRight email —
# account lifecycle / marketing, never a match digest. Used ONLY to suppress
# the zero-yield WARNING, never to reject before parsing. Deliberately excludes
# any "N new jobs" / "matches" phrasing (that IS the digest we want to parse).
_JOBRIGHT_ACCOUNT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"verify your (?:email|account)", re.IGNORECASE),
    re.compile(r"confirm your (?:email|subscription|account)", re.IGNORECASE),
    re.compile(r"reset your password", re.IGNORECASE),
    re.compile(r"welcome to jobright", re.IGNORECASE),
)

# Link / chrome texts that are navigation or card furniture, never a job title
# or company. Kept lowercase for case-insensitive membership tests.
_GENERIC_LINK_TEXTS: frozenset[str] = frozenset(
    {
        "apply",
        "apply now",
        "view",
        "view job",
        "view jobs",
        "view all jobs",
        "view more",
        "view more opportunities",
        "browse jobs",
        "see more",
        "learn more",
        "click here",
        "get started",
        "see all matches",
        "view matches",
        "unsubscribe",
    }
)

# Location cues, split into two patterns because case sensitivity differs:
#  - keywords (remote/hybrid/onsite) are case-insensitive;
#  - "City, ST" / "City, US" / ZIP MUST stay case-sensitive, else IGNORECASE
#    degrades the trailing [A-Z]{2} into "any two letters" and misclassifies a
#    company ending in ", Co" / ", In" as a location (the Indeed sibling parser
#    keeps its location regex case-sensitive for the same reason). The trailing
#    token is a US state OR a 2-letter country code (real cards show both, e.g.
#    "Mountain View, CA" and "Mountain View, US").
_LOCATION_KEYWORD_RE = re.compile(r"\b(remote|hybrid|onsite|on-site)\b", re.IGNORECASE)
_LOCATION_CITYSTATE_RE = re.compile(
    r"[A-Z][a-zA-Z]+(?:[\s.\-]+[A-Za-z]+)*,\s*[A-Z]{2}\b|\b[A-Z]{2}\s*\d{5}\b"
)

# JobRight card chrome that must never be mistaken for a company. Content-based
# (not class-based) so it does not overfit; "%" / "match score" avoids
# false-rejecting a real employer literally named "Match Group". Covers the AI
# match score, funding-stage tag, posted-age, referral count, and the
# early-applicant badge observed on every real card.
_BADGE_RE = re.compile(
    r"\d+\s*%"
    r"|\bmatch\s+score\b"
    r"|\b(?:strong|good|fair|weak)\s+match\b"
    r"|\b\d+\+?\s+(?:day|days|hour|hours|minute|minutes|week|weeks|month|months)\s+ago\b"
    r"|\bjust\s+posted\b"
    r"|\b\d+\+?\s+referrals?\b"
    r"|\bbe an early applicant\b"
    r"|\b(?:public\s+company|growth\s+stage|late\s+stage|early\s+stage|seed)\b"
    r"|\b(?:full|part)[\s-]?time\b"
    r"|\binternship\b|\btemporary\b",
    re.IGNORECASE,
)

_MIN_WARN_BODY_LEN = 500

# JobRight renders pay as "$138K/yr - $198K/yr" — the period is glued to each
# amount, which the shared salary parser's range regex can't tokenize. Strip the
# per-amount suffixes and re-append a SINGLE trailing cue instead. The cue must
# be a form ``salary_normalizer._PERIOD_CUES`` actually recognizes (``/hr``,
# ``/yr``, ``/mo``, ``/wk`` — abbreviated only; it does NOT match spelled-out
# "/ hour" etc.), otherwise the period resolves to 'unknown' and hourly/monthly/
# weekly pay is mis-annualized (or dropped as sub-floor) instead of correctly
# annualized.
_SALARY_PERIOD_ABBREV = {
    "yr": "yr",
    "year": "yr",
    "hr": "hr",
    "hour": "hr",
    "mo": "mo",
    "month": "mo",
    "wk": "wk",
    "week": "wk",
}
_SALARY_PERIOD_RE = re.compile(r"/\s*(yr|year|hr|hour|mo|month|wk|week)\b", re.IGNORECASE)


def _href(tag) -> str:
    """Return a tag's ``href`` as a plain string ("" if missing/multi-valued)."""
    value = tag.get("href", "")
    return value if isinstance(value, str) else ""


def parse_jobright_alert(body: str | None, email_date: datetime | None = None) -> list[Job]:
    """Parse a JobRight job-match alert email (HTML) into Job objects.

    Best-effort: if the structure is unrecognized, logs a warning (for
    non-trivial bodies that are not account emails) and returns an empty list
    rather than raising.

    Args:
        body: HTML email body from the Gmail/IMAP source.
        email_date: When the email was sent (stored as ``posted_date``).

    Returns:
        List of parsed Job objects (may be empty).
    """
    if not body or not body.strip():
        return []

    try:
        soup = BeautifulSoup(body, "html.parser")

        # Group every posting anchor by canonical job id, preserving first-seen
        # (document) order. All of a card's anchors (blob wrapper, title, CTA)
        # share one href/id, so this collapses them into a single card.
        order: list[str] = []
        by_id: dict[str, list] = {}
        for link in soup.find_all("a", href=True):
            match = _JOB_ID_RE.search(_href(link))
            if not match:
                continue
            jid = match.group(1).lower()
            if jid not in by_id:
                by_id[jid] = []
                order.append(jid)
            by_id[jid].append(link)

        jobs: list[Job] = []
        for jid in order:
            job = _build_job(jid, by_id[jid], email_date)
            if job:
                jobs.append(job)

        if not jobs and len(body.strip()) > _MIN_WARN_BODY_LEN and not _is_account_email(body):
            logger.warning(
                "JobRight parser: no jobs found -- email format may have changed. "
                "Capture a sanitized email at tests/fixtures/emails/jobright.eml and "
                "update jobright_parser.py."
            )

        return jobs

    except Exception as e:
        logger.warning("JobRight parser: unexpected error during parsing: %s", e)
        return []


def _is_account_email(body: str) -> bool:
    """Return True if the preamble marks a non-job account/marketing email.

    Only the first 200 chars are inspected (mirrors ``is_meta_email``) so a
    footer "unsubscribe" in a real digest never trips it.
    """
    preamble = body[:200]
    return any(pat.search(preamble) for pat in _JOBRIGHT_ACCOUNT_PATTERNS)


def _build_job(jid: str, anchors: list, email_date: datetime | None) -> Job | None:
    """Build one Job from all anchors that share a canonical job id."""
    title = _extract_title(anchors)
    if not title:
        return None

    container = _card_container(anchors, jid)
    company = _extract_company(container, title)
    location = _extract_location(container)
    salary_min, salary_max = _extract_salary(container)

    return Job(
        title=title,
        company=company or "Unknown",
        location=location or "Unknown",
        source="jobright",
        # Reconstruct the canonical detail URL from the id: the raw href carries
        # per-email utm/imp_id tracking noise that would defeat dedup, while the
        # id is stable across every alert that surfaces this posting.
        source_url=f"https://jobright.ai/jobs/info/{jid}",
        source_id=jid,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _card_container(anchors: list, jid: str):
    """Return the smallest ancestor subtree scoped to THIS job's card.

    Climb from the card's first anchor while every posting anchor still under the
    parent belongs to this job id — stop before a parent that also contains a
    *different* job (which would bleed a sibling card's company/location/salary
    into this one). Because the whole card is wrapped in one outer job anchor,
    this typically climbs to the enclosing table cell. Falls back to the anchor
    itself if it has no parent.
    """
    base = anchors[0]
    container = base
    el = base
    while el.parent is not None:
        parent = el.parent
        if _job_ids_under(parent) - {jid}:
            break
        el = parent
        container = parent
    return container


def _job_ids_under(el) -> set[str]:
    """Set of distinct JobRight posting ids among an element's descendant links."""
    ids: set[str] = set()
    for a in el.find_all("a", href=True):
        match = _JOB_ID_RE.search(_href(a))
        if match:
            ids.add(match.group(1).lower())
    return ids


def _extract_title(anchors: list) -> str | None:
    """Pull the job title from a card's anchors.

    The title lives in a clean **leaf** anchor (no nested ``<a>``) whose text is
    the role name — distinct from the outer "blob" wrapper anchor (which nests
    other anchors and carries the ``NN %`` match score) and the ``APPLY NOW``
    CTA. Only generic CTA text and placeholders are excluded as candidates: a
    "%" or location-shaped SUBSTRING is used merely as a tiebreak preference
    (defends against a hypothetical variant where the match score or location
    shares a leaf anchor with the title), never as a hard reject — a real title
    can legitimately read "100% Remote Data Engineer" or "Sales Manager, TX",
    and every such candidate must still be extracted, not silently dropped.
    """
    candidates: list[str] = []
    for a in anchors:
        # The blob wrapper nests the title/CTA anchors; skip it.
        if a.find("a"):
            continue
        text = a.get_text(" ", strip=True)
        if not text:
            continue
        low = text.lower()
        if low in _GENERIC_LINK_TEXTS or low in _PLACEHOLDER_STRINGS:
            continue
        candidates.append(text)

    if not candidates:
        return None

    clean = [c for c in candidates if "%" not in c and not _looks_like_location(c)]
    pool = clean or candidates
    title = min(pool, key=len)
    if len(title) > 160:
        return None
    return title


def _extract_company(container, title: str) -> str:
    """Find the employer name within a card container.

    Primary signal: JobRight renders the employer as a bold ``<p>``
    (``font-weight:600``). The line beneath it ("industry · funding stage") is
    weight 400, and the "Be an early applicant" badge is a ``<span>`` — so
    scoping to a bold ``<p>`` uniquely selects the company on every observed
    card. Falls back to the first plausible short leaf text, then "Unknown".
    """
    if container is None:
        return "Unknown"

    for p in container.find_all("p"):
        style = (p.get("style") or "").replace(" ", "").lower()
        if "font-weight:600" in style or "font-weight:700" in style or "font-weight:bold" in style:
            text = p.get_text(" ", strip=True)
            if _plausible_company(text, title):
                return text

    for el in container.find_all(["p", "span", "td", "div"]):
        # Skip wrapper elements that contain the job link — their aggregated
        # text is the whole card, not a single field.
        if el.find("a"):
            continue
        text = el.get_text(" ", strip=True)
        if _plausible_company(text, title):
            return text

    return "Unknown"


def _plausible_company(text: str, title: str) -> bool:
    """True if *text* could be a company name (not title/badge/location/salary)."""
    if not text or len(text) < 2 or len(text) > 80:
        return False
    low = text.lower()
    if low == title.lower() or low in _PLACEHOLDER_STRINGS or low in _GENERIC_LINK_TEXTS:
        return False
    if _looks_like_location(text) or _BADGE_RE.search(text):
        return False
    if "$" in text or "http" in low or "www." in low:
        return False
    # The muted "industry · stage" line carries a middle-dot / bullet separator.
    return "·" not in text and "•" not in text


def _extract_location(container) -> str:
    """Find the location among a card's leaf elements (City, ST / US / Remote).

    ``find_all`` yields ancestor cells before their leaves, and an aggregate
    like "$138K/yr - $198K/yr Mountain View, CA 5+ referrals" also matches the
    City,ST pattern — so pick the SHORTEST matching text (the tight leaf ``<p>``)
    and skip any candidate carrying a "$" (salary bleed).
    """
    if container is None:
        return "Unknown"

    best: str | None = None
    for el in container.find_all(["p", "span", "td", "div"]):
        if el.find("a"):
            continue
        text = el.get_text(" ", strip=True)
        if not text or len(text) > 60 or "$" in text:
            continue
        if _looks_like_location(text) and (best is None or len(text) < len(best)):
            best = text

    return best or "Unknown"


def _extract_salary(container) -> tuple[int | None, int | None]:
    """Parse the salary range from the card's ``$…`` leaf element, if present.

    Prefers the SHORTEST ``$``-bearing text (the tight salary ``<p>``, not an
    aggregate cell) and normalizes JobRight's glued ``/yr`` before delegating.
    """
    if container is None:
        return None, None

    best: str | None = None
    for el in container.find_all(["p", "span", "td", "div"]):
        if el.find("a"):
            continue
        text = el.get_text(" ", strip=True)
        if "$" in text and (best is None or len(text) < len(best)):
            best = text

    if best is None:
        return None, None
    return parse_salary_range(_normalize_salary_text(best))


def _normalize_salary_text(text: str) -> str:
    """Rewrite ``$138K/yr - $198K/yr`` into ``$138K - $198K / yr``.

    Strips the per-amount period suffix the shared parser's range regex can't
    tokenize and re-appends the unit ONCE at the end, in the ABBREVIATED form
    (``/yr``, ``/hr``, ``/mo``, ``/wk``) that ``salary_normalizer._PERIOD_CUES``
    actually matches. A spelled-out cue like "/ hour" is invisible to that
    regex and silently resolves to period='unknown', which mis-annualizes
    hourly/monthly/weekly pay (or drops it as implausible) instead of
    annualizing it correctly.
    """
    match = _SALARY_PERIOD_RE.search(text)
    stripped = re.sub(r"\s{2,}", " ", _SALARY_PERIOD_RE.sub(" ", text)).strip()
    if match:
        abbrev = _SALARY_PERIOD_ABBREV.get(match.group(1).lower())
        if abbrev:
            return f"{stripped} / {abbrev}"
    return stripped


def _looks_like_location(text: str) -> bool:
    """Return True if *text* matches a common location pattern."""
    return bool(_LOCATION_KEYWORD_RE.search(text) or _LOCATION_CITYSTATE_RE.search(text))
