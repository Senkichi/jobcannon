"""#234 -- html_to_plain_text chokepoint fix (pure unit tests, no DB).

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

Sabotage verification (actually run, twice, each fix reverted alone against
the other's fix left in place -- results below are the real observed
failures, not a prediction):

- Reverting (a) ALONE (html_to_plain_text back to unconditional
  ``strip_html_to_text(_html.unescape(raw))``, (b)'s tightened stripper
  still active): FAILS ONLY
  test_mixed_body_escaped_tag_like_token_in_prose_survives
  (``assert '<div>' in 'experience with layouts'``) -- early unescaping
  turns the escaped ``&lt;div&gt;`` into a real, validly tag-SHAPED
  ``<div>`` that (b)'s tightened stripper then legitimately strips as if it
  were real markup. test_mixed_body_with_escaped_comparison_operators_survives
  (the refuter's original probe) does NOT fail under (a) alone, because
  "``< $100k``" has no letter/``/``/``!``/``?`` after the ``<``, so (b)'s
  tightened stripper never treats it as a tag boundary regardless of decode
  order -- (b) alone happens to already defend that specific shape.
- Reverting (b) ALONE (inline stripper back to unconditional ``<[^>]+>``,
  (a)'s discrimination still active): FAILS
  test_plain_text_with_comparisons_and_no_tags_is_unchanged (``'a d' !=
  'a < b and c > d'``, everything between the two operators swallowed) and
  the direct strip_html_to_text probes in TestStripHtmlToTextTagOpenShape
  (bare comparison operators, and a comparison fused across a later
  unrelated real tag: ``'Requires score Apply now.'`` losing "``< 100 on
  the assessment.``"). The two escaped-entity mixed-body tests do NOT fail
  under (b) alone, because (a) keeps ``&lt;``/``&gt;`` as inert literal text
  all the way through stripping when a real tag is present, so no bare
  ``<``/``>`` character exists yet for the greedy regex to exploit.

Net: (a) and (b) close two DIFFERENT sub-cases of the same fuse-and-swallow
failure mode (entity-escaped tag-shaped tokens vs. bare-`<`-in-prose
spanning to an unrelated later `>`); both are required for full coverage,
neither is redundant with the other.

Host-level coverage: ``tests/host/test_jd_full.py`` carries the same two
shapes through the real ``set_jd_full`` write path --
``test_mixed_body_with_escaped_comparison_operator_stores_intact_prose``
(fix (b), (a) alone does not discriminate it, sabotage-verified) and
``test_mixed_body_with_escaped_tag_like_token_stores_literal_text`` (fix
(a) specifically, sabotage-verified against a real revert-(a)-alone run --
only the tag-like-token test fails, not the comparison-operator one,
matching the unit-level finding above).
"""

from jobcannon.engine.description_formatter import html_to_plain_text, strip_html_to_text


class TestHtmlToPlainTextMixedBodies:
    """#234 fix: html_to_plain_text on bodies mixing real tags with
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
        # the next real `>` the way the pre-#234 bug dropped them.
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
