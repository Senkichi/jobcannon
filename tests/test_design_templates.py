"""Structural guards for the Living Journal template layer (spec §7.3–7.5).

1. Class closure: every class a template references must be defined in jc.css
   (or allowlisted) — catches invented names and Tailwind leftovers at once.
2. No color literals in applied template styles (legal_page.html's inline
   <style> composes from var(--lj-*); literal channels are banned there too).
3. No literal third-party resource origins: every URL a template loads a
   resource from (script/link/img/iframe attributes, url() in applied CSS)
   is parsed with urlsplit and must have no hostname of its own. That is the
   template-level half of the CSP's `'self'` posture — the only external
   origin (base.html's Clerk loader) is Jinja-templated from config and
   owned by jobcannon.web.security_headers' derivation, so a hardcoded
   deny-list of CDN hosts is neither needed nor safe (a new CDN would slip
   past it, and substring-matching a host inside a URL is the exact
   pattern CodeQL's py/incomplete-url-substring-sanitization flags).

The color-literal scan is scoped to APPLIED CSS contexts (<style> block bodies
with CSS comments stripped, plus inline style="..." values). The plan's
scan-every-line version false-positived on hex-shaped issue-number comments
(#222 / #207 / #182 / ...), id anchors (hx-target="#feed-content"), and href
fragments that recur across ~16 templates — none of which style anything.
Scoping to applied styling matches the guard's intent (no literal color VALUES
outside lj-tokens.css) and is backed by a positive control so it can never pass
vacuously (resolves lj-gap-notes.md gap 1).
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

WEB = Path(__file__).resolve().parent.parent / "jobcannon" / "web"
TEMPLATES = sorted(WEB.glob("templates/**/*.html"))

_DEFINED_RE = re.compile(r"\.([a-zA-Z][\w-]*)")
_CLASS_ATTR_RE = re.compile(r'class="([^"]*)"')
_JINJA_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
_ALLOWED_PREFIXES = ("htmx-",)  # htmx runtime classes
_ALLOWED = {"legal-prose", "table-scroll"}  # defined in legal_page.html's inline style


def _defined_classes() -> set[str]:
    css = (WEB / "static" / "jc.css").read_text(encoding="utf-8")
    return set(_DEFINED_RE.findall(css))


def _referenced() -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}
    for path in TEMPLATES:
        text = _JINJA_RE.sub(" ", path.read_text(encoding="utf-8"))
        tokens = {tok for attr in _CLASS_ATTR_RE.findall(text) for tok in attr.split()}
        if tokens:
            refs[path.name] = tokens
    return refs


def test_template_classes_close_over_jc_css():
    defined = _defined_classes() | _ALLOWED
    problems = {
        name: sorted(
            tok for tok in tokens if tok not in defined and not tok.startswith(_ALLOWED_PREFIXES)
        )
        for name, tokens in _referenced().items()
    }
    problems = {k: v for k, v in problems.items() if v}
    assert not problems, f"classes referenced but not defined in jc.css: {problems}"


def test_scan_found_a_plausible_number_of_classes():
    total = sum(len(v) for v in _referenced().values())
    assert total >= 40, "class scan collapsed — check the regexes"


_LITERAL_PATTERNS = (
    re.compile(r"#[0-9a-fA-F]{3,8}\b"),
    re.compile(r"\b(?:rgb|rgba|hsl|hsla|oklch|oklab)\(\s*\d"),
)

_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
_INLINE_STYLE_RE = re.compile(r'style="([^"]*)"')
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def _applied_style_text(html: str) -> str:
    """Every APPLIED CSS context in a template: <style> block bodies (CSS
    comments stripped) plus inline style="..." values. A color literal only
    violates the guard where it takes effect; hex-shaped issue-number comments
    (#222), id anchors (#feed-content), and href fragments style nothing."""
    parts = [_CSS_COMMENT_RE.sub(" ", block) for block in _STYLE_BLOCK_RE.findall(html)]
    parts.extend(_INLINE_STYLE_RE.findall(html))
    return "\n".join(parts)


def test_templates_have_no_color_literals():
    hits = []
    for path in TEMPLATES:
        applied = _applied_style_text(path.read_text(encoding="utf-8"))
        for i, line in enumerate(applied.splitlines(), 1):
            if any(p.search(line) for p in _LITERAL_PATTERNS):
                hits.append((path.name, i, line.strip()))
    assert not hits, f"color literals in applied template styles: {hits}"


def test_applied_style_extractor_is_not_vacuous():
    """Positive control for _applied_style_text: without it, a broken extractor
    makes test_templates_have_no_color_literals pass for the wrong reason
    (nothing scanned). Proves the real legal_page.html <style> block is pulled
    and comment-stripped, and that a synthetic sample surfaces genuine applied
    literals while ignoring a commented one."""
    legal = (WEB / "templates" / "legal_page.html").read_text(encoding="utf-8")
    applied = _applied_style_text(legal)
    assert ".legal-prose" in applied, "extractor missed legal_page.html's <style> block"
    assert "#222" not in applied and "#207" not in applied, "CSS comments not stripped"

    sample = '<style>a { color: #fff } /* #222 */</style><p style="color: #abcdef">x</p>'
    extracted = _applied_style_text(sample)
    assert "#fff" in extracted and "#abcdef" in extracted
    assert "#222" not in extracted
    assert any(p.search(extracted) for p in _LITERAL_PATTERNS)


# Resource-loading attributes per the HTML spec (fixed vocabulary, not app
# state): the tags whose src/href fetch bytes into the page, as opposed to
# <a href>, which merely navigates.
_RESOURCE_ATTR_RE = re.compile(
    r"<(?:script|link|img|iframe|source|video|audio|embed)\b[^>]*?\s(?:src|href)\s*=\s*"
    r"[\"']([^\"']*)[\"']",
    re.I | re.S,
)
_CSS_URL_RE = re.compile(r"url\(\s*[\"']?([^\"')]+)[\"']?\s*\)", re.I)
_JINJA_EXPR_RE = re.compile(r"\{\{.*?\}\}", re.S)


def _resource_urls(html: str) -> list[str]:
    """Every URL a template loads a resource from: resource-tag src/href
    values plus url() references inside applied CSS."""
    return _RESOURCE_ATTR_RE.findall(html) + _CSS_URL_RE.findall(_applied_style_text(html))


def _literal_external_host(url: str) -> str | None:
    """The hostname of a resource URL when, and only when, it is a literal
    third-party origin. Relative and same-origin paths have none. A
    Jinja-templated URL (url_for('static', ...) assets, base.html's Clerk
    loader whose host comes from config) resolves at render time and is
    covered by the CSP derivation in jobcannon.web.security_headers, not
    here."""
    if _JINJA_EXPR_RE.search(url):
        return None
    return urlsplit(url.strip()).hostname


def test_templates_load_no_literal_third_party_resources():
    hits = {}
    for path in TEMPLATES:
        hosts = sorted(
            {
                host
                for url in _resource_urls(path.read_text(encoding="utf-8"))
                if (host := _literal_external_host(url))
            }
        )
        if hosts:
            hits[path.name] = hosts
    assert not hits, f"templates load resources from literal third-party origins: {hits}"


def test_resource_url_extractor_is_not_vacuous():
    """Positive control: the real templates must yield resource URLs (the
    self-hosted stylesheets/htmx plus the Clerk loader all go through the
    extractor), and a synthetic CDN sample must surface exactly its literal
    hosts while relative, url_for-templated, and config-templated URLs pass."""
    real = [url for p in TEMPLATES for url in _resource_urls(p.read_text(encoding="utf-8"))]
    assert len(real) >= 4, "resource-URL scan collapsed — check the regexes"
    assert all(_literal_external_host(url) is None for url in real)

    sample = (
        '<script src="https://unpkg.com/htmx.org@1.9.10"></script>\n'
        '<link rel="stylesheet"\n      href="//cdn.tailwindcss.com">\n'
        "<style>@font-face { src: url(https://fonts.gstatic.com/s/inter.woff2) }</style>\n"
        "<link rel=\"stylesheet\" href=\"{{ url_for('static', filename='jc.css') }}\">\n"
        '<script src="https://{{ clerk_frontend_api_host }}/npm/clerk.js"></script>\n'
        '<img src="/static/logo.svg"><img src="data:image/png;base64,AAAA">'
    )
    hosts = {host for url in _resource_urls(sample) if (host := _literal_external_host(url))}
    assert hosts == {"unpkg.com", "cdn.tailwindcss.com", "fonts.gstatic.com"}
