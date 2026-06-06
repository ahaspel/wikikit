"""Recursive classifier — turns walker output into a labeled tree
of :class:`ClassifiedElement` records.

This is the new architecture's entry point, dormant until Phase C of
the walker / classifier / producer refactor wires it into
``_transform_text_v2``.  Today it coexists with the legacy three-pass
``_walk_recursive`` / ``_classify_recursive`` / ``_produce_recursive``
pipeline in ``__init__.py``.

The flow (mutually recursive, classifier-driven):

    walker → (shape, raw_bytes)
         │
         ▼
    classifier strips outer delimiters → inner_bytes
         │
         ▼
    classifier asks walker for one-level extracts of inner_bytes
         │
         ▼
    for each (placeholder, child_shape, child_raw):
        classifier recurses → ClassifiedElement
         │
         ▼
    classifier derives own label from (shape, raw, inner_registry)
         │
         ▼
    returns ClassifiedElement(label, raw, inner_text, inner_registry)

Walker output: ``(text_with_placeholders, [(ph, shape, raw), ...])``.
For Phase B-1 the walker is today's :func:`extract`, with a thin
name-to-shape adapter; Phase B-2 swaps in a shape-emitting walker
directly.
"""

from __future__ import annotations

import re

from britannica.pipeline.stages.elements._registry import (
    ClassifiedElement,
    ElementRegistry,
    TABLE_LABELS,
)
from britannica.pipeline.stages.elements._shapes import (
    LEAF_SHAPES,
    SHAPE_BODY,
    SHAPE_BRACE_PIPE,
    SHAPE_CENTER,
    SHAPE_CHART2,
    SHAPE_DOUBLE_BRACE,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_FIGURE,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_HTML_TAG,
    SHAPE_INLINE_IMAGE,
    SHAPE_MIRROR_GLYPH,
    SHAPE_ORDERED_LIST,
    SHAPE_OUTLINE,
    SHAPE_SECTION,
    SHAPE_STYLED,
    strip_outer,
)
from britannica.pipeline.stages.elements._walker import walk


# ── Per-shape label derivers ──────────────────────────────────────────
#
# Each function maps (raw bytes [+ inner, + classified children]) to a
# label string.  Atomic shapes derive label from the opening
# identifier in the raw bytes alone.  Composite BRACE_PIPE wikitables
# need their children's labels — composites finalise after the inner
# recursion has unwound.

_HTML_TAG_NAME_RE = re.compile(r"^<\s*([A-Za-z][A-Za-z0-9]*)", re.IGNORECASE)
_TEMPLATE_NAME_RE = re.compile(r"^\{\{\s*([^|{}<>\n\s]+)")
_BRACKET_PREFIX_RE = re.compile(r"^\[\[\s*([A-Za-z]+)\s*:", re.IGNORECASE)
# The fraction family — matched on the RAW opener (not the first token) so that
# multi-word names like `EB1911 sfrac` route here rather than to the `eb1911`
# CONTRIBUTOR_FOOTER case.  Mirrors the walker's `_FRACTION_RE`.
_FRAC_LABEL_RE = re.compile(
    r"\{\{\s*(?:sfrac\s+nobar|EB1911\s+sfrac|EB1911\s+tfrac"
    r"|EB¹⁹¹¹\s+sfrac|EB¹⁹¹¹\s+tfrac|EB₁₉₁₁\s+ₜfᵣₐc"
    r"|sfracN|sfrac|mfrac|frac|over|binom)\s*\|", re.IGNORECASE)


_HTML_TAG_LABEL: dict[str, str] = {
    "math":  "MATH",
    "poem":  "POEM",
    "ref":   "REF",
    "score": "SCORE",
    "table": "TABLE",
    "hiero": "HIEROGLYPH",
}


def _derive_html_tag_label(raw: str) -> str:
    m = _HTML_TAG_NAME_RE.match(raw)
    if not m:
        raise ValueError(
            f"HTML_TAG raw doesn't open with a tag: {raw[:40]!r}")
    tag = m.group(1).lower()
    if tag not in _HTML_TAG_LABEL:
        raise ValueError(
            f"Unknown HTML tag for HTML_TAG shape: {tag!r}")
    return _HTML_TAG_LABEL[tag]


def _derive_html_self_closing_label(raw: str) -> str:
    if raw[:13].lower().startswith("<pagequality"):
        return "PAGEQUALITY"
    return "REF_SELF"


def _derive_double_bracket_label(raw: str) -> str:
    m = _BRACKET_PREFIX_RE.match(raw)
    if m and m.group(1).lower() in {"file", "image"}:
        return "IMAGE"
    raise ValueError(f"Unknown DOUBLE_BRACKET prefix: {raw[:40]!r}")


def _derive_double_brace_label(raw: str, inner_text: str = "") -> str:
    # Standalone `{{Css image crop|…}}` — routed to the faithful figure
    # producer, which treats the crop as just another image: a stateless
    # geometry-hash filename (`djvu_crop_filename`) → `{{IMG:…}}`, the crop
    # already rendered to disk under that name.
    if re.match(r"\{\{\s*Css image crop\b", raw, re.IGNORECASE):
        return "DJVU_CROP"
    # `{{raw image|…}}` — full-page DjVu scan or plain figure file
    # (`_process_raw_image` normalizes a DjVu page-ref + folds a `{{c|cap}}`).
    if re.match(r"\{\{\s*raw\s+image\b", raw, re.IGNORECASE):
        return "RAW_IMAGE"
    # `{{Plain image with caption|image=…|caption=…}}` — named-parameter figure
    # macro (`_process_plain_image` builds the {{IMG:…}} from the named params).
    if re.match(r"\{\{\s*plain image with caption\b", raw, re.IGNORECASE):
        return "PLAIN_IMAGE"
    # `{{EB1911 article link|…}}` — cross-reference link; matched on the raw opener
    # (like the image cases) since the first-token name is the ambiguous "eb1911".
    if re.match(r"\{\{\s*EB1911\s+article\s+link\b", raw, re.IGNORECASE):
        return "EB1911_ARTICLE_LINK"
    # Target-first link siblings — lkpl / 1911link / EB1911 link (NOT "article link").
    if re.match(r"\{\{\s*(?:(?:EB1911|DNB)\s+lkpl|1911link|11link|EB1911\s+link)\b",
                raw, re.IGNORECASE):
        return "TARGET_FIRST_LINK"
    # Spacer / rule / char-escape leaves — em/gap/clear/anchor/ditto/dhr/rule/bar/shy
    # and the literal-char escapes ({{=}}, {{(}}, {{...}}, …).
    if re.match(r"\{\{\s*(?:(?:em|gap|clear|anchor|ditto|dhr|rule|bar|shy)\b"
                r"|=|\(|\)|'|!|\*\*\*|\*|–|\.\.\.|…)", raw, re.IGNORECASE):
        return "SPACER"
    # Content extractors — display arg is the content; producer unwraps + recurses it.
    if re.match(r"\{\{\s*(?:tooltip|abbr|lang|sic|fqm|drop\s?initial)\b", raw, re.IGNORECASE):
        return "CONTENT_EXTRACT"
    m = _TEMPLATE_NAME_RE.match(raw)
    if not m:
        raise ValueError(
            f"DOUBLE_BRACE raw doesn't open with a template: {raw[:40]!r}")
    name = m.group(1).lower()
    # Fraction family (sfrac/mfrac/frac/over/EB1911 tfrac/…) — a styler lifted
    # as an element; the FRACTION producer recurses its slots.  Matched on the
    # raw opener (so `{{EB1911 sfrac|…}}` routes here, not to the `eb1911`
    # CONTRIBUTOR_FOOTER case below).
    if _FRAC_LABEL_RE.match(raw):
        return "FRACTION"
    # `{{lb-|N}}` / `{{lb-}}` — pound-weight glyph leaf (promoted out of the
    # body-text `_convert_lb_dash` re.sub, which only fired inside `_apply_markup`).
    if name == "lb-":
        return "LB"
    # `{{sub|x}}` / `{{sup|x}}` — sub/superscript typography (out of body-text's
    # `_convert_sub_sup`); the producer recurses the slot + Unicode-translates.
    if name in {"sub", "sup"}:
        return "SUBSUP"
    # The IMAGE_FLOAT walker regex matches `{{(?:img float|figure|FI)\|…}}`.
    # `img float` tokenizes here as "img" (whitespace stop in the
    # template-name pattern).  Figure-equivalent templates with a
    # full-name token go through the same producer.
    if name in {"img", "figure", "fi"}:
        return "IMAGE_FLOAT"
    if name == "hieroglyph":
        return "HIEROGLYPH"
    # `{{ppoem|…}}` — preformatted-poem template; the verse analog of `<poem>`.
    # `_process_ppoem` strips the stanza-frame control params and emits VERSE.
    if name == "ppoem":
        return "PPOEM"
    # `{{dual line|A|B}}` — pure layout primitive (two-line stack).
    # CONTENT-AWARE sub-classification: chem-shaped (element-formula
    # clusters) → CHEM_DUAL, owned by chem; math-shaped (italic
    # variables, sub/sup on non-element content) → MATH_DUAL, owned by
    # math; else plain DUAL_LINE (table headers, hyphenations, figure-
    # caption splits).  Chem first because chem signature can overlap
    # math markers (sub/sup) — chem catches the chem case before the
    # looser math predicate sees it.
    if name == "dual":
        from britannica.pipeline.stages.elements._chem import (
            is_chem_dual_line)
        from britannica.pipeline.stages.elements._math import (
            is_math_dual_line)
        if is_chem_dual_line(inner_text):
            return "CHEM_DUAL"
        if is_math_dual_line(inner_text):
            return "MATH_DUAL"
        return "DUAL_LINE"
    # Labeled-display-equation templates — declared as math by their
    # template name.  All three labels dispatch to one producer
    # (`_process_math_equation`) which selects the per-template arg
    # convention and emits the shared `«EQN:LABEL»content«/EQN»`
    # marker with `\n\n` paragraph margins.
    if name == "equation":
        return "MATH_EQUATION"
    if name == "mathform1":
        return "MATH_FORMULA_LABELED"
    if name == "ne":
        return "MATH_NE"
    # (The `{{EB1911 footer initials}}` footer is no longer a walker element — it's a
    # FIELD, cut upstream by `strip_attributions` before the walker ever runs, so it
    # never reaches this dispatch.)
    # Inline typography (sfrac/frac/mfrac/over/sfracN/EB1911 variants/
    # sub/sup) stays in body-text — the source doesn't declare those
    # as math via the template name; they're rendered as typography
    # whose output flows back into prose.  Walker doesn't lift them.
    raise ValueError(f"Unknown DOUBLE_BRACE template: {name!r}")


# ── BRACE_PIPE composite classifier ───────────────────────────────────
#
# Delegates to today's `_classify_table` predicates via a legacy-shaped
# ElementRegistry view.  Transitional: when the predicates are
# rewritten to take `dict[str, ClassifiedElement]` directly the bridge
# disappears.


def _label_to_legacy_name(label: str) -> str:
    """Map a classifier label back to today's walker source-type name.

    Identity for non-table labels (the walker name equals the
    classifier label for MATH / POEM / IMAGE / etc.).  TABLE_LABELS
    collapse to 'TABLE' — the walker writes 'TABLE' for every
    brace-pipe element regardless of the sub-classification the
    classifier eventually assigns.
    """
    if label in TABLE_LABELS:
        return "TABLE"
    return label


def _to_legacy_registry(
    inner_registry: dict[str, ClassifiedElement]
) -> ElementRegistry:
    """Build a legacy `ElementRegistry` view over a
    `dict[str, ClassifiedElement]`.  Lets today's table predicates
    and producer handlers run unchanged against the new classified
    tree.

    Populates ``markers`` from ``ce.marker`` where available — empty
    during classification, populated bottom-up during the producer
    pass — so producer handlers that inspect inner markers see the
    right state.
    """
    reg = ElementRegistry()
    for ph, ce in inner_registry.items():
        legacy_name = _label_to_legacy_name(ce.label)
        reg.elements[ph] = (legacy_name, ce.raw)
        reg.labels[ph] = ce.label
        reg.inners[ph] = ce.inner_text
        reg.inner_registries[ph] = _to_legacy_registry(ce.inner_registry)
        if ce.marker:
            reg.markers[ph] = ce.marker
    return reg


def _classify_brace_pipe(
    raw: str,
    inner_text: str,
    inner_registry: dict[str, ClassifiedElement],
) -> str:
    """Run today's wikitable predicates over the classified inner
    registry.  Returns one of `TABLE_LABELS`.
    """
    # Late import: `_classify_table` is defined in `__init__.py`
    # alongside the legacy three-pass pipeline.
    from britannica.pipeline.stages.elements import _classify_table
    legacy = _to_legacy_registry(inner_registry)
    label = _classify_table(raw, inner_text, legacy)
    # Figure `{|`-tables are just tables — collapse the dead pairability labels to
    # TABLE (same producer, no shadow recognizer).
    return "TABLE" if label in _FIGURE_TABLE_LABELS else label


# ── `<table>` routing ─────────────────────────────────────────────────
#
# A `<table>` is classified by the SAME `_classify_table` ladder a `{|`
# wikitable gets, so a disguised non-table (figure/math/chem) leaves the
# table path onto its own producer.  Everything the ladder calls a table is
# just "TABLE": the unified table engine (`_process_table_unified`) is
# syntax-neutral — it renders `<table>` and `{|` alike by opener — so the
# else-branch IS "TABLE", not a separate HTML_TABLE label.  Only labels whose
# own producer is verified `<table>`-ready (figure/math/chem) route away; any
# other label (incl. a `<table>` the ladder calls LAYOUT_WRAPPER / DJVU_CROP)
# stays on the table engine — exactly as before the collapse, when the
# else-branch was HTML_TABLE → the same unified engine.
# A figure `{|`-table is NOT a separate family: a plate is processed exactly like
# an article (image leaves + legend prose in a table), fenced off only so the
# page-seam joiner can treat it as one unit — which the `{|` bounding already gives.
# So every figure label collapses to TABLE; the article walker's table path
# (`_process_table_unified`) recurses it identically.  These labels are now dead
# (`_classify_icl_shape` / `_layout` / `_figure_decompose` are the shadow recognizer
# left to delete).
_FIGURE_TABLE_LABELS: frozenset[str] = frozenset({
    "CAPTIONED_FIGURE", "CAPTIONED_FIGURE_INLINE", "UNPAIRED_FIGURE_GROUP",
    "LEGENDED_FIGURE", "LEGENDED_FIGURE_BESIDE", "LEGENDED_FIGURE_CHILD",
    "FIGURE_GROUP", "LAYOUT_WRAPPER",
})
# Only CHEM keeps its own `<table>`-aware producer (`_split_html_chem_row`); MATH
# isn't routed away (`<math>` is a leaf, so a math-cell table is just a TABLE).
_HTML_TABLE_ROUTE_AWAY: frozenset[str] = frozenset({"CHEMISTRY_LAYOUT"})


def _classify_html_table(
    raw: str,
    inner_text: str,
    inner_registry: dict[str, ClassifiedElement],
) -> str:
    """Classify a `<table>` element via the shared table classifier.  A
    non-table producer family (figure/math/chem) keeps its own label and
    leaves the table path; every genuine table is "TABLE" and renders through
    the unified, syntax-neutral table engine."""
    from britannica.pipeline.stages.elements import _classify_table
    legacy = _to_legacy_registry(inner_registry)
    label = _classify_table(raw, inner_text, legacy)
    return label if label in _HTML_TABLE_ROUTE_AWAY else "TABLE"


# ── Top-level label dispatcher ────────────────────────────────────────


def _derive_label(
    shape: str,
    raw: str,
    inner_text: str,
    inner_registry: dict[str, ClassifiedElement],
) -> str:
    if shape == SHAPE_BRACE_PIPE:
        return _classify_brace_pipe(raw, inner_text, inner_registry)
    if shape == SHAPE_HTML_TAG:
        m = _HTML_TAG_NAME_RE.match(raw)
        if m and m.group(1).lower() == "table":
            return _classify_html_table(raw, inner_text, inner_registry)
        return _derive_html_tag_label(raw)
    if shape == SHAPE_HTML_SELF_CLOSING:
        return _derive_html_self_closing_label(raw)
    if shape == SHAPE_DOUBLE_BRACKET:
        return _derive_double_bracket_label(raw)
    if shape == SHAPE_INLINE_IMAGE:
        return "INLINE_IMAGE"
    if shape == SHAPE_DOUBLE_BRACE:
        return _derive_double_brace_label(raw, inner_text)
    if shape == SHAPE_CHART2:
        return "CHART2"
    if shape == SHAPE_ORDERED_LIST:
        return "ORDERED_LIST"
    if shape == SHAPE_OUTLINE:
        return "OUTLINE"
    if shape == SHAPE_FIGURE:
        return "FIGURE"
    if shape == SHAPE_SECTION:
        return "SECTION"
    if shape == SHAPE_BODY:
        return "BODY"
    if shape == SHAPE_MIRROR_GLYPH:
        return "MIRROR_GLYPH"
    if shape == SHAPE_CENTER:
        return "CENTER"  # only the c-family reaches SHAPE_CENTER (walker)
    if shape == SHAPE_STYLED:
        return "STYLED"  # styled <div>/<p>/<span> (walker); producer derives CSS
    raise ValueError(f"Unknown shape: {shape!r}")


# ── Recursive classifier ──────────────────────────────────────────────


def classify(
    shape: str, raw: str, _allow_outline: bool = True
) -> ClassifiedElement:
    """Classify one element.

    Strips the shape's outer delimiters, asks the walker for the
    next-level extracts, recursively classifies each child, then
    derives the label for this element from the assembled
    inner_registry.
    """
    if shape in LEAF_SHAPES:
        # Leaf shapes own their entire payload — the producer reads
        # `raw` (CHART2, REF_SELF) or `inner_text` (OUTLINE) directly
        # and does whatever internal parsing it needs.  We still call
        # `strip_outer` so `inner_text` reflects each leaf's own
        # contract: CHART2 / REF_SELF return "", OUTLINE returns the
        # indented-line ladder unchanged.
        inner_text = strip_outer(shape, raw)
        inner_registry: dict[str, ClassifiedElement] = {}
    else:
        peeled = strip_outer(shape, raw)
        # `_allow_outline=False` on the next descent if we're inside
        # an OUTLINE — prevents the outline extractor from
        # re-triggering on its own bytes (today's `recurse_safe`).
        next_allow_outline = _allow_outline and shape != SHAPE_OUTLINE
        # Figures are body-level only — never recognize one inside an
        # already-extracted element (incl. the FIGURE producer's own
        # re-processing of its span, which would recurse forever).
        inner_text, extracts = walk(
            peeled, _allow_outline=next_allow_outline, _allow_figure=False)
        inner_registry = {}
        for ph, child_shape, child_raw in extracts:
            inner_registry[ph] = classify(
                child_shape, child_raw,
                _allow_outline=next_allow_outline,
            )
    label = _derive_label(shape, raw, inner_text, inner_registry)

    # NOTE: the walker is conservative — what comes in goes out unchewed.
    # An IMAGE element therefore carries its raw `[[File:…]]` + trailing
    # caption span VERBATIM; the file-ref/caption split is the producer's
    # job (`_process_image_from_raw`), not a classify-time fold.  No
    # per-label inner_text rewriting happens here.

    return ClassifiedElement(
        label=label,
        raw=raw,
        inner_text=inner_text,
        inner_registry=inner_registry,
    )


def classify_article(
    text: str, _allow_figure: bool = True,
) -> tuple[str, dict[str, ClassifiedElement]]:
    """Top-level entry: classify every embedded element in an article
    body.

    Returns ``(placeholderized_text, top_level_registry)`` where
    ``top_level_registry`` is ``dict[placeholder, ClassifiedElement]``
    — one record per top-level placeholder, recursively populated.

    ``_allow_figure=False`` (used by the FIGURE producer's own re-process
    of its span) suppresses figure recognition so it doesn't re-recognize —
    and recurse on — its own span.

    Article-level walk runs with ``_wrap_body=True`` so residual prose
    between extracted elements becomes its own SHAPE_BODY extracts.
    After this call the placeholderized text contains only placeholders
    — every byte of the article is owned by some classified element.
    """
    placeholderized_text, extracts = walk(
        text, _allow_figure=_allow_figure, _wrap_body=True)
    registry: dict[str, ClassifiedElement] = {}
    for ph, shape, raw in extracts:
        registry[ph] = classify(shape, raw)
    return placeholderized_text, registry


# ── Producer pass over the classified tree ────────────────────────────


def produce_tree(
    tree: dict[str, ClassifiedElement], text_transform, context
) -> None:
    """Producer pass: bottom-up over the classified tree.

    For each element, recurses into children first, then runs the
    label's producer via the legacy ``ElementRegistry`` bridge.
    Substitutes child markers into the producer's output afterwards.
    Stores the final marker on ``ce.marker``.

    Mutates ``tree`` in place by populating each element's
    ``marker`` field.
    """
    from britannica.pipeline.stages.elements import (
        _PRODUCER_DISPATCH, _passthrough_inner,
    )

    for ph, ce in tree.items():
        # Recurse first — children's markers must be populated
        # before this element's producer runs and before
        # `_to_legacy_registry` is called below (which copies
        # children's markers into the registry view).
        if ce.inner_registry:
            produce_tree(ce.inner_registry, text_transform, context)

        legacy_inner_reg = (
            _to_legacy_registry(ce.inner_registry)
            if ce.inner_registry else None
        )
        handler = _PRODUCER_DISPATCH.get(ce.label, _passthrough_inner)
        marker = handler(
            ce.raw, ce.inner_text, text_transform, context, legacy_inner_reg)

        # Substitute child markers into the producer's output.
        # Multi-pass because a substituted child marker can itself
        # carry a placeholder for another child (cross-references).
        # Empty markers SUBSTITUTE NORMALLY — they're legitimate
        # producer output (`_process_ref_self` returns "" when a ref
        # name isn't resolved per its drop-silently contract; SECTION /
        # `<ref follow=>` continuations return "" too).
        # The previous `if child_ce.marker and ...` check short-
        # circuited on empty markers and leaked the placeholder
        # bytes (`\x03ELEM:N\x03`) into output — AFRICA's territorial
        # tables had ~17 such leaks from unresolved-`<ref name=X/>`
        # citation reuses inside `«HTMLTABLE:` blocks.
        if ce.inner_registry:
            for _ in range(5):
                changed = False
                for child_ph, child_ce in ce.inner_registry.items():
                    if child_ph in marker:
                        marker = marker.replace(child_ph, child_ce.marker)
                        changed = True
                if not changed:
                    break

        ce.marker = marker


def substitute_top_level_markers(
    text: str, tree: dict[str, ClassifiedElement]
) -> str:
    """Substitute top-level markers into the article body.

    Multi-pass to handle sibling cross-references — a top-level
    marker may carry a placeholder for another top-level marker.
    Mutates marker strings on `tree` entries during substitution.
    """
    for _ in range(3):
        changed = False
        for ph, ce in tree.items():
            marker = ce.marker
            if ph in text:
                text = text.replace(ph, marker)
                changed = True
            for other_ph, other_ce in tree.items():
                if other_ph != ph and ph in other_ce.marker:
                    other_ce.marker = other_ce.marker.replace(ph, marker)
        if not changed:
            break
    return text
