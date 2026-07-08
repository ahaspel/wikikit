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
    SHAPE_DOUBLE_BRACE,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_HTML_TAG,
    SHAPE_OUTLINE,
    SHAPE_PAIRED_WRAPPER,
    SHAPES,
    strip_outer,
)


class TestShapeVocabulary:
    def test_shape_count(self):
        # 7 delimiter-balanced/text shapes + structural extras:
        #   BODY (residual prose between other elements — task #14:
        #     SHAPE_BODY, owner-of-output principle says every span
        #     maps to one producer),
        #   PAIRED_WRAPPER (``{{NAME/s}}…{{NAME/e}}`` paired open/close span —
        #     the merged former CENTER + CHART2: one STRUCTURE, two families the
        #     classifier tells apart by NAME (chart2 → CHART2, else → CENTER);
        #     the producers/labels are unchanged),
        #   (ORDERED_LIST, SECTION and MIRROR_GLYPH were collapsed back into
        #     their generic delimiter shapes — DOUBLE_BRACE, HTML_SELF_CLOSING
        #     and HTML_TAG respectively — with the type carve moved from the
        #     walker to the classifier; the producers/labels are unchanged.)
        #   (STRIP / PARAM / SHOULDER / RUNNING_HEADER / SPAN_TITLE / HTML_STYLE
        #     — the six STYLED-derived structures — were ALSO collapsed back into
        #     their generic shapes: the four template-form ones (STRIP / PARAM /
        #     SHOULDER / RUNNING_HEADER) ride DOUBLE_BRACE and the two styled-tag
        #     ones (SPAN_TITLE / HTML_STYLE) ride HTML_TAG, with the type carve
        #     moved from the walker to the classifier's two label-derivers; the
        #     producers/labels (`process_strip` / `process_param` / … /
        #     `process_html_style`) are unchanged.)
        # NOINCLUDE was removed when the article pipeline started
        # wiping `<noinclude>` tags upstream in `_transform_text_v2`
        # — chrome content owned by explicit template recognizers,
        # plate pipeline uses its own walker.
        # Bump this count alongside ``_shapes.SHAPES``.  Includes PAGE
        # (``\x01PAGE:N\x01``) and TITLE (the ``«TITLE»…«/TITLE»`` stamp from
        # ``preprocess_article``) — both injected markers recognized as elements.
        # Net −7 vs the 17-shape post-figure-delete state: the six STYLED-derived
        # shapes (STRIP / PARAM / SHOULDER / RUNNING_HEADER / SPAN_TITLE /
        # HTML_STYLE) dissolved into the generic DOUBLE_BRACE / HTML_TAG shapes,
        # and INLINE_IMAGE dissolved into DOUBLE_BRACKET (the walker draws no
        # inline-vs-block image distinction — the raw never marks one) —
        # recognition by name/attribute is the classifier's job, not the walker's
        # — leaving 10.
        assert len(SHAPES) == 10
        assert SHAPE_HTML_TAG in SHAPES
        assert SHAPE_DOUBLE_BRACE in SHAPES
        assert SHAPE_PAIRED_WRAPPER in SHAPES

    def test_all_shapes_are_strings(self):
        assert all(isinstance(s, str) for s in SHAPES)

    def test_leaf_shapes_subset(self):
        assert LEAF_SHAPES <= SHAPES
        assert SHAPE_HTML_SELF_CLOSING in LEAF_SHAPES
        # PAIRED_WRAPPER is NOT a leaf: CENTER un-leafed into a composite that
        # recurses its own inner; only the CHART2 family stays leaf, gated inside
        # `classify` (by name), not by the shape being in LEAF_SHAPES.
        assert SHAPE_PAIRED_WRAPPER not in LEAF_SHAPES


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

    def test_paired_wrapper_chart2_strips_to_empty(self):
        # PAIRED_WRAPPER's chart2 family isn't walked — its inner is
        # non-wikitext, so strip_outer returns "" (the old CHART2 contract).
        raw = "{{chart2/start}}A → B{{chart2/end}}"
        assert strip_outer(SHAPE_PAIRED_WRAPPER, raw) == ""

    def test_paired_wrapper_center_peels_wrapper(self):
        # PAIRED_WRAPPER's centring family peels the `{{NAME/s}}` opener and
        # `{{NAME/e}}` closer (the old CENTER contract).
        raw = "{{c/s}}centred text{{c/e}}"
        assert strip_outer(SHAPE_PAIRED_WRAPPER, raw) == "centred text"

    def test_unknown_shape_raises(self):
        with pytest.raises(ValueError, match="Unknown shape"):
            strip_outer("MADE_UP_SHAPE", "anything")
