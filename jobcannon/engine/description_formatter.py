"""Jinja2 template filter for rendering job descriptions as structured HTML.

Handles three description formats:
1. Structured (markdown headers or plain-text section headers with paragraphs
   and bullet lists) -- renders with proper HTML structure.
2. Legacy pipe-separated -- renders as a bullet list.
3. Simple text -- renders as a paragraph.

Exports:
    format_description_filter: Jinja2 filter function for the 'format_description' filter.
"""

import html as _html
import re

from markupsafe import Markup, escape

# --- Compiled regex patterns ---

# Matches markdown headers (# Title, ## Section)
_md_header_re = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)

# Matches plain-text section headers (About, Responsibilities, etc.)
_plain_header_re = re.compile(
    r"^(?:About|Overview|Summary|Responsibilities|Requirements|Qualifications|"
    r"Benefits|What You|Minimum|Preferred|Nice to Have|The Role|Your Role|"
    r"Who You Are|What We|Key |Job |Position |Company |Team |Culture |"
    r"About the |How to |Why |Our |Skills|Experience|Education|Compensation|"
    r"Duties|Description|Location)",
    re.IGNORECASE,
)

# Matches bullet list items (- item or * item)
_bullet_re = re.compile(r"^\s*[-*]\s")

# Matches any HTML tag
_html_tag_re = re.compile(r"<[a-zA-Z/][^>]*>")


def strip_html_to_text(text: str) -> str:
    """Strip HTML tags from text, preserving structure via newlines and bullet markers.

    Converts block-level closing tags to newlines and <li> to bullet prefixes
    so the plain-text structured renderer can detect headers and bullets.
    Remaining (inline) tags are replaced with a space rather than removed, so
    two adjacent inline elements keep their word boundary
    (``<b>Foo</b><i>Bar</i>`` → ``"Foo Bar"``, not ``"FooBar"``) — the property
    the portal-search ``_strip_html`` previously got from BeautifulSoup's
    ``get_text(separator=" ")`` and now inherits by delegating here.

    The catch-all inline-tag regex below requires an HTML tag-open shape
    (``<`` immediately followed by a letter, ``/``, ``!``, or ``?``) rather
    than matching any ``<...>`` span. A bare comparison operator in prose
    (``salary < $100k and role requires > 5 years``) has no letter/``/``/
    ``!``/``?`` right after the ``<``, so it is never mistaken for a tag
    opener and never fused with an unrelated later ``>`` into one bogus
    "tag" that swallows everything between (#234) — this protects every
    caller of this function, not just ``html_to_plain_text``.
    """
    # Convert <br> variants to newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # Convert <li> to bullet prefix for list items
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.IGNORECASE)
    # Convert closing block-level tags to newlines
    text = re.sub(
        r"</(?:p|div|h[1-6]|li|ul|ol|tr|td|th|table|section|article|"
        r"header|footer|blockquote)\s*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    # Strip remaining (inline) tags, leaving a space so adjacent inline elements
    # do not fuse into one token. Tag-open shape required (see docstring) --
    # NOT the old unconditional `<[^>]+>`, which matched a bare `<` through to
    # the next unrelated `>` anywhere later in the string.
    text = re.sub(r"<(?:[a-zA-Z/!?])[^>]*>", " ", text)
    # Decode any remaining entities (e.g. &amp; &nbsp;)
    text = _html.unescape(text)
    # Collapse runs of spaces/tabs introduced by tag removal (newlines preserved
    # so the structured renderer still sees paragraph/bullet breaks), then trim
    # whitespace hugging the newlines.
    text = re.sub(r"[^\S\n]{2,}", " ", text)
    text = re.sub(r"[^\S\n]*\n[^\S\n]*", "\n", text)
    # Collapse 3+ newlines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_plain_text(raw: str) -> str:
    """Lossless HTML → plain-text for ATS description fragments (JD Layer 2).

    ATS APIs return the JD body already isolated (no page nav/header/footer), so
    the goal is a faithful HTML→text conversion that drops NOTHING — unlike the
    trafilatura-based ``html_extract.html_to_clean_text`` used for full crawled
    pages, whose recall/precision heuristics are tuned for boilerplate removal,
    not for terse standalone fragments.

    Two input shapes, two orderings (#234 fix) -- discriminated via the same
    ``_html_tag_re`` detector ``format_description_filter`` already uses:

    - **Real HTML** (``_html_tag_re`` finds at least one literal tag): strip
      tags FIRST, unescape entities LAST -- delegate straight to
      ``strip_html_to_text``, which already orders it that way. This keeps an
      entity-escaped comparison operator sitting in the prose (``salary &lt;
      $100k``) as inert literal text all the way through tag-stripping, so it
      can never decode into a bare ``<`` that fuses with a later real tag
      into one bogus "tag" span and gets swallowed.
    - **No literal tag anywhere** (plain text, or a body that signals HTML
      ONLY through entity-encoded tags -- Greenhouse's ``content`` field
      arrives as ``&lt;p&gt;…``): unescape FIRST to turn the encoded tags
      back into real ones, THEN strip -- there is nothing else to protect
      here since no real tag exists yet for a decoded ``<`` to fuse with.

    Prior to the #234 fix this function unconditionally unescaped first for
    both shapes, matching the private original's ``html_to_plain_text``
    (``job_finder/web/description_formatter.py``, READ-ONLY reference) --
    which carries the identical bug (verified: same
    ``strip_html_to_text(_html.unescape(raw))`` body). This is a deliberate,
    Wave-4-scoped divergence from that file rather than a byte-identical
    port; a private-repo port-back issue is being filed separately to bring
    the fix upstream. See PR #232 / issue #234.
    """
    if not raw:
        return ""
    if _html_tag_re.search(raw):
        return strip_html_to_text(raw)
    return strip_html_to_text(_html.unescape(raw))


def _is_header(line: str) -> bool:
    """Check if a line is a section header (markdown or plain text)."""
    stripped = line.strip()
    if _md_header_re.match(stripped):
        return True
    return bool(_plain_header_re.match(stripped))


def _header_text(line: str) -> str:
    """Extract display text from a header line (strips markdown #)."""
    stripped = line.strip()
    md = _md_header_re.match(stripped)
    if md:
        return md.group(1).strip()
    return stripped


def _merge_orphaned_words(lines: list[str]) -> list[str]:
    """Merge single capitalized words with their lowercase continuations.

    Fixes browser-paste artifacts where bold verbs (e.g., <strong>Lead</strong>)
    get captured as separate lines from their sentence continuations.
    """
    merged = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if (
            stripped
            and re.match(r"^[A-Z][a-z]+$", stripped)
            and i + 1 < len(lines)
            and lines[i + 1].strip()
            and lines[i + 1].strip()[0].islower()
        ):
            merged.append(f"{stripped} {lines[i + 1].strip()}")
            i += 2
        else:
            merged.append(lines[i])
            i += 1
    return merged


def _render_structured_description(value: str) -> Markup:
    """Render a description with headers, paragraphs, and bullet lists."""
    html_parts = []
    lines = _merge_orphaned_words(value.split("\n"))
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip empty lines
        if not stripped:
            i += 1
            continue

        # Section header
        if _is_header(stripped):
            html_parts.append(
                f'<h4 class="text-sm font-semibold text-slate-200 mt-3 mb-1">'
                f"{escape(_header_text(stripped))}</h4>"
            )
            i += 1
            continue

        # Bullet item -- collect consecutive bullets into a list
        if _bullet_re.match(line):
            bullet_items = []
            while i < len(lines) and _bullet_re.match(lines[i]):
                item_text = re.sub(r"^\s*[-*]\s+", "", lines[i].strip())
                bullet_items.append(f'<li class="mb-0.5">{escape(item_text)}</li>')
                i += 1
            html_parts.append(
                f'<ul class="list-disc list-inside space-y-0.5 ml-1">{"".join(bullet_items)}</ul>'
            )
            continue

        # Regular text line -- render as paragraph
        html_parts.append(f'<p class="mb-1">{escape(stripped)}</p>')
        i += 1

    return Markup("\n".join(html_parts))  # noqa: S704 — all user data passed through escape() above


def format_description_filter(value: str) -> str:
    """Format job description text into safe HTML.

    Handles three description formats:
    1. Structured (markdown headers or plain-text section headers with
       paragraphs and bullet lists) -- renders with proper HTML structure.
    2. Legacy pipe-separated -- renders as a bullet list.
    3. Simple text -- renders as a paragraph.
    """
    if not value:
        return ""

    # Decode any HTML entities stored in the DB before rendering.
    # Must happen BEFORE escape() so entities like &amp; become & first,
    # then escape() re-encodes for safe HTML output.
    value = _html.unescape(value)

    # If the unescaped text contains HTML tags (from entity-encoded HTML
    # in the DB like &lt;p&gt;), strip them to plain text while preserving
    # structure. Without this, escape() in the renderer would re-encode
    # the tags back to visible &lt;p&gt; entities.
    if _html_tag_re.search(value):
        value = strip_html_to_text(value)

    # Legacy pipe-separated format (no newlines, has pipes)
    if "\n" not in value and "|" in value:
        parts = [p.strip() for p in value.split("|") if p.strip()]
        items = "".join(f'<li class="mb-1">{escape(p)}</li>' for p in parts)
        return Markup(f'<ul class="list-disc list-inside space-y-1">{items}</ul>')  # noqa: S704 — items built with escape() per element

    # Single line, no structure
    if "\n" not in value:
        return Markup(f"<p>{escape(value)}</p>")  # noqa: S704 — value escape()d

    # Multi-line: render line-by-line with structure detection
    return _render_structured_description(value)
