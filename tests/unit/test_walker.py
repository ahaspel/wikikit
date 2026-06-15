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
    SHAPE_BODY,
    SHAPE_BRACE_PIPE,
    SHAPE_DOUBLE_BRACE,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_HTML_TAG,
)
from britannica.pipeline.stages.elements._walker import walk


def _non_body(extracts):
    """Real (non-prose) extracts: BODY is now its own element shape, emitted
    for every residual prose run between other elements.  These tests pin the
    structural (non-BODY) elements, so filter the interleaved BODY runs out."""
    return [e for e in extracts if e[1] != SHAPE_BODY]


class TestWalkOutput:
    def test_empty_text(self):
        # Plain prose is now its own BODY element: the walker emits one
        # SHAPE_BODY extract carrying the run and a placeholder in its place.
        text_out, extracts = walk("just plain prose")
        assert len(extracts) == 1
        ph, shape, raw = extracts[0]
        assert shape == SHAPE_BODY
        assert raw == "just plain prose"
        assert text_out == ph

    def test_math_tag(self):
        text_out, extracts = walk("body <math>x^2</math> end")
        real = _non_body(extracts)
        assert len(real) == 1
        ph, shape, raw = real[0]
        assert shape == SHAPE_HTML_TAG
        assert raw == "<math>x^2</math>"
        assert ph in text_out

    def test_image_link(self):
        # `[[File:…]]` is ONE shape regardless of context — SHAPE_DOUBLE_BRACKET.
        # The walker draws no inline-vs-block distinction (the raw never marks
        # one); the image is a leaf, and its surrounding prose / line-breaks /
        # table cell do the layout.
        text_out, extracts = walk("body [[File:Foo.jpg]] end")
        real = _non_body(extracts)
        assert len(real) == 1
        _ph, shape, raw = real[0]
        assert shape == SHAPE_DOUBLE_BRACKET
        assert raw.startswith("[[File:")

    def test_self_closing_ref(self):
        text_out, extracts = walk("see <ref name=foo/>")
        real = _non_body(extracts)
        assert len(real) == 1
        _ph, shape, _raw = real[0]
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
        # ({{EB1911 footer initials}} is no longer a DOUBLE_BRACE element — it's a
        #  contributor FIELD, cut by strip_attributions before the walker.)
    ])
    def test_double_brace_template_is_recognized(self, instance):
        _t, extracts = walk(instance)
        assert any(shape == SHAPE_DOUBLE_BRACE for _ph, shape, _raw in extracts), (
            f"{instance[:40]!r} was NOT extracted — likely missing from "
            "_OPENER_HINT_RE (recognizer registered but the scan never reaches it)"
        )

    def test_ordered_list_recognized_as_leaf_with_nesting(self):
        # {{ordered list}} is carved by the generic DOUBLE_BRACE shape (a leaf);
        # the balanced scanner must capture the WHOLE nested template as one
        # extract (not split each nested level), and the opener must be in
        # _OPENER_HINT_RE.  The classifier routes the `ordered list` name →
        # ORDERED_LIST label (verified in test_classifier).
        src = "{{ordered list|type=upper-roman|A|{{ordered list|type=lower-alpha|B|C}}}}"
        _t, extracts = walk(src)
        ol = [e for e in extracts
              if e[1] == SHAPE_DOUBLE_BRACE and e[2].startswith("{{ordered list")]
        assert len(ol) == 1, f"expected 1 ordered-list extract, got {extracts!r}"
        assert ol[0][2] == src, "scanner must capture the full nested template"

    def test_multiple_top_level_extracts(self):
        text_out, extracts = walk(
            "before <math>x</math> middle [[File:F.jpg]] end"
        )
        real = _non_body(extracts)
        assert len(real) == 2
        shapes = sorted(s for _ph, s, _raw in real)
        # `<math>` → HTML_TAG; `[[File:F.jpg]]` → DOUBLE_BRACKET (one image
        # shape, no inline distinction).  sorted() orders them alphabetically.
        assert shapes == [SHAPE_DOUBLE_BRACKET, SHAPE_HTML_TAG]
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
        real = _non_body(extracts)
        assert len(real) == 1
        ph, shape, raw = real[0]
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


class TestGenericDoubleBraceRecognizer:
    """The ONE generic `{{…}}` recognizer bounds EVERY double-brace template as a
    single DOUBLE_BRACE unit — including a previously-unrecognized one whose body
    holds a `[[File:…]]` / `{{sc|…}}`.  Before the flip such a `{{…}}` matched no
    type-specific opener, so the scanner walked INSIDE it and shredded the inner
    image/template out from under the wrapper (orphaning the wrapper's halves)."""

    def test_familytree_with_image_and_sc_is_one_unit_not_shredded(self):
        src = "{{familytree|[[File:X.png|18px]] {{sc|Y}}}}"
        _t, extracts = walk(src)
        real = _non_body(extracts)
        # ONE extract — the whole `{{familytree|…}}` — and it is DOUBLE_BRACE.
        assert len(real) == 1, (
            f"expected the whole {{{{familytree|…}}}} as ONE extract, got "
            f"{[(s, r[:30]) for _p, s, r in real]!r}"
        )
        _ph, shape, raw = real[0]
        assert shape == SHAPE_DOUBLE_BRACE
        assert raw == src, "the generic recognizer must capture the WHOLE template"
        # The inner image is NOT separately lifted (it rides as raw bytes inside
        # the familytree extract for the producer's own recursion).
        assert not any(s == SHAPE_DOUBLE_BRACKET for _p, s, _r in extracts)
        assert "[[File:X.png|18px]]" in raw and "{{sc|Y}}" in raw

    def test_unknown_named_template_still_bounded_as_double_brace(self):
        # A template with NO type-specific opener (here a made-up `{{zzz|…}}`)
        # is still bounded as ONE DOUBLE_BRACE unit by the generic recognizer —
        # the walker recognizes its SHAPE; routing/raising is the classifier's job.
        _t, extracts = walk("a {{zzz|body [[File:F.jpg]]}} b")
        real = _non_body(extracts)
        assert len(real) == 1
        _ph, shape, raw = real[0]
        assert shape == SHAPE_DOUBLE_BRACE
        assert raw == "{{zzz|body [[File:F.jpg]]}}"

    def test_triple_brace_degenerate_keeps_inner_template(self):
        # A degenerate `{{{name|…}}` (stray leading `{`, double close) must not
        # crash: the leading `{` is a literal (body), the inner `{{Polytonic|ρ}}`
        # is recognized as a normal DOUBLE_BRACE.
        _t, extracts = walk("x {{{Polytonic|ρ}} y")
        db = [e for e in extracts if e[1] == SHAPE_DOUBLE_BRACE]
        assert len(db) == 1
        assert db[0][2] == "{{Polytonic|ρ}}"
