"""Extract, process, and reassemble embedded elements in wikitext.

Embedded elements (tables, images, footnotes, poems, math, scores)
are extracted from the raw wikitext into a registry, the remaining
text is transformed (bold, italic, links, etc.), then each element
is processed recursively and reassembled into the final text.

The key rule: extract outermost first, process innermost first.
Each element processor calls the same extract-process-reassemble
on its own content, so nesting (e.g. footnotes inside tables)
is handled naturally by recursion.
"""
from __future__ import annotations

import re
from dataclasses import replace as _dc_replace
from typing import Callable

from britannica.parsers import img_float as _img_float_parser
from britannica.pipeline.stages.elements._context import ElementContext
from britannica.pipeline.stages.elements._text import (
    _clean_text,
    _convert_inline_sub_sup,
    _strip_br,
)
from britannica.pipeline.stages.elements._ref import (
    _process_ref,
    _process_ref_self,
    _ref_attrs,
)
from britannica.pipeline.stages.elements._image import (
    _CSS_CROP_RE,
    _parse_crop_param,
    _process_chart2,
    _process_djvu_crop,
    _process_image,
    _process_image_float,
    _process_image_from_raw,
)
from britannica.pipeline.stages.elements._leaf import (
    _format_structural_formula,
    _is_structural_formula,
    _process_math,
    _process_poem,
    _process_score,
)
from britannica.pipeline.stages.elements._registry import (
    ElementRegistry,
)
from britannica.pipeline.stages.elements._math_layout import (
    _MATH_CELL_RE,
    _is_math_dominant_layout,
    _math_cell_to_latex,
    _math_table_kind,
    _parse_math_layout_cells,
    _process_equation_layout,
    _process_math_layout_table,
)
from britannica.pipeline.stages.elements._outline import (
    _OUTLINE_RANGE_HEADER_RE,
    _PAGE_MARKER_PREFIX_RE,
    _extract_outlines,
    _outline_indent_depth,
    _outline_is_bare_emphasis,
    _outline_is_list_shaped,
    _process_outline,
    _strip_page_marker_prefix,
)
from britannica.pipeline.stages.elements._layout import (
    _append_attribution,
    _ascii_fold_label,
    _clean_legend_text,
    _collect_attribution_rows,
    _emit_legend_chunk,
    _entries_look_like_legend,
    _extract_caption_from_colspan_row,
    _extract_poem_legend,
    _find_caption_row_idx,
    _format_legend_entries,
    _image_ph_filename,
    _is_layout_wrapper,
    _looks_like_caption,
    _parse_inline_legend_cell,
    _parse_multicol_legend_row,
    _parse_prose_legend_rows,
    _process_captioned_figure,
    _simple_table_text,
    _strip_cell_attributes,
    _try_image_layout_subclass,
    _unwrap_layout_table,
)
from britannica.pipeline.stages.elements._tables import (
    _extract_subtable_values,
    _has_chem_brackets,
    _is_html_illustration_wrapper,
    _process_chemistry_layout,
    _process_complex_table,
    _process_compound_table,
    _process_html_table,
    _process_table,
    _unwrap_html_illustration,
)


# Range-style header: `N–M.—TITLE.<br />` (GEM plate captions, similar
# numbered-section openers).  Acts as a top-level label (depth 0) for
# the indented numbered items that follow.  Required to be SHORT and
# end with `<br />` so prose paragraphs that happen to start with
# `1–5.` don't false-match.

def _is_compound_table(raw: str,
                       inner_registry: ElementRegistry | None) -> bool:
    """Detect data tables with nested sub-tables in cells.

    Two signals, both required:

      * Header carries a data-table indicator (`border=N`, `rules=`,
        or `class="…wikitable|tablecolhd|border…"`) on the `{|…`
        opener line.  Inspected from raw bytes — the walker doesn't
        classify table-opener attributes.

      * The table has nested wikitables as direct children.  Under
        bottom-up walking, any nested `{|…|}` blocks have already
        been extracted as TABLE-typed elements in
        ``inner_registry.elements`` — replacing an earlier raw-byte
        ``\\{\\|`` / ``\\|\\}`` depth scan.

    Returns True when both signals are present — the table is a real
    data table that needs parallel-sub-table-row processing in
    ``_process_compound_table``.
    """
    header = raw.split("\n", 1)[0]
    has_data = bool(
        re.search(r'border\s*=\s*"?[1-9]', header, re.IGNORECASE) or
        re.search(r'rules\s*=', header, re.IGNORECASE) or
        re.search(r'class\s*=\s*"[^"]*(?:wikitable|tablecolhd|border)',
                   header, re.IGNORECASE))
    if not has_data:
        return False
    if inner_registry is None:
        return False
    return any(t == "TABLE" for t, _ in inner_registry.elements.values())


# ── Processing ────────────────────────────────────────────────────────

# Producer handler signature.  All producers share this shape:
#   (raw, inner, text_transform, context, inner_registry) -> marker
# `raw` is the source bytes of the element (with delimiters); `inner`
# is the delimiter-stripped content with child placeholders.  Most
# producers use `inner` + the child registry; the leaf cases
# (CHART2, DJVU_CROP, COMPOUND_TABLE) use `raw` and ignore the rest.
_ElementHandler = Callable[
    [str, str, Callable, ElementContext, "ElementRegistry | None"], str]


def _passthrough_inner(raw, inner, text_transform, context,
                       inner_registry):
    return inner


# ── Producer dispatch ─────────────────────────────────────────────────
#
# Flat label → producer table.  Both wikitable sub-kinds (returned by
# `classify` for `element_type=TABLE`) and single-label element types
# (returned trivially) coexist in one dict.  Replaces the previous
# two-level dispatch (`_ELEMENT_HANDLERS` → `_dispatch_table` →
# `_TABLE_KIND_HANDLERS`).
_PRODUCER_DISPATCH: dict[str, _ElementHandler] = {
    # Wikitable sub-kinds.
    "MATH_LAYOUT_TOKENS": lambda raw, inner, tt, ctx, reg:
        _process_math_layout_table(raw),
    "MATH_LAYOUT_EQUATIONS": lambda raw, inner, tt, ctx, reg:
        _process_equation_layout(inner, tt),
    "LAYOUT_WRAPPER": lambda raw, inner, tt, ctx, reg:
        _unwrap_layout_table(inner, tt, reg),
    # CAPTIONED_FIGURE — single-image figure layout (one IMAGE child
    # in row 0, alone in cell, no data-table header signal).  Has
    # its own focused producer; falls back to `_unwrap_layout_table`
    # for shapes it doesn't yet handle (baked captions in image raw,
    # nested wikitables, POEM cells).
    "CAPTIONED_FIGURE": lambda raw, inner, tt, ctx, reg:
        _process_captioned_figure(raw, inner, tt, reg),
    # CAPTIONED_FIGURE_GRID — multi-image side-by-side figure layout
    # (≥2 IMAGE children in the first row, separated by `||`).
    # Initially shared with LAYOUT_WRAPPER's producer; a focused
    # grid-disentanglement producer is the natural follow-up.
    "CAPTIONED_FIGURE_GRID": lambda raw, inner, tt, ctx, reg:
        _unwrap_layout_table(inner, tt, reg),
    # FIGURE_GROUP — outer wikitable wrapping ≥2 nested figure
    # wikitables (HYDROMEDUSAE-style composite).  No direct images
    # at this level.  Initially shared with LAYOUT_WRAPPER.
    "FIGURE_GROUP": lambda raw, inner, tt, ctx, reg:
        _unwrap_layout_table(inner, tt, reg),
    "COMPLEX_HTML": lambda raw, inner, tt, ctx, reg:
        _process_complex_table(inner, tt),
    "CHEMISTRY_LAYOUT": lambda raw, inner, tt, ctx, reg:
        _process_chemistry_layout(inner, tt, reg),
    "DATA_TABLE": lambda raw, inner, tt, ctx, reg:
        _process_table(inner, tt, reg),
    # Single-label kinds — element_type == label.
    "DJVU_CROP": lambda raw, inner, tt, ctx, reg:
        _process_djvu_crop(raw, tt, ctx),
    "CHART2": lambda raw, inner, tt, ctx, reg: _process_chart2(raw, ctx),
    "COMPOUND_TABLE": lambda raw, inner, tt, ctx, reg:
        _process_compound_table(raw, tt),
    "MATH": lambda raw, inner, tt, ctx, reg: _process_math(inner),
    "SCORE": lambda raw, inner, tt, ctx, reg: _process_score(inner),
    "REF_SELF": lambda raw, inner, tt, ctx, reg:
        _process_ref_self(raw, ctx.ref_bodies),
    "REF": lambda raw, inner, tt, ctx, reg:
        _process_ref(raw, inner, tt, ctx.ref_bodies),
    "IMAGE": lambda raw, inner, tt, ctx, reg: _process_image(inner, tt),
    "IMAGE_FLOAT": lambda raw, inner, tt, ctx, reg:
        _process_image_float(inner, tt),
    "POEM": lambda raw, inner, tt, ctx, reg: _process_poem(inner, tt),
    "HIEROGLYPH": lambda raw, inner, tt, ctx, reg:
        f"[hieroglyph: {inner}]",
    "HTML_TABLE": lambda raw, inner, tt, ctx, reg:
        _process_html_table(raw, inner, tt, reg),
    "OUTLINE": lambda raw, inner, tt, ctx, reg: _process_outline(inner, tt),
}





# ── Independent wikitable predicates ──────────────────────────────────
#
# Each function below is a self-contained "is this MY kind of
# wikitable?" check.  Same signature, same input shape, no knowledge
# of any other predicate.  No predicate is defined as "X but not Y" —
# overlaps are resolved by priority in `_TABLE_PREDICATES`, not by the
# code structure.
#
# Adding a new wikitable kind is one new predicate + one entry in
# the registry.  Removing one is one delete in each.  No edits to
# other predicates.  Entanglement is the source of bugs here, so
# entanglement is what the architecture refuses.
def _is_djvu_crop_table(raw: str, inner: str,
                         registry: ElementRegistry | None) -> bool:
    """Table wraps a `{{Css image crop}}` template — the crop's
    producer parses the raw wikitext directly."""
    return bool(re.search(r"\{\{Css image crop", raw, re.IGNORECASE))


def _is_compound_table_pred(raw: str, inner: str,
                             registry: ElementRegistry | None) -> bool:
    """Data table (header has `border`/`rules`/`class=wikitable`) with
    at least one nested TABLE child.  Compound producer parses the
    parallel-row structure from raw."""
    return _is_compound_table(raw, registry)


def _is_chemistry_layout_pred(raw: str, inner: str,
                               registry: ElementRegistry | None) -> bool:
    """At least one descendant IMAGE element carries a chemistry
    bracket file ref (Langle/Rangle variant)."""
    return _has_chem_brackets(registry)


_FIGURE_LABELS: frozenset[str] = frozenset({
    "CAPTIONED_FIGURE", "CAPTIONED_FIGURE_GRID",
})


def _is_figure_group_pred(
    raw: str, inner: str,
    registry: ElementRegistry | None,
) -> bool:
    """Outer wikitable wrapping ≥2 nested figure wikitables, with NO
    direct IMAGE child at this level.  The figures live one level
    down — each nested wikitable already classified as
    `CAPTIONED_FIGURE` / `CAPTIONED_FIGURE_GRID` by the bottom-up
    pass — and this wrapper is just a layout shell to display them
    together (HYDROMEDUSAE side-by-side Statocyst figures, etc.).

    Producer (initially shared with LAYOUT_WRAPPER) handles the
    composite layout by routing each nested figure through its own
    producer and concatenating.

    Constraint: nested children must be FIGURE-labeled (not arbitrary
    wikitables).  A wrapper around two data tables is something
    different — leave it at LAYOUT_WRAPPER or DATA_TABLE.
    """
    if registry is None:
        return False
    # No direct IMAGE child — those go to CAPTIONED_FIGURE /
    # CAPTIONED_FIGURE_GRID instead.
    if any(lbl == "IMAGE" for lbl in registry.labels.values()):
        return False
    # ≥2 nested figure children.
    figure_children = [ph for ph, lbl in registry.labels.items()
                       if lbl in _FIGURE_LABELS]
    if len(figure_children) < 2:
        return False
    # No data-table header signal.
    header = raw.split("\n", 1)[0]
    if (re.search(r'border\s*=\s*"?[1-9]', header, re.IGNORECASE)
            or re.search(r'rules\s*=', header, re.IGNORECASE)
            or re.search(r'class\s*=\s*"[^"]*(?:wikitable|tablecolhd|border)',
                          header, re.IGNORECASE)):
        return False
    return True


def _is_captioned_figure_grid_pred(
    raw: str, inner: str,
    registry: ElementRegistry | None,
) -> bool:
    """Multi-image figure grid: ≥2 IMAGE children, all sitting in
    the first row separated by `\\|\\|` column dividers (parallel-
    column layout, captions and legends in matching columns below).

    The classic side-by-side figures case (BEE Figs 18/19, LARVAL
    FORMS 5/6, FUNGI 12/13).  Producer disentangles by walking down
    each column to pair an image with its column-stacked
    caption / attribution / legend content.

    No data-table header signal — otherwise the wikitable is a real
    data table that happens to contain images.

    Variant NOT covered by this predicate: a wikitable that wraps
    two nested figure-wikitables side-by-side (HYDROMEDUSAE-style).
    There the images are children-of-children, not direct children
    of the outer wikitable — different structural shape, separate
    predicate.
    """
    if registry is None:
        return False
    image_phs = [ph for ph, lbl in registry.labels.items()
                 if lbl == "IMAGE"]
    if len(image_phs) < 2:
        return False
    rows = re.split(r"\n\|-[^\n]*\n", inner)
    if not rows:
        return False
    # All images must sit in the FIRST row, in separate cells.  Cell
    # separators within a row can be either `||` (same-line) or
    # `\n|` (newline-pipe) — both are valid wikitable syntax for
    # parallel cells.  ARACHNIDA's side-by-side figures use newline-
    # pipe with `{{em|3}}` / `{{gap}}` spacer cells between images.
    first = rows[0]
    if not all(ph in first for ph in image_phs):
        return False
    # Count cells in the first row.  Any non-empty cell counts.
    cells = re.split(r"\n\||\|\|", first)
    cells = [c for c in cells if c.strip()]
    image_cells = [c for c in cells if any(ph in c for ph in image_phs)]
    if len(image_cells) < 2:
        return False
    # No data-table header signal.
    header = raw.split("\n", 1)[0]
    if (re.search(r'border\s*=\s*"?[1-9]', header, re.IGNORECASE)
            or re.search(r'rules\s*=', header, re.IGNORECASE)
            or re.search(r'class\s*=\s*"[^"]*(?:wikitable|tablecolhd|border)',
                          header, re.IGNORECASE)):
        return False
    return True


def _is_captioned_figure_pred(raw: str, inner: str,
                                registry: ElementRegistry | None) -> bool:
    """Single-image figure layout: exactly one IMAGE child sitting
    alone IN THE FIRST ROW (no `\\|\\|`-separated parallel column),
    no data-table header signal.

    The dominant ~73% of figure-shaped wikitables: an image first,
    then N rows of caption / attribution / legend below.  The
    image-must-be-in-row-0 gate rejects data tables that happen to
    contain a trailing image (ARENIG GROUP is a multi-row taxonomic
    ladder with a geological-unconformity image in the last row —
    the wikitable is data, not a figure).

    Runs before LAYOUT_WRAPPER so single-image figures get their
    own label.  Multi-image grids fall through to LAYOUT_WRAPPER
    until a dedicated grid predicate lands.
    """
    if registry is None:
        return False
    image_phs = [ph for ph, lbl in registry.labels.items()
                 if lbl == "IMAGE"]
    if len(image_phs) != 1:
        return False
    # Image must sit in row 0 — figures lead with the image; data
    # tables with an embedded image rarely do.
    rows = re.split(r"\n\|-[^\n]*\n", inner)
    if not rows:
        return False
    if image_phs[0] not in rows[0]:
        return False
    # And alone in that row — no `||` parallel cells (parallel-row
    # multi-image grids fall through to LAYOUT_WRAPPER / future
    # grid predicate).
    if "||" in rows[0].strip():
        return False
    # Header carries no data-table signal.
    header = raw.split("\n", 1)[0]
    if (re.search(r'border\s*=\s*"?[1-9]', header, re.IGNORECASE)
            or re.search(r'rules\s*=', header, re.IGNORECASE)
            or re.search(r'class\s*=\s*"[^"]*(?:wikitable|tablecolhd|border)',
                          header, re.IGNORECASE)):
        return False
    return True


def _is_layout_wrapper_pred(raw: str, inner: str,
                             registry: ElementRegistry | None) -> bool:
    """Image+caption wrapper or nested-table wrapper shape."""
    return _is_layout_wrapper(raw, inner, registry)


def _is_brace_table(raw: str, inner: str,
                     registry: ElementRegistry | None) -> bool:
    """Table contains a `{{brace}}` / `{{brace|…}}` template — the
    poem-with-translation layout pattern.  Always rendered as
    DATA_TABLE even when carrying rowspan."""
    return bool(re.search(r"\{\{brace(?:\s*\||\s*\})", raw, re.IGNORECASE))


def _is_math_dominant_pred(raw: str, inner: str,
                            registry: ElementRegistry | None) -> bool:
    """≥75% of children are MATH placeholders, no disqualifying child
    types, no header (`!`) row, no data-table class, ≤1 substantive
    prose cell.  Math-dominant tables emit MATH placeholders in cells
    and the equation-layout producer handles them."""
    return _is_math_dominant_layout(raw, inner, registry)


def _is_math_layout_tokens_pred(raw: str, inner: str,
                                 registry: ElementRegistry | None) -> bool:
    """Cells hold raw math tokens (not `<math>` blocks).  KaTeX
    `\\begin{aligned}` / `\\begin{vmatrix}` producer applies."""
    return _math_table_kind(raw, inner, registry) == "tokens"


def _is_math_layout_equations_pred(raw: str, inner: str,
                                    registry: ElementRegistry | None) -> bool:
    """Either `<math>`-block layout or HTML-wrapper-with-math.  Both
    flow through the row-per-paragraph equation-layout producer."""
    return _math_table_kind(raw, inner, registry) in ("math_blocks",
                                                       "html_wrapper")


def _has_data_signal_and_ts(raw: str, inner: str,
                             registry: ElementRegistry | None) -> bool:
    """Header carries a data-table signal AND any cell uses `{{Ts}}`
    styling templates.  The combination breaks `_process_table`'s
    cell parsing (Ts adds phantom pipes); needs HTML rendering."""
    header = raw.split("\n", 1)[0]
    has_data_signal = (
        re.search(r'border\s*=\s*"?[1-9]', header, re.IGNORECASE) or
        re.search(r'rules\s*=', header, re.IGNORECASE) or
        re.search(r'class\s*=\s*"?[^"\s]*(?:wikitable|tablecolhd|border)',
                  header, re.IGNORECASE))
    return bool(has_data_signal) and bool(
        re.search(r'\{\{[Tt]s\|', raw))


def _has_rowspan_or_colspan(raw: str, inner: str,
                             registry: ElementRegistry | None) -> bool:
    """Cell-spanning attributes — needs HTML passthrough to render
    properly.  Last-resort COMPLEX_HTML route for tables that aren't
    caught by more-specific predicates above (chemistry, layout
    wrapper, math-dominant, brace)."""
    return bool(
        re.search(r"rowspan\s*=", raw, re.IGNORECASE)
        or re.search(r"colspan\s*=", raw, re.IGNORECASE))


def _always_true(raw: str, inner: str,
                  registry: ElementRegistry | None) -> bool:
    """Catch-all — anything reaching here is a regular DATA_TABLE."""
    return True


# Priority-ordered list of `(predicate, label)` pairs.  When a table
# matches multiple predicates, the EARLIEST entry wins.  The order
# below reflects the resolution behaviour of the prior if-elif chain
# in `_classify_table`; the difference is each predicate is now
# self-contained — no predicate's truthiness depends on another's
# falseness.
_TABLE_PREDICATES: list[tuple[Callable[
    [str, str, "ElementRegistry | None"], bool], str]] = [
    (_is_djvu_crop_table,             "DJVU_CROP"),
    (_is_compound_table_pred,          "COMPOUND_TABLE"),
    (_is_chemistry_layout_pred,        "CHEMISTRY_LAYOUT"),
    (_is_captioned_figure_grid_pred,   "CAPTIONED_FIGURE_GRID"),
    (_is_captioned_figure_pred,        "CAPTIONED_FIGURE"),
    (_is_figure_group_pred,            "FIGURE_GROUP"),
    (_is_layout_wrapper_pred,          "LAYOUT_WRAPPER"),
    (_is_brace_table,                  "DATA_TABLE"),
    (_is_math_dominant_pred,           "MATH_LAYOUT_EQUATIONS"),
    (_has_data_signal_and_ts,          "COMPLEX_HTML"),
    (_has_rowspan_or_colspan,          "COMPLEX_HTML"),
    (_is_math_layout_tokens_pred,      "MATH_LAYOUT_TOKENS"),
    (_is_math_layout_equations_pred,   "MATH_LAYOUT_EQUATIONS"),
    (_always_true,                     "DATA_TABLE"),
]


def _classify_table(raw: str, inner: str,
                     inner_registry: ElementRegistry | None) -> str:
    """Classify a wiki table to its producer-dispatch label.

    Iterates `_TABLE_PREDICATES` in priority order; first match wins.
    Each predicate is independent — examines its own structural
    signal in isolation.  Adding a new wikitable kind is a new
    predicate + a new entry in the registry above; nothing else
    changes."""
    for predicate, label in _TABLE_PREDICATES:
        if predicate(raw, inner, inner_registry):
            return label
    # Unreachable — `_always_true` catches everything not matched above.
    return "DATA_TABLE"



# ── Image-layout subclassification ────────────────────────────────────
#
# Layout wikitables wrapping a single image fall into a few structural
# subclasses we recognize explicitly. Each subclass has a dedicated
# handler that emits `{{IMG:filename|caption}}` plus, when applicable,
# `{{LEGEND:…}LEGEND}` for the structured legend. Classes:
#
#   IMG_INLINE_LEGEND  — image cell + same-line `||` legend items;
#                        caption in a `|colspan=N|…Fig.` row below.
#                        Example: ABBEY vol 1 p. 43 (Fig. 1, Santa Laura).
#   IMG_MULTICOL_LEGEND — image row + caption row + subsequent rows of
#                         `||`-separated (label, text) pairs that need
#                         re-sorting into alphabetic reading order.
#                         Example: ABBEY vol 1 p. 46 (Fig. 5, Cluny).
#   IMG_POEM_LEGEND    — outer table wraps image + caption + a nested
#                        POEM-only layout table (left/right columns of
#                        `<poem>` legends with `{{csc|…}}` subheadings).
#                        Example: ABBEY vol 1 p. 44 (Fig. 3, St Gall).
#
# Anything that doesn't match falls back to the generic layout unwrap
# logic further down.


# Accepts legend labels like:
#   A              single letter
#   P₁             letter + subscript
#   X₁X₁           repeated letter-subscript pair (Abbey_3 X₁X₁, X₂X₂)
#   c,c            comma-separated repeats (Abbey_3 "c,c. Mills.")
#   k,k,k          same, 3 entries
#   1              single digit (Fig. 9 Kirkstall Abbey)
#   10             multi-digit (Fig. 10 Fountains third column)
#   16-19          inclusive range (Fig. 9 "16-19. Uncertain…")
#   c.c            dotted compound abbreviation (HYDROMEDUSAE Fig. 30
#                    "c.c,  Circular canal.")
#   st.c           similar
# Label must start AND end with an alphanumeric; internal chars may
# include letters, digits, subscript chars, a hyphen (for ranges),
# or a period (for compound abbreviations).
# The trailing `.` is required (legends always use "L. text" form).

# Same shape but with an italic-wrapped label.  ARACHNIDA Fig. 31
# uses ``''d'', Chelicera. || ''ad'', Muscle…`` — every cell is a
# complete (italic-label, text) entry instead of an alternating
# label/text pair.
# Strict validator for a parsed legend label — rejects "Steiner," and
# similar chemist-name false positives while accepting "A", "P₁",
# "X₁X₁", "c,c", "k,k,k", "1", "10", "16-19", "c.c", "st.c", etc.
#
# Also accepts multi-word italicized biological abbreviations
# (HYDROMEDUSAE Fig. 30 "c.c,  Circular canal.", SPONGES Fig. 2
# "cl. osc.,  Closed osculum.", etc.) — up to ~20 chars, made of
# short alphanumeric words separated by periods or spaces.

# Latin ligatures used in biological abbreviations (œsophagus, æ for
# ae in taxonomic names) — fold to ASCII so legend-label validation
# accepts them (TUNICATA Fig. 5: `œ` for Oesophagus, `œa` for
# Oesephageal aperture).




# Matches a `Fig. N.` / `Plate N.` caption anywhere in a string.
# Allows up to 10 chars of punctuation/whitespace between `Fig` and
# the number so variants like `{{sc|Fig}}. 8` (period AFTER the `}}`)
# still match.

# Prose-legend entry: `LABEL, text.` or `LABEL. text.` chunk where
# LABEL is a short alphanumeric token (possibly Roman numeral).

# Static lookup: (volume, page) → chart image filename

# ── Public API ────────────────────────────────────────────────────────

def process_elements(text: str, text_transform, context: ElementContext) -> str:
    """Extract, process, and reassemble all embedded elements.

    Walker–classifier–producer pipeline.  The classifier drives
    recursion: at each level it strips the shape's outer delimiters,
    asks the walker for one-level extracts of the inner, recursively
    classifies each child, then decides its own label.  The producer
    pass walks the classified tree bottom-up emitting markers.

    Args:
        text: raw wikitext (may contain tables, images, footnotes, etc.)
        text_transform: function that transforms plain wikitext to marker format
        context: per-article ElementContext (volume / page used for score
            and chart-image lookups)

    Returns:
        text with all embedded elements processed to their final form
    """
    from britannica.pipeline.stages.elements._classifier import (
        classify_article,
        produce_tree,
        resolve_ref_bodies,
        substitute_top_level_markers,
    )

    # Walk + classify (one mutually-recursive pass).  Returns the
    # placeholderized article body plus a tree of ClassifiedElement
    # records — each element knows its own label, raw bytes, inner
    # text, and inner registry of classified children.
    placeholderized_text, tree = classify_article(text)

    # Body text transform — operates on the prose between top-level
    # placeholders only.  Each element's inner content is transformed
    # by its own producer when it runs.
    text = text_transform(placeholderized_text)

    # Article-wide ref-body resolution.  `<ref name=X/>` self-closing
    # reuses (and `<ref name=X>body…` definitions whose body arrives
    # via a later `<ref follow=X>…` continuation) resolve to the
    # merged body.  Threaded into `context` so the REF producer can
    # read it.  Copy the caller's context so we don't mutate it.
    context = _dc_replace(context)
    context.ref_bodies = resolve_ref_bodies(tree, text_transform)

    # Produce: bottom-up over the tree.  Each element's producer
    # runs after its children's markers exist; child markers are
    # substituted into the producer's output by the framework.
    produce_tree(tree, text_transform, context)

    # Reassemble: substitute top-level markers into the article body.
    return substitute_top_level_markers(text, tree)
