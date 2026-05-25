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
    SHAPE_BRACE_PIPE,
    SHAPE_CHART2,
    SHAPE_DOUBLE_BRACE,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_FIGURE,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_HTML_TAG,
    SHAPE_OUTLINE,
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


_HTML_TAG_LABEL: dict[str, str] = {
    "math":  "MATH",
    "poem":  "POEM",
    "ref":   "REF",
    "score": "SCORE",
    "table": "HTML_TABLE",
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
    # Today only `<ref name=X/>` is extracted as HTML_SELF_CLOSING.
    return "REF_SELF"


def _derive_double_bracket_label(raw: str) -> str:
    m = _BRACKET_PREFIX_RE.match(raw)
    if m and m.group(1).lower() in {"file", "image"}:
        return "IMAGE"
    raise ValueError(f"Unknown DOUBLE_BRACKET prefix: {raw[:40]!r}")


# IMAGE's "raw" extends past the `]]` to include an optional trailing
# caption block (today's IMAGE extractor regex captures `[[File:…]]`
# PLUS a following caption line if present).  Producer consumes the
# trailing caption via a `|EXTCAP:` tail on `inner_text` — so the
# classifier post-processes inner_text for IMAGE specifically to
# strip the `File:`/`Image:` prefix AND fold the trailing caption in.
_IMAGE_RAW_RE = re.compile(
    r"\[\[(?:File|Image):(.+)\]\](?:\s*\n\n?(.+))?$",
    re.IGNORECASE | re.DOTALL,
)


def _image_inner_with_extcap(raw: str) -> str:
    m = _IMAGE_RAW_RE.match(raw)
    if not m:
        return raw
    inner = m.group(1)
    ext_caption = m.group(2)
    if ext_caption:
        inner = inner + "|EXTCAP:" + ext_caption
    return inner


def _derive_double_brace_label(raw: str) -> str:
    m = _TEMPLATE_NAME_RE.match(raw)
    if not m:
        raise ValueError(
            f"DOUBLE_BRACE raw doesn't open with a template: {raw[:40]!r}")
    name = m.group(1).lower()
    # The IMAGE_FLOAT walker regex matches `{{(?:img float|figure|FI)\|…}}`.
    # `img float` tokenizes here as "img" (whitespace stop in the
    # template-name pattern).  Figure-equivalent templates with a
    # full-name token go through the same producer.
    if name in {"img", "figure", "fi"}:
        return "IMAGE_FLOAT"
    if name == "hieroglyph":
        return "HIEROGLYPH"
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
    return _classify_table(raw, inner_text, legacy)


# ── `<table>` flip (Phase 2 step 3) ───────────────────────────────────
#
# Route a `<table>` through the SAME shape classifiers a `{|` wikitable gets
# (`_classify_table`), so disguised non-tables (figures) leave the table path
# instead of being flatly labelled HTML_TABLE.  But two constraints keep the
# blast radius to exactly the non-tables we can handle:
#   * a genuine `<table>` grid must stay on the span-preserving
#     `_process_html_table` producer — the DATA_TABLE catch-all's producer
#     `_process_table` is wiki-only and would break it; and
#   * the not-yet-`<table>`-ready non-table producers (math/chem/verse/
#     single-column still parse wiki `|`/`|-`) would also break on `<table>`.
# So ONLY the figure-family labels — whose producers were made syntax-neutral
# via `_table_grid` in step 2 and verified to render `<table>` figures content-
# preserved — leave the table path; every other label falls back to HTML_TABLE,
# byte-identical to today.  Grow `_HTML_TABLE_ROUTE_AWAY` as each producer
# family is converted (math/chem/verse next).
_HTML_TABLE_ROUTE_AWAY: frozenset[str] = frozenset({
    "CAPTIONED_FIGURE", "CAPTIONED_FIGURE_INLINE", "UNPAIRED_FIGURE_GROUP",
    "LEGENDED_FIGURE", "LEGENDED_FIGURE_CHILD",
})


def _classify_html_table(
    raw: str,
    inner_text: str,
    inner_registry: dict[str, ClassifiedElement],
) -> str:
    """Classify a `<table>` element via the shared table classifier, letting
    only `<table>`-ready figure shapes leave the table path; genuine grids and
    not-yet-ready shapes stay HTML_TABLE."""
    from britannica.pipeline.stages.elements import _classify_table
    legacy = _to_legacy_registry(inner_registry)
    label = _classify_table(raw, inner_text, legacy)
    return label if label in _HTML_TABLE_ROUTE_AWAY else "HTML_TABLE"


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
    if shape == SHAPE_DOUBLE_BRACE:
        return _derive_double_brace_label(raw)
    if shape == SHAPE_CHART2:
        return "CHART2"
    if shape == SHAPE_OUTLINE:
        return "OUTLINE"
    if shape == SHAPE_FIGURE:
        return "FIGURE"
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

    # Per-label inner_text post-processing.  Most labels accept the
    # shape-uniform strip_outer result; IMAGE is the exception —
    # its raw bytes may extend past the `]]` to include a trailing
    # caption block, and its producer expects the `File:`/`Image:`
    # prefix stripped with an `|EXTCAP:` tail folded in.
    if label == "IMAGE":
        inner_text = _image_inner_with_extcap(raw)

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
    """
    placeholderized_text, extracts = walk(text, _allow_figure=_allow_figure)
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
    from britannica.pipeline.stages.elements._tables import (
        _inline_nested_table_markers_in_htmltable_blocks,
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
        if ce.inner_registry:
            for _ in range(5):
                changed = False
                for child_ph, child_ce in ce.inner_registry.items():
                    if child_ce.marker and child_ph in marker:
                        marker = marker.replace(child_ph, child_ce.marker)
                        changed = True
                if not changed:
                    break

        # Post-substitution cleanup: a child wiki-table's
        # `{{TABLE:…}TABLE}` marker embedded inside an HTMLTABLE
        # cell leaks as cell text unless converted to inline
        # ``<table>`` HTML (ORNITHOLOGY taxonomic alignments,
        # EOCENE etymology glossary inside a `<ref>`).
        if "«HTMLTABLE:" in marker and "{{TABLE:" in marker:
            marker = _inline_nested_table_markers_in_htmltable_blocks(marker)

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
