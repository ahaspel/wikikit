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
    TABLE_LABELS,
    _PH,
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
    _cell_is_legend_full_entry,
    _cell_is_legend_label,
    _clean_legend_text,
    _collect_attribution_rows,
    _emit_legend_chunk,
    _entries_look_like_legend,
    _extract_caption_from_colspan_row,
    _extract_poem_legend,
    _find_caption_row_idx,
    _format_legend_entries,
    _HI_LEGEND_RE,
    _image_ph_filename,
    _is_layout_wrapper,
    _legend_cell_prep,
    _looks_like_caption,
    _normalize_icl_markup,
    _parse_inline_legend_cell,
    _parse_multicol_legend_row,
    _parse_prose_legend_rows,
    _process_captioned_figure,
    _process_captioned_figure_inline,
    _process_legended_figure,
    _process_legended_figure_beside,
    _process_legended_figure_child,
    _process_prose_figure,
    _process_simple_plate,
    _row_has_legend_multicol_cells,
    _simple_table_text,
    _strip_cell_attributes,
    _try_image_layout_subclass,
    _unwrap_layout_table,
)
from britannica.pipeline.stages.elements._tables import (
    _extract_subtable_values,
    _has_chem_brackets,
    _has_chem_equation_content,
    _has_chem_reaction_content,
    _is_html_illustration_wrapper,
    _is_single_column_table,
    _is_verse_table,
    _process_chemistry_layout,
    _process_complex_table,
    _process_compound_table,
    _process_html_table,
    _process_single_column_table,
    _process_table,
    _process_verse_table,
    _table_grid,
    _unwrap_html_illustration,
    split_wiki_row,
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


def _produce_figure(raw, inner, text_transform, context, inner_registry):
    """Assemble a figure (image + its structural caption run).

    Structural path: `_process_prose_figure` parses the span, folds the
    caption + attribution into the `{{IMG:…}}` marker and emits any legend
    separately — consuming the caption so it never renders twice (the
    leak/duplicate fix).

    Multi-image caption rows (Figs 22-23) aren't owned by the structural
    producer yet; for those it returns None and we fall back to the legacy
    `_assemble_figures` (re-process with figure recognition OFF so the
    sub-walk doesn't recurse on this span) so they keep rendering as before.
    """
    structural = _process_prose_figure(raw, text_transform)
    if structural is not None:
        return structural
    from britannica.pipeline.stages.transform_articles.legend_promote import (
        _assemble_figures,
    )
    produced = process_elements(
        raw, text_transform, context, _allow_figure=False)
    return _assemble_figures(produced)


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
    # UNPAIRED_FIGURE_GROUP — ≥2 images the classifier hands off as a
    # group; the producer partitions cells per image (vertical-stack OR
    # parallel-row column-slice) and routes each image's cells through the
    # shared figure pipeline, bundling what pairs and passing through the
    # rest.  Uses the former SIMPLE_PLATE producer, which is TOTAL here:
    # vs the generic `_unwrap_layout_table` passthrough it bundles equal-
    # or-more in every case (0 regressions, +11 fixes incl. 4 grids the
    # passthrough was silently under-bundling).  Collapses the old
    # SIMPLE_PLATE + CAPTIONED_FIGURE_GRID labels into one.
    "UNPAIRED_FIGURE_GROUP": lambda raw, inner, tt, ctx, reg:
        _process_simple_plate(raw, inner, tt, reg),
    # CAPTIONED_FIGURE — single-image figure layout (one IMAGE child
    # in row 0, alone in cell, no data-table header signal).  Has
    # its own focused producer; falls back to `_unwrap_layout_table`
    # for shapes it doesn't yet handle (baked captions in image raw,
    # nested wikitables, POEM cells).
    "CAPTIONED_FIGURE": lambda raw, inner, tt, ctx, reg:
        _process_captioned_figure(raw, inner, tt, reg),
    # CAPTIONED_FIGURE_INLINE — image and caption share one cell,
    # `<br>`-separated, often wrapped in `{{center|…}}` or a
    # `<span style="…">…</span>` styling element.  Producer strips
    # the image placeholder + wrapper templates from the cell, the
    # remaining text is the caption.
    "CAPTIONED_FIGURE_INLINE": lambda raw, inner, tt, ctx, reg:
        _process_captioned_figure_inline(raw, inner, tt, reg),
    # LEGENDED_FIGURE — single-image figure carrying legend material
    # (multicol `|LABEL.||text|…`, prose `LABEL. text` cells, POEM
    # placeholder, or nested wikitable).  Producer finds + extracts
    # the legend first, then runs the same one-pass extract on the
    # caption/attribution remainder, then assembles.
    "LEGENDED_FIGURE": lambda raw, inner, tt, ctx, reg:
        _process_legended_figure(raw, inner, tt, reg),
    # LEGENDED_FIGURE_BESIDE — single-image figure whose legend sits
    # in a sibling cell on the image's row, separated by `||`
    # (ABBEY Fig 1 Santa Laura).  Producer parses the sibling cell's
    # paragraph-separated `LABEL. text` entries directly from the
    # raw row text (so paragraph-`\n\n` breaks survive), then finds
    # the caption in a subsequent colspan row.
    "LEGENDED_FIGURE_BESIDE": lambda raw, inner, tt, ctx, reg:
        _process_legended_figure_beside(raw, inner, tt, reg),
    # LEGENDED_FIGURE_CHILD — single-image figure whose legend lives
    # in a POEM placeholder or a nested wikitable child.  Producer
    # finds the child, calls the appropriate raw-parser
    # (`_emit_legend_chunk` for POEM, `_extract_poem_legend` for
    # nested TABLE), excises the placeholder, and runs one-pass
    # extraction on the remainder.
    "LEGENDED_FIGURE_CHILD": lambda raw, inner, tt, ctx, reg:
        _process_legended_figure_child(raw, inner, tt, reg),
    # (SIMPLE_PLATE + CAPTIONED_FIGURE_GRID labels removed — multi-image
    # figures now classify as UNPAIRED_FIGURE_GROUP, above, which inherits
    # the SIMPLE_PLATE producer `_process_simple_plate`.)
    # FIGURE_GROUP — outer wikitable wrapping ≥2 nested figure
    # wikitables (HYDROMEDUSAE-style composite).  No direct images
    # at this level.  Initially shared with LAYOUT_WRAPPER.
    "FIGURE_GROUP": lambda raw, inner, tt, ctx, reg:
        _unwrap_layout_table(inner, tt, reg),
    # FIGURE — a bare image + its structural caption run, carved as one span
    # by the walker.  Producer re-processes + assembles (see `_produce_figure`).
    "FIGURE": _produce_figure,
    "COMPLEX_HTML": lambda raw, inner, tt, ctx, reg:
        _process_complex_table(inner, tt),
    "CHEMISTRY_LAYOUT": lambda raw, inner, tt, ctx, reg:
        _process_chemistry_layout(inner, tt, reg),
    "DATA_TABLE": lambda raw, inner, tt, ctx, reg:
        _process_table(inner, tt, reg),
    # SINGLE_COLUMN_TABLE — a `{|…|}` boxing a run of text (one content
    # cell per row), not a grid.  Carved out of `_process_table`'s hidden
    # dispatch; rendered as a `«PRE:` text block.
    "SINGLE_COLUMN_TABLE": lambda raw, inner, tt, ctx, reg:
        _process_single_column_table(inner, tt),
    # VERSE_TABLE — a 2-column quotation layout (hanging-quote col1 + verse
    # lines col2).  Carved out of `_process_table`'s hidden dispatch;
    # rendered as `{{VERSE:}VERSE}`.
    "VERSE_TABLE": lambda raw, inner, tt, ctx, reg:
        _process_verse_table(inner, tt),
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
    """A chemistry-reaction / structural-formula layout, recognized by any of:
    a descendant Langle/Rangle bracket IMAGE; the `<big>`-operator + `<sub>`
    formula signal (ACCUMULATOR discharge/energy, acetone); or — the
    element-aware arm — operator-connected molecular formulae in a cell,
    which catches reactions typeset with plain =/+/-> and {{sub}} formulae
    that otherwise fall through to DATA_TABLE / SINGLE_COLUMN."""
    return (_has_chem_brackets(registry)
            or _has_chem_equation_content(raw)
            or _has_chem_reaction_content(inner))


_FIGURE_LABELS: frozenset[str] = frozenset({
    "CAPTIONED_FIGURE", "CAPTIONED_FIGURE_INLINE",
    "UNPAIRED_FIGURE_GROUP",
    "LEGENDED_FIGURE", "LEGENDED_FIGURE_BESIDE",
    "LEGENDED_FIGURE_CHILD",
})


_DATA_TABLE_HEADER_RE = re.compile(
    r'border\s*=\s*"?[1-9]'
    r'|rules\s*='
    r'|class\s*=\s*"[^"]*(?:wikitable|tablecolhd|border)',
    re.IGNORECASE,
)


def _is_icl_family(raw: str, inner: str,
                    registry: ElementRegistry | None) -> bool:
    """Reliable structural/semantic test: is this wikitable an
    image/caption/legend (ICL) figure?

    Two conditions must hold simultaneously:

      1. No data-table signal — the wikitable doesn't look like a
         tabular layout.  Anti-signals: ``border=N``, ``rules=``,
         ``class="…wikitable…"`` on the ``{|`` opener; ``|+`` table-
         caption sigil; ``!`` column-header row sigil.

      2. A figure carrier is present — either an IMAGE child or, for
         the ``FIGURE_GROUP`` wrapper case, ≥2 nested figure-labelled
         children whose images live one level down.

    Sub-shape dispatch in ``_classify_icl_shape`` decides whether the
    image carries caption / legend / attribution material or is bare.
    If the structure doesn't match any focused sub-shape (image with
    only ``<ref>``-footnote siblings, etc.), the dispatcher returns
    ``None`` and the table falls through to ``LAYOUT_WRAPPER`` — the
    catch-all for residual figure layouts.
    """
    if registry is None:
        return False
    # 1. Anti-signals.
    header = raw.split("\n", 1)[0]
    if _DATA_TABLE_HEADER_RE.search(header):
        return False
    # caption sigil: `|+` (wiki) or `<caption>` (HTML)
    if (re.search(r"^\|\+", inner, re.MULTILINE)
            or re.search(r"<caption\b", inner, re.IGNORECASE)):
        return False
    # column-header sigil: `!` (wiki) or `<th>` (HTML)
    if (re.search(r"^\s*!", inner, re.MULTILINE)
            or re.search(r"<th\b", inner, re.IGNORECASE)):
        return False
    # Tall-brace taxonomy grouping (`<math>\left\{…\right.>`): a print-
    # typographic device that brackets grouped rows of an outline/
    # taxonomy table (ARENIG GROUP — Ordovician sub-divisions).  The
    # table may carry an incidental small diagram, but it is an
    # outline, not a figure — leave it for the data/outline path.
    if re.search(r"\\left\s*\\?\{", raw):
        return False
    # 2. Figure carrier present.
    labels = registry.labels.values()
    has_image = any(lbl == "IMAGE" for lbl in labels)
    figure_child_count = sum(1 for lbl in labels if lbl in _FIGURE_LABELS)
    return has_image or figure_child_count >= 2


# Legend-material detectors — used by `_classify_icl_shape`.
#
# A figure carries legend material when one of:
#   (a) multicol shape — rows shaped like `|LABEL.||text||LABEL.||text…`
#       where the labels are short alphanumerics; ABBEY Fig 5 is the
#       canonical case (5 rows × 3 (label,text) pairs).
#   (b) prose shape — a cell holding ≥3 line-start `LABEL. text` entries,
#       optionally on one collapsed line; HYDROMEDUSAE Fig 1 cell:
#       `a. Hydranth; b. Hydrocaulus; c. Hydrorhiza; …`.
#   (c) a POEM child placeholder (legend rendered as a poem block).
#   (d) a nested wikitable child (legend in its own sub-table).
#
# Predicate-time detection is coarse — the producer's `find_legend`
# does the precise parse.  False-positives downgrade gracefully (the
# producer just emits no legend block).

# Prose-cell legend entry: a SHORT, LETTER-STARTING label (`a`, `bo`,
# `«I»mg«/I»`) followed by `.,` then text.  Three deliberate
# restrictions, each tuned against a class of false positive:
#   * Letter start (not digit) — numbered move-lists like a chess
#     solution (`1. P – Kt6; 2. P – B6`) otherwise look like a
#     numbered legend and get mis-detected (CHESS vol 6 p106).
#     Digit-labelled legends only occur in the MULTICOL shape
#     (MOSQUE `| 1. Kibla. ||…`), handled cell-aware.
#   * 1-4 chars only — anatomical legend abbreviations are short
#     (`a`, `oc`, `mg`, `s.gl`); a 5-6 char limit lets ordinary
#     prose words ending in a comma (`corner,`, `border,`, `field.`
#     in a heraldry description — SOUTH AFRICA vol 25 p479) match
#     as pseudo-labels.  Longer real labels (`prae-gen`) only occur
#     in the MULTICOL shape, handled cell-aware.
#   * No internal spaces/commas — a permissive label would greedily
#     swallow the entry's descriptive text and never reach the `[.,]`
#     terminator (METAMORPHOSIS `A, Side view…`).
_LEGEND_PROSE_ENTRY_RE = re.compile(
    r"(?:^|\n|\|\s*|;\s*)"
    r"(?:«I»[A-Za-z][A-Za-z0-9.]{0,3}«/I»|[A-Za-z][A-Za-z0-9.]{0,3})"
    r"[′″‴]?"
    r"\s*[.,]\s+[A-Z‘“a-z]"
)

_LEGEND_NON_LABELS = frozenset({"Fig", "Plate", "fig", "plate"})


def _has_legend_material(inner: str,
                          registry: "ElementRegistry | None",
                          image_phs: list[str]) -> bool:
    """Predicate-time coarse detection of cell-based legend material.

    Two row shapes count:

      * **Multicol** — a row whose cells (post-``split_wiki_row``)
        carry ≥2 legend-shaped entries (label-only OR full
        `LABEL[.,]\\s+TEXT`).  Cell-aware so the detector sees past
        cell-attribute prefixes (``|align="right"|…``) and template
        wrappers (``{{nowrap|…}}``).  Two such rows in the inner
        confirm legend material.

      * **Prose** — a single cell carrying ≥3 inline ``LABEL.,text``
        entries separated by ``;`` or newlines (HYDROMEDUSAE Fig 1
        shape).

    Shared with the producer's row-partitioner via
    ``_row_has_legend_multicol_cells`` so the predicate and
    producer agree on what counts as legend.
    """
    # Hanging-indent legend: ≥2 `{{Hi|SIZE|LABEL, text}}` entries
    # (ARACHNIDA Fig 26, 72, 78).  Each is one entry; the producer's
    # Phase 0 (`_extract_hi_legend`) extracts them.
    if len(_HI_LEGEND_RE.findall(inner)) >= 2:
        return True
    rows = re.split(r"\|-[^\n]*", inner)
    multicol_row_count = 0
    for row in rows:
        if not row.strip():
            continue
        if image_phs and any(ph in row for ph in image_phs):
            continue
        if _row_has_legend_multicol_cells(row):
            multicol_row_count += 1
            if multicol_row_count >= 2:
                return True
    # Prose-cell legend: any cell with ≥3 inline `LABEL.,text` entries.
    prose_hits = 0
    for m in _LEGEND_PROSE_ENTRY_RE.finditer(inner):
        label_m = re.search(r"([A-Za-z]+|\d+)", m.group(0))
        if label_m and label_m.group(1) in _LEGEND_NON_LABELS:
            continue
        prose_hits += 1
        if prose_hits >= 3:
            return True
    return False


def _figure_has_child_legend(
    registry: ElementRegistry, image_phs: list[str],
) -> bool:
    """True iff the figure carries a POEM or nested-wikitable child —
    the signal that the legend lives in a child element, not in
    regular cells."""
    for ph, lbl in registry.labels.items():
        if ph in image_phs:
            continue
        if lbl == "POEM" or lbl in TABLE_LABELS:
            return True
    return False


_INLINE_CAPTION_MARKER_RE = re.compile(
    r"\{\{\s*(?:sc|csc|SC)\s*\|\s*(?:Fig|Plate)s?\.?"
    r"|\b(?:Fig|Plate)s?\.?\s*\d",
    re.IGNORECASE,
)


def _has_inline_caption_signal(inner: str, image_ph: str) -> bool:
    """Inline-caption signal: the image's row carries a Fig./Plate.
    marker (the image and its caption share the same row, often the
    same cell separated by ``<br>``).  Canonical cases: ORDNANCE
    Fig 54, STEAM_ENGINE Fig 10.

    Differs from a plain CAPTIONED_FIGURE in that the caption sits
    in the image's own row rather than a subsequent row.
    """
    grid = _table_grid(inner)
    image_row = next((cells for cells in grid
                      if any(image_ph in c for c in cells)), None)
    if image_row is None:
        return False
    return bool(_INLINE_CAPTION_MARKER_RE.search(" ".join(image_row)))


def _image_alone_in_row(inner: str, image_ph: str) -> bool:
    """The image is the only non-empty cell in its row — the canonical
    captioned-figure shape (image on top, caption/legend in subsequent
    rows).  Empty leading ``||`` cells (typographic spacing) don't
    disqualify; non-empty sibling cells (legend material, footnote
    refs, decorative content) do.
    """
    grid = _table_grid(inner)
    image_row = next((cells for cells in grid
                      if any(image_ph in c for c in cells)), None)
    if image_row is None:
        return False
    siblings = [c for c in image_row if c.strip() and image_ph not in c]
    return len(siblings) == 0


def _has_beside_legend_signal(inner: str, image_ph: str) -> bool:
    """Beside-legend signal: the image's row contains the image
    placeholder on a line with `||` separating it from a sibling
    cell whose paragraph-`\\n\\n`-separated content matches
    ``LABEL. text`` entries (ABBEY Fig 1 shape).

    Looks at the raw row text — `split_wiki_row` would have
    collapsed the paragraph breaks that distinguish legend entries
    from a single multi-sentence caption.
    """
    rows = re.split(r"\|-[^\n]*", inner)
    image_row = next((r for r in rows if image_ph in r), None)
    if image_row is None:
        return False
    row_lines = [l for l in image_row.split("\n") if l.strip()]
    image_line = next((l for l in row_lines if image_ph in l), None)
    if image_line is None or "||" not in image_line:
        return False
    li = row_lines.index(image_line)
    cell_lines = [image_line]
    for nxt in row_lines[li + 1:]:
        if nxt.lstrip().startswith("|"):
            break
        cell_lines.append(nxt)
    cell_text = "\n".join(cell_lines).lstrip("|")
    # `_parse_inline_legend_cell` returns entries iff the cell matches
    # the image-placeholder + `||` + paragraph-`LABEL. text` shape.
    # Use a no-op text_transform here — predicate runs before markers
    # are emitted; the producer re-parses with the real transform.
    _, entries = _parse_inline_legend_cell(cell_text, lambda s: s)
    return len(entries) >= 3 and _entries_look_like_legend(entries)


def _classify_icl_shape(raw: str, inner: str,
                         registry: ElementRegistry | None) -> str | None:
    """Single ICL (image/caption/legend) dispatcher.

    Step 1: ``_is_icl_family`` runs the family-membership decision.
    Tables that fail the gate are NOT funneled to any ICL producer —
    they fall through to LAYOUT_WRAPPER (verse/text wrappers) or to
    the non-ICL classifiers (DATA_TABLE, COMPLEX_HTML, math layouts).

    Step 2: once family-confirmed, dispatch by structural features
    that distinguish ICL sub-shapes from each other:

      * 0 images + ≥2 nested figure children → ``FIGURE_GROUP``.
      * ≥2 images                            → ``UNPAIRED_FIGURE_GROUP``
        (the producer bundles what pairs, passes through the rest; the
        old SIMPLE_PLATE / CAPTIONED_FIGURE_GRID split is gone).
      * 1 image + POEM/TABLE child           → ``LEGENDED_FIGURE_CHILD``.
      * 1 image + cell-based legend material → ``LEGENDED_FIGURE``.
      * 1 image + Fig./Plate. marker in image row
                                             → ``CAPTIONED_FIGURE_INLINE``.
      * 1 image, no special signal           → ``CAPTIONED_FIGURE``.

    Because family membership is decided FIRST, sub-shape predicates
    are free of data-table-defense gating (no ``||``-in-row-0
    rejection, no image-must-be-in-row-0).  Sibling-cell shapes —
    image + legend cell in row 0 (ABBEY Fig 1), image + POEM
    placeholder in row 0 (ABBEY Fig 12, BAG-PIPE) — route correctly
    because the family gate has already ruled out data tables.

    Returns the sub-label, or ``None`` if the table is not in the
    ICL family (caller falls through to non-ICL classifiers).
    """
    if not _is_icl_family(raw, inner, registry):
        return None

    assert registry is not None  # _is_icl_family rejects on None
    # Family-scoped normalization: strip pure-decoration layout
    # templates so the sub-shape detectors see clean content.  Runs
    # AFTER family confirmation, so it never touches a non-ICL table
    # that happens to contain `{{center}}` etc.
    inner = _normalize_icl_markup(inner)
    image_phs = [ph for ph, lbl in registry.labels.items()
                 if lbl == "IMAGE"]

    # No-image case: FIGURE_GROUP wraps ≥2 nested figure children.
    if not image_phs:
        figure_children = [ph for ph, lbl in registry.labels.items()
                            if lbl in _FIGURE_LABELS]
        if len(figure_children) >= 2:
            return "FIGURE_GROUP"
        # Family signal was figure-child-only, but only 1 child —
        # not a group.  Fall through to LAYOUT_WRAPPER.
        return None

    # Multi-image case: UNPAIRED_FIGURE_GROUP — ≥2 images that we don't
    # bundle per-image (the un-pairable multi-image figure; its intent is
    # "multiple images that can't be mapped 1:1 to captions/legends").
    # The old SIMPLE_PLATE / CAPTIONED_FIGURE_GRID plate-shaped labels are
    # gone: plates route to parse_plate, and per-image bundling of the
    # *pairable* multi-image case is deferred to the plate work (top-legend
    # + ICLs + group-legend + bottom-legend).  We return the label
    # DIRECTLY (not fall through) because the family gate has ALREADY
    # confirmed this is a figure — letting it reach the data-table
    # predicates would leak `{{small-caps|Fig.}}` + long-legend figures
    # like ARTHROPODA into COMPLEX_HTML.  Shares LAYOUT_WRAPPER's
    # passthrough producer.
    if len(image_phs) >= 2:
        return "UNPAIRED_FIGURE_GROUP"

    # Single-image case: dispatch by what neighbours the image.
    image_ph = image_phs[0]
    if _figure_has_child_legend(registry, image_phs):
        return "LEGENDED_FIGURE_CHILD"
    if _has_beside_legend_signal(inner, image_ph):
        return "LEGENDED_FIGURE_BESIDE"
    if _has_legend_material(inner, registry, image_phs):
        return "LEGENDED_FIGURE"
    if _has_inline_caption_signal(inner, image_ph):
        return "CAPTIONED_FIGURE_INLINE"
    # Plain CAPTIONED_FIGURE requires the image to be alone in its
    # row — caption / attribution material lives in subsequent rows.
    # Tables where the image shares its row with non-empty non-
    # legend siblings (e.g., ``<ref>`` footnote markers around an
    # inline musical-notation image — BAG-PIPE Fig 1) aren't really
    # captioned figures; they fall through to LAYOUT_WRAPPER which
    # passes the placeholders through without bundling them as a
    # caption.
    if _image_alone_in_row(inner, image_ph):
        return "CAPTIONED_FIGURE"
    return None


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
    cell parsing (Ts adds phantom pipes); needs HTML rendering.

    NOTE (#29): #28 made `_process_table` handle Ts cells, so re-routing the
    PLAIN no-span tables here to DATA_TABLE is the intended knock-out — but the
    attempt surfaced a pre-existing `_process_table` bug (it DROPS a sub-header
    row, e.g. AGRICULTURE's "Average Acreage … Whole Farm | Proportion" table).
    Re-route is parked until that DATA_TABLE row-drop is fixed."""
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


def _is_verse_table_pred(raw: str, inner: str,
                          registry: ElementRegistry | None) -> bool:
    """A 2-column quotation layout (hanging-quote col1 + verse lines).
    Content-recognition last resort — no structural signal separates verse
    from a 2-column data table.  Placed near the end (before single-col and
    the DATA_TABLE catch-all): only re-labels tables that would otherwise be
    DATA_TABLE."""
    return _is_verse_table(inner)


def _is_single_column_table_pred(raw: str, inner: str,
                                  registry: ElementRegistry | None) -> bool:
    """A `{|…|}` whose every non-empty row holds exactly one content
    cell — a boxed/centred run of text, not a data grid.  Its producer
    renders it as a `«PRE:` block.

    Placed last (just before the DATA_TABLE catch-all) so it only ever
    re-labels tables that would otherwise fall through to DATA_TABLE; it
    never steals a table a more-specific predicate (layout/math/complex)
    already claimed.  This is the first shape lifted out of
    `_process_table`'s hidden dispatch into the classifier."""
    return _is_single_column_table(inner)


def _always_true(raw: str, inner: str,
                  registry: ElementRegistry | None) -> bool:
    """Catch-all — anything reaching here is a regular DATA_TABLE."""
    return True


# Two priority lists with the ICL family dispatch in between.  This
# layout makes "is this an ICL?" a single explicit step rather than
# a question dispersed across multiple independent predicates:
#
#   1. PRE_ICL — more-specific structural shapes that should take
#      precedence even over ICL family membership (DJVU crops have
#      ICL-shaped wikitext but their producer parses the raw bytes;
#      compound and chemistry layouts have data-table signals that
#      _is_icl_family would also reject, but they need their own
#      producers, not LAYOUT_WRAPPER).
#   2. ICL family dispatch — `_classify_icl_shape` runs the
#      `_is_icl_family` gate and, on success, returns one of the
#      ICL sub-labels.  On failure (not family) it returns None and
#      the table falls through to the post-ICL list.
#   3. POST_ICL — LAYOUT_WRAPPER (catch-all for residual layout
#      shapes including verse/text wrappers and ICL shapes the
#      dispatcher returned None for), then the data/math
#      classifications.
_PRE_ICL_PREDS: list[tuple[Callable[
    [str, str, "ElementRegistry | None"], bool], str]] = [
    (_is_djvu_crop_table,        "DJVU_CROP"),
    (_is_compound_table_pred,    "COMPOUND_TABLE"),
    (_is_chemistry_layout_pred,  "CHEMISTRY_LAYOUT"),
]


_POST_ICL_PREDS: list[tuple[Callable[
    [str, str, "ElementRegistry | None"], bool], str]] = [
    (_is_layout_wrapper_pred,        "LAYOUT_WRAPPER"),
    (_is_brace_table,                "DATA_TABLE"),
    (_is_math_dominant_pred,         "MATH_LAYOUT_EQUATIONS"),
    (_has_data_signal_and_ts,        "COMPLEX_HTML"),
    (_has_rowspan_or_colspan,        "COMPLEX_HTML"),
    (_is_math_layout_tokens_pred,    "MATH_LAYOUT_TOKENS"),
    (_is_math_layout_equations_pred, "MATH_LAYOUT_EQUATIONS"),
    (_is_verse_table_pred,           "VERSE_TABLE"),
    (_is_single_column_table_pred,   "SINGLE_COLUMN_TABLE"),
    (_always_true,                   "DATA_TABLE"),
]


def _classify_table(raw: str, inner: str,
                     inner_registry: ElementRegistry | None) -> str:
    """Classify a wiki table to its producer-dispatch label.

    Three-stage dispatch:

      1. Pre-ICL predicates (DJVU crops, compound tables, chemistry
         layouts) get priority over generic figure classification.
      2. `_classify_icl_shape` decides ICL family membership in one
         place and returns a sub-label when it applies; otherwise
         returns None so the table falls through.
      3. Post-ICL predicates handle LAYOUT_WRAPPER (catch-all),
         data-table variants, and math layouts.

    Adding a new wikitable kind: add a predicate + entry to the
    appropriate list, or add a new sub-shape to `_classify_icl_shape`
    if it's an ICL variant.  Non-ICL tables can never receive an
    ICL label because the family gate is the only path to one.
    """
    for predicate, label in _PRE_ICL_PREDS:
        if predicate(raw, inner, inner_registry):
            return label
    icl_label = _classify_icl_shape(raw, inner, inner_registry)
    if icl_label is not None:
        return icl_label
    for predicate, label in _POST_ICL_PREDS:
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

def process_elements(text: str, text_transform, context: ElementContext,
                     _allow_figure: bool = True) -> str:
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
        substitute_top_level_markers,
    )
    from britannica.pipeline.stages.elements._ref import resolve_ref_bodies

    # Walk + classify (one mutually-recursive pass).  Returns the
    # placeholderized article body plus a tree of ClassifiedElement
    # records — each element knows its own label, raw bytes, inner
    # text, and inner registry of classified children.
    placeholderized_text, tree = classify_article(
        text, _allow_figure=_allow_figure)

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
