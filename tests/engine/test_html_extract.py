"""Unit tests for the structure-aware HTML→text extractor (JD Layer 2, step 1).

Verifies html_to_clean_text():
  - strips nav/header/footer boilerplate (trafilatura primary path),
  - preserves headings + list structure,
  - drops MS-Word-export attribute sludge (only visible prose survives),
  - removes gross within-document block duplication (Bozzuto-style ×N repeat),
  - keeps terse "Compensation: $X" lines (favor_precision regression guard),
  - falls back to BeautifulSoup when trafilatura can't parse the page,
  - returns None on empty / whitespace-only / None input.
"""

from jobcannon.engine.html_extract import html_to_clean_text

# A realistic JD body. trafilatura's fallback emits degenerate output on
# too-short documents, so fixtures use real-length prose like production.
_JD_MAIN = """
<main>
<h2>Senior Data Engineer</h2>
<p>We are hiring a Senior Data Engineer to join our platform team in San
Francisco. This is a full-time hybrid role with strong benefits, equity, and
real growth opportunity for the right person.</p>
<h3>Responsibilities</h3>
<ul>
<li>Build and own batch and streaming data pipelines end to end.</li>
<li>Partner with analytics and ML teams on the warehouse data model.</li>
</ul>
<p>Compensation: $180,000 - $220,000 per year plus equity.</p>
</main>
"""

_FULL_PAGE = f"""<html><head><title>Careers</title></head><body>
<nav>Home About Careers Login Sign In</nav>
<header>MegaCorp Global Holdings</header>
{_JD_MAIN}
<footer>Equal Opportunity Employer. Copyright 2026 MegaCorp. Privacy Policy.</footer>
</body></html>"""


def test_strips_nav_header_footer_boilerplate():
    out = html_to_clean_text(_FULL_PAGE)
    assert out is not None
    # Body prose survives
    assert "Senior Data Engineer" in out
    assert "batch and streaming data pipelines" in out
    # Chrome is gone
    assert "Login" not in out
    assert "Sign In" not in out
    assert "Privacy Policy" not in out
    assert "Equal Opportunity Employer" not in out


def test_preserves_headings_and_list_structure():
    out = html_to_clean_text(_FULL_PAGE)
    assert out is not None
    # Heading text retained (markdown heading marker or at least the words)
    assert "Responsibilities" in out
    # List items survive as distinct lines
    assert "Partner with analytics" in out


def test_keeps_terse_compensation_line():
    # favor_precision must not strip a short, signal-bearing comp line.
    out = html_to_clean_text(_FULL_PAGE)
    assert out is not None
    assert "$180,000" in out
    assert "$220,000" in out


def test_drops_word_export_attribute_sludge():
    # MS-Word export wraps prose in spans with data-ccp-props / data-contrast.
    # Only the visible text should survive — never the attribute tokens.
    html = """<html><body><main>
<p><span data-ccp-props="{&quot;201341983&quot;:0}" data-contrast="auto">The
ideal candidate has seven years of experience building distributed systems and
mentoring engineers across multiple teams in a fast-paced environment.</span></p>
<p><span data-contrast="none">You will report directly to the VP of Engineering
and own the technical roadmap for the data platform organization.</span></p>
</main></body></html>"""
    out = html_to_clean_text(html)
    assert out is not None
    assert "ideal candidate has seven years" in out
    assert "data-ccp-props" not in out
    assert "data-contrast" not in out
    assert "201341983" not in out


def test_removes_gross_block_duplication():
    # Bozzuto-style bloat: the same real JD block repeated 20x. The cleaned
    # output must collapse exact-duplicate blocks to a single occurrence.
    block = """<h3>About the Role</h3>
<p>We are hiring a Property Manager to oversee a 300-unit residential community
in the metro area. You will lead a team and own resident satisfaction, financial
performance, and the capital improvement project portfolio.</p>
<ul><li>Manage leasing, renewals, and resident relations day to day.</li>
<li>Own the property budget and monthly financial reporting cadence.</li></ul>
<p>Compensation: $90,000 - $110,000 plus an annual performance bonus.</p>"""
    html = (
        f"<html><body><nav>Home Careers</nav><main>{block * 20}</main>"
        f"<footer>EOE Copyright 2026</footer></body></html>"
    )
    out = html_to_clean_text(html)
    assert out is not None
    # Content present exactly once, not twenty times.
    assert out.count("About the Role") == 1
    assert out.count("oversee a 300-unit residential community") == 1
    # And the result is far smaller than the raw 20x repetition.
    assert len(out) < 2000


def test_preserves_all_sections_no_silent_drop():
    # Regression guard: favor_precision/default drop lower-confidence in-content
    # blocks (a posting's Requirements section + bullets). The extractor must use
    # favor_recall so NO section is silently dropped — the failure mode the whole
    # JD-extraction effort exists to prevent.
    html = """<html><body><main>
<p>We are hiring a Staff Engineer to lead our platform team. You will own
reliability for a high-traffic API used by millions of users every day.</p>
<h3>Requirements</h3>
<ul>
<li>Eight or more years building distributed systems at real scale.</li>
<li>Deep Python and Go experience in production environments.</li>
</ul>
<p>Compensation: $200,000 - $240,000 plus meaningful equity.</p>
</main></body></html>"""
    out = html_to_clean_text(html)
    assert out is not None
    assert "Requirements" in out
    assert "distributed systems at real scale" in out
    assert "Deep Python and Go experience" in out
    assert "$200,000" in out


def test_falls_back_to_beautifulsoup_when_trafilatura_returns_none():
    # trafilatura returns None on bare fragments with no article structure.
    # The BeautifulSoup fallback must still recover the visible text so we
    # never regress to empty on pages that extract today.
    html = (
        "<div>Staff Backend Engineer. Remote within the US. We need someone to "
        "lead our API platform and own reliability for a high-traffic service "
        "used by millions of customers every single day across the globe.</div>"
    )
    out = html_to_clean_text(html)
    assert out is not None
    assert "Staff Backend Engineer" in out
    assert "lead our API platform" in out


def test_returns_none_on_empty_inputs():
    assert html_to_clean_text(None) is None
    assert html_to_clean_text("") is None
    assert html_to_clean_text("   \n\t  ") is None
