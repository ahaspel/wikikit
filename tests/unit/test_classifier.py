"""Unit tests for the new recursive classifier.

The classifier is dormant — not wired into the production pipeline
yet — so these tests exercise it standalone.  They cover:

  * Atomic-shape label derivation (MATH / POEM / IMAGE / …)
  * Recursive descent that builds nested inner_registries
  * Composite BRACE_PIPE wikitable classification (DATA_TABLE /
    MATH_LAYOUT_EQUATIONS / LAYOUT_WRAPPER)
  * The top-level `classify_article` entry point
"""

from __future__ import annotations

from britannica.pipeline.stages.elements._classifier import (
    classify,
    classify_article,
)
from britannica.pipeline.stages.elements._shapes import (
    SHAPE_BRACE_PIPE,
    SHAPE_CHART2,
    SHAPE_DOUBLE_BRACE,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_HTML_TAG,
    SHAPE_OUTLINE,
)


class TestAtomicLabels:
    def test_math(self):
        ce = classify(SHAPE_HTML_TAG, "<math>x^2</math>")
        assert ce.label == "MATH"
        assert ce.raw == "<math>x^2</math>"
        assert ce.inner_text == "x^2"
        assert ce.inner_registry == {}

    def test_poem(self):
        ce = classify(SHAPE_HTML_TAG, "<poem>line one\nline two</poem>")
        assert ce.label == "POEM"
        assert ce.inner_text == "line one\nline two"

    def test_ref(self):
        ce = classify(SHAPE_HTML_TAG, "<ref>note text</ref>")
        assert ce.label == "REF"

    def test_score(self):
        ce = classify(SHAPE_HTML_TAG, "<score>{ \\new Staff }</score>")
        assert ce.label == "SCORE"

    def test_html_table(self):
        # A genuine multi-column grid stays HTML_TABLE.  (Post-flip, `<table>`
        # is routed through the shape classifiers, so a ONE-cell-per-row table
        # is now SINGLE_COLUMN_TABLE — see test_html_table_single_column.)
        ce = classify(SHAPE_HTML_TAG,
                       "<table><tr><td>a</td><td>b</td></tr></table>")
        assert ce.label == "HTML_TABLE"

    def test_html_table_single_column(self):
        # One cell per row → not a grid → routed out of the table path.
        ce = classify(SHAPE_HTML_TAG,
                       "<table><tr><td>a</td></tr><tr><td>b</td></tr></table>")
        assert ce.label == "SINGLE_COLUMN_TABLE"

    def test_hieroglyph_tag(self):
        ce = classify(SHAPE_HTML_TAG, "<hiero>A1-B2</hiero>")
        assert ce.label == "HIEROGLYPH"

    def test_image(self):
        ce = classify(SHAPE_DOUBLE_BRACKET,
                       "[[File:Foo.jpg|thumb|caption]]")
        assert ce.label == "IMAGE"

    def test_image_float(self):
        ce = classify(SHAPE_DOUBLE_BRACE,
                       "{{img float|file=X.jpg|width=200}}")
        assert ce.label == "IMAGE_FLOAT"

    def test_hieroglyph_template(self):
        ce = classify(SHAPE_DOUBLE_BRACE, "{{hieroglyph|A1-B2}}")
        assert ce.label == "HIEROGLYPH"

    def test_ref_self(self):
        ce = classify(SHAPE_HTML_SELF_CLOSING, "<ref name=foo/>")
        assert ce.label == "REF_SELF"
        assert ce.inner_text == ""
        assert ce.inner_registry == {}

    def test_chart2(self):
        ce = classify(SHAPE_CHART2,
                       "{{chart2/start}}A{{chart2/end}}")
        assert ce.label == "CHART2"
        assert ce.inner_text == ""
        assert ce.inner_registry == {}

    def test_outline(self):
        raw = "; head : desc\n; head2 : desc2"
        ce = classify(SHAPE_OUTLINE, raw)
        assert ce.label == "OUTLINE"


class TestNestedClassification:
    def test_poem_with_ref(self):
        ce = classify(SHAPE_HTML_TAG,
                       "<poem>verse <ref>note</ref> end</poem>")
        assert ce.label == "POEM"
        # One REF child registered after recursion.
        assert len(ce.inner_registry) == 1
        ref_ce = next(iter(ce.inner_registry.values()))
        assert ref_ce.label == "REF"
        assert ref_ce.raw == "<ref>note</ref>"

    def test_simple_data_table(self):
        ce = classify(SHAPE_BRACE_PIPE, "{|\n|cell\n|}")
        assert ce.label == "DATA_TABLE"
        assert ce.inner_registry == {}

    def test_table_with_one_math_child_is_data(self):
        # One math child isn't math-dominant (predicate needs ≥2).
        ce = classify(SHAPE_BRACE_PIPE, "{|\n|<math>x</math>\n|}")
        assert ce.label == "DATA_TABLE"
        assert len(ce.inner_registry) == 1
        child = next(iter(ce.inner_registry.values()))
        assert child.label == "MATH"

    def test_math_dominant_wikitable(self):
        # Two math children, ≥75% of elements are math → math-dominant.
        raw = "{|\n|<math>x</math>\n|<math>y</math>\n|}"
        ce = classify(SHAPE_BRACE_PIPE, raw)
        assert ce.label == "MATH_LAYOUT_EQUATIONS"

    def test_captioned_figure_image_with_caption_row(self):
        # Single-image wikitable, image alone in its row, caption
        # row below.  Fires _is_captioned_figure_pred which runs
        # ahead of _is_layout_wrapper_pred.
        raw = (
            "{|\n"
            "|[[File:Foo.jpg]]\n"
            "|-\n"
            "|Fig. 1. A descriptive caption.\n"
            "|}"
        )
        ce = classify(SHAPE_BRACE_PIPE, raw)
        assert ce.label == "CAPTIONED_FIGURE"


class TestClassifyArticle:
    def test_multiple_top_level_elements(self):
        text = ("Body text <math>x</math> more text "
                "[[File:F.jpg]] end")
        placeholderized, registry = classify_article(text)
        assert len(registry) == 2
        labels = sorted(ce.label for ce in registry.values())
        assert labels == ["IMAGE", "MATH"]
        # All registered placeholders appear in the placeholderized
        # body (substituted by the walker).
        for ph in registry:
            assert ph in placeholderized

    def test_empty_text(self):
        placeholderized, registry = classify_article("just plain prose")
        assert registry == {}
        assert placeholderized == "just plain prose"
