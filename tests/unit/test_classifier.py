"""Unit tests for the new recursive classifier.

The classifier is dormant — not wired into the production pipeline
yet — so these tests exercise it standalone.  They cover:

  * Atomic-shape label derivation (MATH / POEM / IMAGE / …)
  * Recursive descent that builds nested inner_registries
  * Composite BRACE_PIPE wikitable classification (TABLE /
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
    SHAPE_DOUBLE_BRACE,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_HTML_TAG,
    SHAPE_OUTLINE,
    SHAPE_PAIRED_WRAPPER,
)


def _labels(ce):
    """Every label in the classified subtree (self + all descendants)."""
    out = [ce.label]
    for c in (ce.inner_registry or {}).values():
        out += _labels(c)
    return out


def _flat_text(ce):
    """Reconstruct the subtree's text by substituting each child placeholder."""
    txt = ce.inner_text
    for ph, c in (ce.inner_registry or {}).items():
        txt = txt.replace(ph, _flat_text(c))
    return txt


class TestAtomicLabels:
    def test_math(self):
        # `<math>` is an opaque tag — a LEAF.  Its LaTeX interior is verbatim and
        # never recursed (a `{{…}}` inside is LaTeX grouping, NOT a template), so
        # the classifier does not descend: empty inner_registry, inner_text is the
        # raw math content.  Consistent with `<math>`-as-leaf in test_math_cell.
        ce = classify(SHAPE_HTML_TAG, "<math>x^2</math>")
        assert ce.label == "MATH"
        assert ce.raw == "<math>x^2</math>"
        assert ce.inner_registry == {}
        assert ce.inner_text == "x^2"

    def test_poem(self):
        ce = classify(SHAPE_HTML_TAG, "<poem>line one\nline two</poem>")
        assert ce.label == "POEM"
        # Verse line breaks are recognized at the READ POINT: each `\n` between lines becomes a
        # `<br>` (which the walker owns as «BR»), so the body decomposes into per-line BODY
        # children separated by a BR node — fully recursive, the breaks carried as structure.
        children = list(ce.inner_registry.values())
        assert [c.label for c in children] == ["BODY", "BR", "BODY"]
        assert [c.raw for c in children if c.label == "BODY"] == ["line one", "line two"]

    def test_ref(self):
        ce = classify(SHAPE_HTML_TAG, "<ref>note text</ref>")
        assert ce.label == "REF"

    def test_score(self):
        ce = classify(SHAPE_HTML_TAG, "<score>{ \\new Staff }</score>")
        assert ce.label == "SCORE"

    def test_html_table(self):
        # Every table — `<table>` or `{|`, grid or single-column — collapses
        # to the one TABLE label and the one unified table producer.
        ce = classify(SHAPE_HTML_TAG,
                       "<table><tr><td>a</td><td>b</td></tr></table>")
        assert ce.label == "TABLE"

    def test_html_table_single_column(self):
        # One cell per row is still just a TABLE (the producer decomposes it).
        ce = classify(SHAPE_HTML_TAG,
                       "<table><tr><td>a</td></tr><tr><td>b</td></tr></table>")
        assert ce.label == "TABLE"

    def test_hieroglyph_tag(self):
        ce = classify(SHAPE_HTML_TAG, "<hiero>A1-B2</hiero>")
        assert ce.label == "HIEROGLYPH"

    def test_image(self):
        # A bare image is a LEAF — IMAGE.
        ce = classify(SHAPE_DOUBLE_BRACKET, "[[File:Foo.jpg|200px]]")
        assert ce.label == "IMAGE"

    def test_captioned_image(self):
        # Every image spelling is IMAGE — bare or captioned.  The ONE producer
        # detects a thumb/frame caption and lays it out; classify stays structural.
        ce = classify(SHAPE_DOUBLE_BRACKET,
                       "[[File:Foo.jpg|thumb|caption]]")
        assert ce.label == "IMAGE"

    def test_image_float(self):
        # `{{img float}}` is a captioned figure — also just IMAGE now.
        ce = classify(SHAPE_DOUBLE_BRACE,
                       "{{img float|file=X.jpg|width=200}}")
        assert ce.label == "IMAGE"

    def test_hieroglyph_template(self):
        ce = classify(SHAPE_DOUBLE_BRACE, "{{hieroglyph|A1-B2}}")
        assert ce.label == "HIEROGLYPH"

    def test_ref_self(self):
        ce = classify(SHAPE_HTML_SELF_CLOSING, "<ref name=foo/>")
        assert ce.label == "REF_SELF"
        assert ce.inner_text == ""
        assert ce.inner_registry == {}

    def test_paired_wrapper_chart2(self):
        # The chart2 family of the merged PAIRED_WRAPPER shape — the classifier
        # routes by name to CHART2 (a leaf: empty inner, no children).
        ce = classify(SHAPE_PAIRED_WRAPPER,
                       "{{chart2/start}}A{{chart2/end}}")
        assert ce.label == "CHART2"
        assert ce.inner_text == ""
        assert ce.inner_registry == {}

    def test_paired_wrapper_center(self):
        # The centring family of the merged PAIRED_WRAPPER shape — the classifier
        # routes by name to CENTER; strip_outer peels the `{{c/s}}…{{c/e}}` wrapper.
        # CENTER is a COMPOSITE (un-leafed): its inner classifies into the tree, so
        # `inner_text` is a placeholder and a child holds the content.
        ce = classify(SHAPE_PAIRED_WRAPPER, "{{c/s}}centred{{c/e}}")
        assert ce.label == "CENTER"
        assert ce.inner_text in ce.inner_registry
        assert ce.inner_registry[ce.inner_text].inner_text == "centred"

    def test_outline(self):
        raw = "; head : desc\n; head2 : desc2"
        ce = classify(SHAPE_OUTLINE, raw)
        assert ce.label == "OUTLINE"


class TestBacklogFamilyRoutes:
    """The families routed by the generic-`{{…}}` flip (Step 2): every corpus
    template must route — the classifier `raise`s on a genuine unknown, which is
    the permanent guard.  One representative per family."""

    def _label(self, raw):
        return classify(SHAPE_DOUBLE_BRACE, raw).label

    def test_spacer_word_named(self):
        assert self._label("{{spaces|10}}") == "SPACER"
        assert self._label("{{nop}}") == "SPACER"
        assert self._label("{{ae}}") == "SPACER"

    def test_frame_dissolved_to_real_homes(self):
        # The old FRAME catch-all is dissolved — each shape routes to its family.
        assert self._label("{{ti|1em|indented}}") == "PARAM"
        assert self._label("{{margin-left|3.2em|text}}") == "PARAM"
        assert self._label("{{familytree|border=0| | |ALD|ALD=John}}") == "CHART2"
        assert self._label("{{vrl|Label}}") == "CONTENT_EXTRACT"
        assert self._label("{{wdl|Q123|Display}}") == "CONTENT_EXTRACT"
        assert self._label("{{nodent|content}}") == "CONTENT_EXTRACT"

    def test_hanging_indent_routes(self):
        # `{{hi}}` / `{{hanging indent}}` / `{{outdent}}` are synonyms → ONE owner,
        # RENDERED at the source's stated (or 2em-default) width, not dropped.
        assert self._label("{{hi|3.5em|1867. Paris. 1 Kolisch.}}") == "HANGING_INDENT"
        assert self._label("{{hi|content, no width given}}") == "HANGING_INDENT"
        assert self._label("{{hanging indent|caption}}") == "HANGING_INDENT"
        assert self._label("{{outdent|some text}}") == "HANGING_INDENT"

    def test_frame_control_marker_is_spacer(self):
        assert self._label("{{multicol-break}}") == "SPACER"
        assert self._label("{{col-begin}}") == "SPACER"

    def test_refs_routes_to_empty(self):
        # Footnote-list emitters → REFS (footnotes render inline, so the emitter is
        # empty).  The former chrome-empty names are gone — front-matter is excluded
        # at the gather, furniture stripped in preprocess, content-bearers lifted.
        assert self._label("{{smallrefs|90%}}") == "REFS"
        assert self._label("{{reflist}}") == "REFS"

    def test_dissolved_chrome_routes_to_real_homes(self):
        # The page-split words and content templates that used to be dumped into
        # `_CHROME_EMPTY_NAMES → empty` now reach their own producers.
        assert self._label("{{hws|frag|WORD}}") == "SPLIT_WORD"
        assert self._label("{{lps|hws=A|hwe=B}}") == "SPLIT_WORD"
        assert self._label("{{lpe|hws=A|hwe=B}}") == "SPLIT_WORD"
        assert self._label("{{suspect|on}}") == "CONTENT_EXTRACT"
        assert self._label("{{main other|x|}}") == "MAIN_OTHER"

    def test_missing_stub(self):
        assert self._label("{{missing table}}") == "MISSING"
        assert self._label("{{formula missing}}") == "MISSING"

    def test_over_fraction_bare_form(self):
        assert self._label("{{1\\over 2}}") == "FRACTION"
        assert self._label("{{\\kappa\\over\\kappa'}}") == "FRACTION"

    def test_over_in_named_template_content_routes_by_name(self):
        # A real `{{name|…}}` whose CONTENT carries `\over`/`\overline` routes by
        # its NAME, NOT to FRACTION (the bare-`\over` route must not steal it).
        assert self._label(
            "{{ne||<math>\\tfrac12\\overline{mu^2}</math>|(7)}}") == "MATH_EQUATION"

    def test_size_keyword_wrapper_routes_param(self):
        # `{{size|xl|X}}` — the keyword size is a font-size styler (PARAM); xl→x-large.
        assert self._label("{{size|xl|CAPE COLONY}}") == "PARAM"

    def test_bare_styler_form_routes_strip(self):
        # `{{sc}}` (no pipe) — a registered styler still routes via name membership.
        assert self._label("{{sc}}") == "STRIP"

    def test_lb_pound_both_spellings(self):
        assert self._label("{{lb|10}}") == "LB"
        assert self._label("{{lb-|10}}") == "LB"

    def test_unknown_template_leaks(self):
        # An unrouted template LEAKS (renders raw, surfaced by the leak audit); it
        # does NOT crash the build — "a miss must be loud AND recoverable, which a
        # raise is not" (_classifier's DOUBLE_BRACE_LEAK, commit 033858e).
        assert self._label("{{utterly unknown template name|x}}") == "DOUBLE_BRACE_LEAK"


class TestNestedClassification:
    def test_poem_with_ref(self):
        ce = classify(SHAPE_HTML_TAG,
                       "<poem>verse <ref>note</ref> end</poem>")
        assert ce.label == "POEM"
        # Recursion now registers the surrounding prose as BODY children too:
        # BODY("verse ") + REF + BODY(" end").  Exactly one REF among them.
        labels = [c.label for c in ce.inner_registry.values()]
        assert labels == ["BODY", "REF", "BODY"]
        ref_ce = next(c for c in ce.inner_registry.values() if c.label == "REF")
        assert ref_ce.raw == "<ref>note</ref>"

    def test_simple_data_table(self):
        # A COMPOSITE: TABLE → ROW → TD, recognized at classify time (the cell
        # tree is real, not a produce-time re-walk).
        ce = classify(SHAPE_BRACE_PIPE, "{|\n|cell\n|}")
        assert ce.label == "TABLE"
        assert "ROW" in _labels(ce) and "TD" in _labels(ce)
        assert "cell" in _flat_text(ce)

    def test_table_with_one_math_child_is_data(self):
        # One math child isn't math-dominant (predicate needs ≥2) → plain TABLE.
        # The composite recurses the cell to a MATH leaf at classify time; the
        # `<math>` tag is consumed (its inner "x" survives).
        ce = classify(SHAPE_BRACE_PIPE, "{|\n|<math>x</math>\n|}")
        assert ce.label == "TABLE"
        assert _labels(ce).count("MATH") == 1
        assert "x" in _flat_text(ce)

    def test_math_cell_wikitable_is_plain_table(self):
        # `<math>` is a self-labeling leaf, so a wikitable of math cells is
        # just a TABLE — no special math-layout classification (that machinery
        # was collapsed away).  Each cell recurses to a MATH leaf in the tree.
        raw = "{|\n|<math>x</math>\n|<math>y</math>\n|}"
        ce = classify(SHAPE_BRACE_PIPE, raw)
        assert ce.label == "TABLE"
        assert _labels(ce).count("MATH") == 2
        assert "x" in _flat_text(ce) and "y" in _flat_text(ce)

    def test_captioned_figure_image_with_caption_row(self):
        # A single-image wikitable with a caption row is NOT a figure family —
        # it's a plain TABLE whose cells recurse (image leaf + caption prose).
        # The pairability taxonomy (CAPTIONED_FIGURE/LEGENDED/…) was de-recognized:
        # a plate is processed exactly like an article, fenced only for page seams.
        raw = (
            "{|\n"
            "|[[File:Foo.jpg]]\n"
            "|-\n"
            "|Fig. 1. A descriptive caption.\n"
            "|}"
        )
        ce = classify(SHAPE_BRACE_PIPE, raw)
        assert ce.label == "TABLE"


class TestClassifyArticle:
    def test_multiple_top_level_elements(self):
        # SHAPE_BODY (task #14) makes residual prose its own element,
        # so plain-prose spans appear in the registry alongside MATH/
        # IMAGE.  The test asserts (a) the non-prose extracts come
        # through with the right labels, (b) every registered
        # placeholder appears in the placeholderized body.
        text = ("Body text <math>x</math> more text "
                "[[File:F.jpg]] end")
        placeholderized, registry = classify_article(text)
        labels = sorted(ce.label for ce in registry.values())
        # MATH + IMAGE + the BODY spans between/around them
        assert "MATH" in labels
        assert "IMAGE" in labels
        assert labels.count("BODY") >= 1
        for ph in registry:
            assert ph in placeholderized

    def test_empty_text(self):
        # Plain prose is now itself a BODY element (task #14:
        # SHAPE_BODY).  Pre-SHAPE_BODY this returned an empty registry;
        # the new architecture's owner-of-output principle is that EVERY
        # span of source maps to one producer, including residual prose.
        placeholderized, registry = classify_article("just plain prose")
        assert len(registry) == 1
        (ce,) = registry.values()
        assert ce.label == "BODY"
        assert ce.raw == "just plain prose"
        # The single placeholder replaces the original text.
        (ph,) = registry.keys()
        assert placeholderized == ph
