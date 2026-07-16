"""Structure-aware HTML → clean text extraction for job descriptions.

JD Extraction Layer 2, step 1 ("broad bloat cut"). This replaces the naive
``BeautifulSoup.get_text()`` stripping that produced ``jd_full`` — which left
HTML entities, MS-Word-export attribute sludge (``data-ccp-props`` /
``data-contrast``), and gross within-document duplication in the description.
See the 2026-06-03 JD-length investigation.

Pipeline (``html_to_clean_text``):
  1. trafilatura.extract(markdown, favor_precision) — strips nav/header/footer
     boilerplate, preserves headings + lists, drops all element attributes.
  2. BeautifulSoup fallback when trafilatura returns nothing usable (bare
     fragments with no article structure) — so we never regress to empty on a
     page that extracted before this change.
  3. _dedupe_blocks() — order-preserving removal of exact-duplicate blocks.
     trafilatura's own ``deduplicate`` is a segment-LRU and does NOT collapse
     gross block-level repetition (verified: a JD block repeated 20x survives
     20 heading copies). Job postings exported from broken CMSes repeat the
     entire description N times (e.g. Bozzuto 123k = one ~6k JD x20); this pass
     is what actually meets the "no duplication" acceptance criterion.

Pure function: no network, no DB, no config. Length capping stays the caller's
responsibility — callers already apply ``[:JD_STORAGE_MAX_CHARS]``.
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Tags whose text is never job-description content. Mirrors the set the legacy
# enrichment_tiers stripping used, so the fallback path is behavior-preserving.
_NOISE_TAGS = ("script", "style", "nav", "footer", "header", "noscript", "aside")

# trafilatura output shorter than this is treated as a failed extraction and we
# fall back to BeautifulSoup. trafilatura returns very short / None results on
# pages with no article-like structure (bare <div> shells, SPA fragments) — the
# fallback recovers the visible text those pages still carry.
_TRAFILATURA_MIN_CHARS = 25


def _trafilatura_extract(html: str) -> str | None:
    """Primary extraction via trafilatura → markdown. None on any failure.

    ``favor_recall=True`` is deliberate: completeness is the JD-extraction
    goal ("no dropped sections / default-keep"). trafilatura's
    ``favor_precision`` (and its default) discard lower-confidence in-content
    blocks — verified to drop a posting's Requirements section + bullets on
    sparse pages, the exact failure this effort exists to prevent. Recall mode
    keeps those sections while STILL structurally stripping page nav/header/
    footer boilerplate. Extra peripheral boilerplate it may retain is bounded
    downstream by _dedupe_blocks + the scorer's char cap and is far cheaper
    than a starved scoring axis.
    """
    try:
        import trafilatura
    except ImportError:  # pragma: no cover - trafilatura is a hard dependency
        logger.warning("trafilatura not installed; falling back to BeautifulSoup")
        return None
    try:
        return trafilatura.extract(
            html,
            output_format="markdown",
            include_comments=False,
            favor_recall=True,
        )
    except Exception as exc:  # trafilatura raises on some malformed trees
        logger.debug("trafilatura.extract failed: %s", exc)
        return None


def _beautifulsoup_extract(html: str) -> str | None:
    """Fallback extraction: strip noise tags, return newline-joined text.

    This is the pre-Layer-2 behavior, preserved verbatim so no page that
    extracted before regresses to empty when trafilatura declines to parse it.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.debug("BeautifulSoup parse failed: %s", exc)
        return None
    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    return text if text.strip() else None


def _dedupe_blocks(text: str) -> str:
    """Drop exact-duplicate blocks, keeping the first occurrence in order.

    A "block" is a run of text separated by blank lines (paragraph / heading /
    list as emitted by trafilatura markdown). The dedupe key normalizes inner
    whitespace and case so cosmetically-identical repeats collapse. Order of
    first occurrences is preserved, so a normal JD (all-unique blocks) is
    returned unchanged.
    """
    blocks = text.split("\n\n")
    seen: set[str] = set()
    kept: list[str] = []
    for block in blocks:
        key = " ".join(block.split()).lower()
        if not key:
            # Preserve intentional blank separators between kept blocks, but
            # never let them be the reason a block is "unique".
            continue
        if key in seen:
            continue
        seen.add(key)
        kept.append(block.strip())
    return "\n\n".join(kept)


def html_to_clean_text(html: str | None) -> str | None:
    """Extract clean job-description text from raw HTML.

    Returns markdown-formatted text with boilerplate stripped, headings/lists
    preserved, and exact-duplicate blocks collapsed — or ``None`` when the input
    is empty or no usable text could be recovered.
    """
    if not html or not html.strip():
        return None

    text = _trafilatura_extract(html)
    if not text or len(text.strip()) < _TRAFILATURA_MIN_CHARS:
        text = _beautifulsoup_extract(html)

    if not text or not text.strip():
        return None

    deduped = _dedupe_blocks(text)
    return deduped or None
