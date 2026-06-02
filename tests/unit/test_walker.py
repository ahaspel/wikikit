"""Unit tests for the one-level shape-emitting walker.

The walker is exercised indirectly by every classifier test in
``test_classifier.py``; these tests pin the walker's contract
explicitly:

  * Output shape: ``(placeholderized_text, [(ph, shape, raw), ...])``
  * Top-level shapes only — no recursion into extracts' inner content
  * Correct shape constant for each delimiter family
  * Each placeholder appears in the placeholderized text
"""

from __future__ import annotations

import pytest

from britannica.pipeline.stages.elements._shapes import (
    SHAPE_BRACE_PIPE,
    SHAPE_DOUBLE_BRACE,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_HTML_TAG,
    SHAPE_INLINE_IMAGE,
)
from britannica.pipeline.stages.elements._walker import walk


class TestWalkOutput:
    def test_empty_text(self):
        text_out, extracts = walk("just plain prose")
        assert text_out == "just plain prose"
        assert extracts == []

    def test_math_tag(self):
        text_out, extracts = walk("body <math>x^2</math> end")
        assert len(extracts) == 1
        ph, shape, raw = extracts[0]
        assert shape == SHAPE_HTML_TAG
        assert raw == "<math>x^2</math>"
        assert ph in text_out

    def test_image_link(self):
        # `[[File:…]]` in inline-prose context now classifies as
        # SHAPE_INLINE_IMAGE rather than SHAPE_DOUBLE_BRACKET — the
        # walker emits a more specific label so producers (figure /
        # inline-glyph) can dispatch without re-recognising.
        text_out, extracts = walk("body [[File:Foo.jpg]] end")
        assert len(extracts) == 1
        _ph, shape, raw = extracts[0]
        assert shape == SHAPE_INLINE_IMAGE
        assert raw.startswith("[[File:")

    def test_self_closing_ref(self):
        text_out, extracts = walk("see <ref name=foo/>")
        assert len(extracts) == 1
        _ph, shape, _raw = extracts[0]
        assert shape == SHAPE_HTML_SELF_CLOSING

    def test_brace_pipe_table(self):
        text_out, extracts = walk("{|\n|cell\n|}")
        assert len(extracts) == 1
        _ph, shape, raw = extracts[0]
        assert shape == SHAPE_BRACE_PIPE
        assert raw.startswith("{|") and raw.endswith("|}")

    def test_img_float_template(self):
        text_out, extracts = walk("{{img float|file=X.jpg}}")
        assert len(extracts) == 1
        _ph, shape, _raw = extracts[0]
        assert shape == SHAPE_DOUBLE_BRACE

    def test_hieroglyph_template_emits_double_brace(self):
        _t, extracts = walk("{{hieroglyph|A1-B2}}")
        assert len(extracts) == 1
        _ph, shape, _raw = extracts[0]
        assert shape == SHAPE_DOUBLE_BRACE

    def test_hieroglyph_tag_emits_html_tag(self):
        _t, extracts = walk("<hiero>A1-B2</hiero>")
        assert len(extracts) == 1
        _ph, shape, _raw = extracts[0]
        assert shape == SHAPE_HTML_TAG

    # GUARDRAIL: a recognizer can be registered in `_REGEX_RECOGNIZERS` yet
    # silently inert because its template name is missing from the walker's
    # `_OPENER_HINT_RE` (the efficiency gate deciding which positions the scan
    # even examines) — the walker then never tries it and the element falls to
    # body-text's catch-all and is deleted.  This bit `{{Plain image with
    # caption}}` and `{{ppoem}}` (recognizers added, opener hint not).  One
    # representative instance per DOUBLE_BRACE template, asserted to extract.
    @pytest.mark.parametrize("instance", [
        "{{img float|file=X.jpg}}",
        "{{figure|file=X.jpg}}",
        "{{hieroglyph|A1-B2}}",
        "{{raw image|EB1911 - Volume 01.djvu/5}}",
        "{{Css image crop\n|Image=X.jpg\n}}",
        "{{dual line|A|B}}",
        "{{Plain image with caption|image=File:X.png|caption=Fig. 1}}",
        "{{ppoem|Verse line one\nVerse line two}}",
        "{{EB1911 footer initials|Full Name|F. N.}}",
    ])
    def test_double_brace_template_is_recognized(self, instance):
        _t, extracts = walk(instance)
        assert any(shape == SHAPE_DOUBLE_BRACE for _ph, shape, _raw in extracts), (
            f"{instance[:40]!r} was NOT extracted — likely missing from "
            "_OPENER_HINT_RE (recognizer registered but the scan never reaches it)"
        )

    def test_ordered_list_recognized_as_leaf_with_nesting(self):
        # {{ordered list}} is its own LEAF shape; the balanced scanner must
        # capture the WHOLE nested template as one extract (not split each
        # nested level), and the opener must be in _OPENER_HINT_RE.
        from britannica.pipeline.stages.elements._shapes import SHAPE_ORDERED_LIST
        src = "{{ordered list|type=upper-roman|A|{{ordered list|type=lower-alpha|B|C}}}}"
        _t, extracts = walk(src)
        ol = [e for e in extracts if e[1] == SHAPE_ORDERED_LIST]
        assert len(ol) == 1, f"expected 1 ORDERED_LIST extract, got {extracts!r}"
        assert ol[0][2] == src, "scanner must capture the full nested template"

    def test_multiple_top_level_extracts(self):
        text_out, extracts = walk(
            "before <math>x</math> middle [[File:F.jpg]] end"
        )
        assert len(extracts) == 2
        shapes = sorted(s for _ph, s, _raw in extracts)
        # `<math>` → HTML_TAG; `[[File:F.jpg]]` in inline-prose
        # context → INLINE_IMAGE (more specific than DOUBLE_BRACKET).
        assert shapes == [SHAPE_HTML_TAG, SHAPE_INLINE_IMAGE]
        for ph, _shape, _raw in extracts:
            assert ph in text_out


class TestOneLevelOnly:
    """Linear scanner is one-level-deep: it never recurses into an
    extract's raw bytes.  Outer elements always own their inner ones,
    regardless of which shape is "inner" or "outer" — the recognizer
    that matches first (left-to-right at the outermost scan position)
    extracts the whole region as a single unit.  Inner elements are
    found later by the classifier's recursive walk into the extract's
    inner content.
    """

    def test_brace_pipe_owns_inner_math(self):
        # `{|…|}` wrap is the outermost shape here.  The walker
        # extracts the whole region; the math inside stays as raw
        # bytes within the table for the classifier's recursion.
        _t, extracts = walk("{|\n|<math>x</math>\n|}")
        assert len(extracts) == 1
        _ph, shape, raw = extracts[0]
        assert shape == SHAPE_BRACE_PIPE
        assert "<math>" in raw  # math NOT separately extracted

    def test_poem_owns_inner_ref(self):
        # POEM is the outermost shape; the ref is INSIDE the poem's
        # raw and belongs to the classifier's recursion, NOT the
        # walker's top-level scan.  No more priority-induced flat
        # extraction of nested elements.
        text_out, extracts = walk(
            "before <poem>verse <ref>note</ref> end</poem> after"
        )
        assert len(extracts) == 1
        ph, shape, raw = extracts[0]
        assert shape == SHAPE_HTML_TAG
        assert raw == "<poem>verse <ref>note</ref> end</poem>"
        # The ref is embedded as raw bytes in the poem's extract,
        # not placeholdered out at this level.
        assert "<ref>note</ref>" in raw
        # Only the poem's placeholder appears in the article body.
        assert ph in text_out
        # And the original ref tag is gone from the placeholderized
        # text (the whole poem region was replaced by one
        # placeholder).
        assert "<ref>" not in text_out
