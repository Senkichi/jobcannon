"""#1967 -- html_to_plain_text chokepoint fix (pure unit tests, no DB).

Ported from the public mirror's ``tests/engine/test_description_formatter.py``
(Senkichi/jobcannon#232, tracked there as #234) — that PR fixed the identical
bug in the public port of this file; this file carried the same bug until
#1967 (verified: same ``strip_html_to_text(_html.unescape(raw))`` body).

``html_to_plain_text`` used to unconditionally unescape entities BEFORE
stripping tags. For a mixed body -- real HTML tags surrounding prose that
itself carries an entity-escaped comparison operator (``salary &lt; $100k``,
the standard HTML encoding for a literal ``<``/``>`` in text) -- that
ordering decoded the entity into a real ``<`` which then fused with a later
real tag boundary, and the old unconditional ``<[^>]+>`` stripper swallowed
everything in between as one bogus "tag". Two independent fixes close this:

(a) ``html_to_plain_text`` now discriminates on whether the raw body already
    contains a literal tag (via the module's existing ``_html_tag_re``): if
    so, entities in the prose are TEXT and must not be unescaped before
    stripping runs (delegates straight to ``strip_html_to_text``, which
    already strips first / unescapes last). Only a body with NO literal tag
    (plain text, or a fully entity-escaped body like Greenhouse's raw
    ``&lt;p&gt;...``) unescapes first.
(b) ``strip_html_to_text``'s inline catch-all stripper was tightened from
    the unconditional ``<[^>]+>`` to an HTML tag-open shape (``<`` followed
    by a letter, ``/``, ``!``, or ``?``), so a bare prose ``<`` (e.g.
    ``salary < $100k``) is never mistaken for a tag opener by ANY caller of
    ``strip_html_to_text``, not just the entity-escaped case (a) handles.

Sabotage verification (actually run against this port -- fix (a)+(b)
reverted together back to the pre-#1967 unconditional-unescape-first shape
and the old ``<[^>]+>`` stripper): 5 of 11 tests fail, deterministically --
``test_mixed_body_with_escaped_comparison_operators_survives``,
``test_mixed_body_escaped_tag_like_token_in_prose_survives``,
``test_plain_text_with_comparisons_and_no_tags_is_unchanged``,
``test_bare_comparison_operators_not_treated_as_tags``, and
``test_comparison_operator_followed_by_later_real_tag_does_not_fuse``. The
other 6 pass even against the sabotaged code: the two fully-escaped-body
tests (``test_fully_escaped_greenhouse_body_decodes_and_strips``,
``test_double_escaped_prose_inside_fully_escaped_body``) have no literal
tag in their raw input, so both code paths unescape first regardless --
fix (a)'s discrimination is a no-op for that shape by construction. The
plain-tag tests (``test_real_tags_still_stripped``,
``test_real_tags_stripped_word_boundary_preserved``,
``test_html_comment_and_processing_instruction_shapes_stripped``) and the
empty-input test are unaffected because real tag-open characters
(letters, ``/``, ``!``, ``?``) match under both the old greedy regex and
the tightened one. See this port's PR body for the exact failing
assertions cited as the sabotage proof.

Net: (a) and (b) close two DIFFERENT sub-cases of the same fuse-and-swallow
failure mode (entity-escaped tag-shaped tokens vs. bare-`<`-in-prose
spanning to an unrelated later `>`); both are required for full coverage,
neither is redundant with the other.
"""

from jobcannon.engine.description_formatter import html_to_plain_text, strip_html_to_text


class TestHtmlToPlainTextMixedBodies:
    """#1967 fix: html_to_plain_text on bodies mixing real tags with
    entity-escaped comparison operators / tag-like tokens in the prose."""

    def test_mixed_body_with_escaped_comparison_operators_survives(self):
        body = (
            "<p>Base salary &lt; $100k and role requires &gt; 5 years of "
            "experience building data platforms</p>"
        )
        result = html_to_plain_text(body)
        assert "Base salary < $100k" in result
        assert "5 years of experience building data platforms" in result
        # Every word must survive -- none dropped between the fused `<` and
        # the next real `>` the way the pre-#1967 bug dropped them.
        for word in (
            "Base",
            "salary",
            "100k",
            "role",
            "requires",
            "5",
            "years",
            "experience",
            "building",
            "data",
            "platforms",
        ):
            assert word in result, f"{word!r} missing from {result!r}"

    def test_mixed_body_escaped_tag_like_token_in_prose_survives(self):
        """An entity-escaped tag-LOOKING token in the prose (not a real tag)
        must survive as literal text, not be silently stripped."""
        body = "<p>experience with &lt;div&gt; layouts</p>"
        result = html_to_plain_text(body)
        assert "<div>" in result
        assert "experience with <div> layouts" in result

    def test_fully_escaped_greenhouse_body_decodes_and_strips(self):
        """No literal tag anywhere (Greenhouse's raw shape) -- unescape
        first, then strip; tags decode AND get stripped, bullet preserved."""
        body = "&lt;p&gt;Hello&lt;/p&gt;&lt;ul&gt;&lt;li&gt;x&lt;/li&gt;&lt;/ul&gt;"
        result = html_to_plain_text(body)
        assert "<p>" not in result
        assert "&lt;" not in result
        assert "Hello" in result
        assert "- x" in result

    def test_double_escaped_prose_inside_fully_escaped_body(self):
        """A double-escaped entity in the prose of a fully-escaped body
        (``&amp;lt;`` -> one unescape pass -> literal ``&lt;`` text, which
        strip_html_to_text's own trailing unescape then resolves) decodes
        exactly once, to `<`."""
        body = "&lt;p&gt;a &amp;lt; b&lt;/p&gt;"
        result = html_to_plain_text(body)
        assert result == "a < b"

    def test_plain_text_with_comparisons_and_no_tags_is_unchanged(self):
        body = "a < b and c > d"
        assert html_to_plain_text(body) == body

    def test_real_tags_still_stripped(self):
        assert html_to_plain_text("<b>Foo</b><i>Bar</i>") == "Foo Bar"
        assert html_to_plain_text("Line one<br>Line two") == "Line one\nLine two"

    def test_empty_and_none_like_input(self):
        assert html_to_plain_text("") == ""


class TestStripHtmlToTextTagOpenShape:
    """(b) direct coverage: the tightened inline stripper on
    strip_html_to_text itself, independent of html_to_plain_text's
    discrimination -- every caller of strip_html_to_text gets this."""

    def test_bare_comparison_operators_not_treated_as_tags(self):
        body = "a < b and c > d"
        assert strip_html_to_text(body) == body

    def test_comparison_operator_followed_by_later_real_tag_does_not_fuse(self):
        """A bare `<` in prose, followed later by an unrelated real closing
        tag, must not have the span between them swallowed as one "tag"."""
        body = "Requires score < 100 on the assessment.<p>Apply now.</p>"
        result = strip_html_to_text(body)
        assert "Requires score < 100 on the assessment." in result
        assert "Apply now." in result

    def test_real_tags_stripped_word_boundary_preserved(self):
        assert strip_html_to_text("<b>Foo</b><i>Bar</i>") == "Foo Bar"

    def test_html_comment_and_processing_instruction_shapes_stripped(self):
        # `!` and `?` tag-open shapes are part of the tightened rule so
        # comment/processing-instruction-like spans strip like any tag.
        assert strip_html_to_text("<!-- note -->Hello") == "Hello"
