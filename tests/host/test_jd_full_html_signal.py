"""#216 -- _HTML_SIGNAL_RE port parity (pure unit tests, no DB).

The private original's ``_HTML_SIGNAL_RE`` (job_finder/db/_jd_full.py,
READ-ONLY reference) was ported verbatim into ``jobcannon/db/_jd_full.py``
after the prior hosted pattern (``<\\s*(p|div|br|li|ul|span|h\\d)\\b``) missed
two signals the private one catches: an HTML-escaped ``&lt;`` (bodies that
arrive entity-encoded) and a bare closing tag ``</tag>`` with no recognized
opening tag earlier in the text. A body that signaled HTML only through
those forms bypassed ``strip_html_to_text``/``html_to_plain_text`` on the
hosted side and was stored with raw markup -- the exact engine-parity drift
the ported-paths manifest exists to prevent.

These tests exercise the compiled regex directly and carry no Postgres
dependency, unlike ``tests/host/test_jd_full.py`` (module-level
``pytestmark = requires_postgres``) -- the end-to-end write-path integration
test for the entity-encoded case lives there instead, since it needs the
``db_conn``/``posting`` fixtures.
"""

import pytest

from jobcannon.db._jd_full import _HTML_SIGNAL_RE


class TestHtmlSignalRegex:
    """Signal-detection cases mirroring the private pattern's documented
    shapes."""

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param("Salary details &lt;p&gt;up to $150k&lt;/p&gt;", id="entity_encoded"),
            pytest.param(
                "Some prose ending mid-sentence</div> continues here", id="closing_tag_only"
            ),
            pytest.param("<p>Structured intro</p>", id="opening_p"),
            pytest.param("<p class='x'>Structured intro</p>", id="opening_p_with_attr"),
            pytest.param("<div class='x'>content</div>", id="opening_div"),
            pytest.param("Line one<br>Line two", id="opening_br"),
            pytest.param("Line one<br/>Line two", id="opening_br_selfclosing"),
            pytest.param("<li>Bullet one</li>", id="opening_li"),
            pytest.param("<ul><li>a</li></ul>", id="opening_ul"),
            pytest.param("<h1>Heading</h1>", id="opening_h1"),
            pytest.param("<h6>Small heading</h6>", id="opening_h6"),
        ],
    )
    def test_matches_html_signal(self, body):
        assert _HTML_SIGNAL_RE.search(body) is not None

    def test_does_not_match_plain_text(self):
        """Negative control: a stray `<` not immediately followed by a word
        char or `/` (e.g. a salary/compensation comparison) must not be
        mistaken for an HTML signal -- the private pattern requires a word
        char or `/` right after `<` for exactly this reason."""
        body = (
            "We pay competitively -- most peer roles offer less than "
            "$100k, earn < $100k at comparable companies, while this role "
            "starts well above that band for a strong candidate."
        )
        assert _HTML_SIGNAL_RE.search(body) is None

    def test_does_not_match_bare_opening_span_with_no_closing_tag_anywhere(self):
        """Verbatim-port pin: the prior hosted pattern listed `span` among
        recognized opening block tags; the private pattern (ported here
        verbatim) does not. A lone `<span ...>` with no `</...>` anywhere in
        the text is therefore not a signal under the ported pattern -- a
        deliberate narrowing, not an oversight (a `</span>` CLOSING tag
        would still match via the generic `</([\\w]+)>` branch; this body
        has none)."""
        body = "Perks include a <span class='badge'>Remote-first"
        assert _HTML_SIGNAL_RE.search(body) is None
