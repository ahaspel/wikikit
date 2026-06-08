"""Unit tests for the shape vocabulary and `strip_outer`.

These cover the walker/classifier interface contract — `strip_outer`
peels each shape's delimiters and returns the inner content.
Per-label specifics (e.g. IMAGE's `EXTCAP:` tail) do NOT belong
here; they live in the per-label classifier code.
"""

from __future__ import annotations

import pytest

from britannica.pipeline.stages.elements._shapes import (
    LEAF_SHAPES,
    SHAPE_BRACE_PIPE,
    SHAPE_CHART2,
    SHAPE_DOUBLE_BRACE,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_FIGURE,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_HTML_TAG,
    SHAPE_OUTLINE,
    SHAPE_SECTION,
    SHAPE_STYLED,
    SHAPES,
    strip_outer,
)


class TestShapeVocabulary:
    def test_shape_count(self):
        # 7 delimiter-balanced/text shapes + structural extras:
        #   FIGURE (image + structural caption run),
        #   SECTION (``<section begin/end/>``),
        #   INLINE_IMAGE (``[[File:…]]`` in inline-prose context —
        #     more specific than DOUBLE_BRACKET so producers don't
        #     re-recognise),
        #   BODY (residual prose between other elements — task #14:
        #     SHAPE_BODY, owner-of-output principle says every span
        #     maps to one producer),
        #   MIRROR_GLYPH (``<span style="{{mirrorH}}">…</span>``),
        #   CENTER (``{{NAME/s}}…{{NAME/e}}`` paired-wrapper span),
        #   ORDERED_LIST (nested ``{{ordered list|…}}`` classification, a leaf),
        #   STYLED (``<div>``/``<p>``/``<span>`` carrying {{Ts}}/style=/align= —
        #     the ONE styled-wrapper element; producer derives CSS + recurses).
        # NOINCLUDE was removed when the article pipeline started
        # wiping `<noinclude>` tags upstream in `_transform_text_v2`
        # — chrome content owned by explicit template recognizers,
        # plate pipeline uses its own walker.
        # Bump this count alongside ``_shapes.SHAPES``.  Includes PAGE
        # (``\x01PAGE:N\x01``) and TITLE (the ``«TITLE»…«/TITLE»`` stamp from
        # ``preprocess_article``) — both injected markers recognized as elements.
        assert len(SHAPES) == 17
        assert SHAPE_FIGURE in SHAPES
        assert SHAPE_SECTION in SHAPES
        assert SHAPE_STYLED in SHAPES

    def test_all_shapes_are_strings(self):
        assert all(isinstance(s, str) for s in SHAPES)

    def test_leaf_shapes_subset(self):
        assert LEAF_SHAPES <= SHAPES
        assert SHAPE_HTML_SELF_CLOSING in LEAF_SHAPES
        assert SHAPE_CHART2 in LEAF_SHAPES


class TestStripOuter:
    def test_brace_pipe_simple(self):
        raw = "{|\n|cell\n|}"
        assert strip_outer(SHAPE_BRACE_PIPE, raw) == "|cell"

    def test_brace_pipe_with_header_attrs(self):
        raw = '{| class="wikitable" border=1\n|a||b\n|}'
        assert strip_outer(SHAPE_BRACE_PIPE, raw) == "|a||b"

    def test_brace_pipe_multiline(self):
        raw = "{|\n|-\n|a\n|-\n|b\n|}"
        # Strip the {|...\n and \n|}, leaving the body.
        result = strip_outer(SHAPE_BRACE_PIPE, raw)
        assert result.startswith("|-")
        assert result.endswith("|b")

    def test_html_tag_math(self):
        assert strip_outer(SHAPE_HTML_TAG, "<math>x^2</math>") == "x^2"

    def test_html_tag_with_attrs(self):
        assert strip_outer(
            SHAPE_HTML_TAG, '<ref name="foo">body</ref>'
        ) == "body"

    def test_html_tag_poem_multiline(self):
        raw = "<poem>line 1\nline 2</poem>"
        assert strip_outer(SHAPE_HTML_TAG, raw) == "line 1\nline 2"

    def test_html_tag_case_insensitive(self):
        assert strip_outer(SHAPE_HTML_TAG, "<MATH>x</MATH>") == "x"

    def test_html_self_closing_yields_empty(self):
        assert strip_outer(SHAPE_HTML_SELF_CLOSING, '<ref name="x"/>') == ""

    def test_double_bracket_file(self):
        raw = "[[File:Foo.jpg|thumb|caption text]]"
        assert strip_outer(SHAPE_DOUBLE_BRACKET, raw) == (
            "File:Foo.jpg|thumb|caption text"
        )

    def test_double_bracket_with_trailing_whitespace(self):
        raw = "[[File:Foo.jpg]]\n"
        assert strip_outer(SHAPE_DOUBLE_BRACKET, raw) == "File:Foo.jpg"

    def test_double_brace_img_float(self):
        raw = "{{img float|file=Foo.jpg|width=200}}"
        assert strip_outer(SHAPE_DOUBLE_BRACE, raw) == (
            "img float|file=Foo.jpg|width=200"
        )

    def test_double_brace_hieroglyph(self):
        raw = "{{hieroglyph|A1-B2}}"
        assert strip_outer(SHAPE_DOUBLE_BRACE, raw) == "hieroglyph|A1-B2"

    def test_outline_passthrough(self):
        raw = "; head : desc\n; head2 : desc2"
        # OUTLINE has no delimiters — the bytes ARE the content.
        assert strip_outer(SHAPE_OUTLINE, raw) == raw

    def test_chart2_strips_to_empty(self):
        # CHART2 content isn't walked — its inner is non-wikitext.
        raw = "{{chart2/start}}A → B{{chart2/end}}"
        assert strip_outer(SHAPE_CHART2, raw) == ""

    def test_unknown_shape_raises(self):
        with pytest.raises(ValueError, match="Unknown shape"):
            strip_outer("MADE_UP_SHAPE", "anything")
