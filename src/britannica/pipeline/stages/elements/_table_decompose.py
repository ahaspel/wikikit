"""Canonical recursive table decomposition: table → rows → cells → body-text.

The architectural principle (per
[[feedback_table_decomposes_recursively]]):

  * **Table** extracts ROWS.  Peels `{|…|}` (wiki) or `<table>…</table>`
    (HTML) plus the opener-attribute slot.  Returns the canonical row
    list.
  * **Row** extracts CELLS.  Peels `|-…` / `<tr …>` plus its attribute
    slot.  Returns the canonical cell list.
  * **Cell** produces its own CONTENT via `text_transform` (i.e.
    body-text, the same `_apply_markup` that processes article prose).
    Its attribute slot feeds `_cell_styles` and is consumed entirely.

Cell content reaches body-text in a context indistinguishable from a
paragraph in prose — same handlers, same totality argument.  There is no
"table context" or "cell context" mode; the inner template handlers
(`_convert_sc`, `_convert_sub_sup`, `_convert_foreign_script`, …) apply
uniformly.

This module provides shape-agnostic infrastructure.  Producers (single
data-table producer, HTML-table producer, special-shape producers in
the layout / chemistry / math families) pick the appropriate ROW
EXTRACTOR for their source flavor, then dispatch through the uniform
cell-produce + assemble pipeline below.

Status: Step 1 — additive infrastructure, no existing producers
migrated yet (they continue using their inline logic).  Step 2 migrates
`_process_html_table` to the canonical path; subsequent steps the wiki
and special-shape producers.
"""
from __future__ import annotations

import re
from typing import Callable

TextTransform = Callable[[str], str]

# A nested-table producer: raw ``{|…|}`` bytes -> produced marker string.
# Passed into :func:`produce_cell` by a caller that owns recursion; the
# cell extractor hands it each raw nested table it finds in cell content.
# See the module note on producer-owned recursion below.
NestedTableProducer = Callable[[str], str]


# ── Canonical data shapes ───────────────────────────────────────────────
#
# A `Cell` is `(sep, attr_part, content)`:
#   * `sep` — `'|'` for data cells (wiki `|`/`||`, HTML `<td>`) or `'!'`
#     for header cells (wiki `!`/`!!`, HTML `<th>`).
#   * `attr_part` — raw attribute prefix (wiki `colspan=2 {{Ts|ac}}` or
#     HTML `colspan="2" style="text-align:center"`).  Consumed entirely
#     by `_cell_styles`; nothing here reaches `text_transform`.
#   * `content` — raw cell body, un-transformed.  Fed to `text_transform`
#     by `produce_cell` below.
#
# A `Row` is `(row_attr_part, list[Cell])`:
#   * `row_attr_part` — wiki `|-<attrs>` tail or HTML `<tr <attrs>>`
#     attribute string.  Today the canonical marker format carries no
#     row-level style slot; we extract it so the data is available for
#     future marker-format extension without re-touching the extractors.

Cell = tuple[str, str, str]
Row = tuple[str, list[Cell]]


# ── Nested-table recognition (producer-owned recursion) ─────────────────
#
# The flat-walker contract: the walker bounds only the OUTERMOST shapes
# and never descends into element bodies; each producer owns its own
# recursion privately.  For tables that means the cell extractor must
# recognize a raw nested ``{|…|}`` sitting in cell content and recurse on
# it, rather than relying on the walker to have lifted it into a
# placeholder + ``inner_registry`` first.
#
# THIS PATH IS DORMANT TODAY.  The walker still placeholderizes nested
# tables during classification, so in production a cell's content carries
# a ``\x03ELEM:N\x03`` placeholder, never raw ``{|`` bytes — the masking
# below matches nothing and :func:`produce_cell`'s recursion (opt-in via
# ``recurse``) is never wired by a caller.  It wakes up only when the
# walker stops descending into ``SHAPE_BRACE_PIPE`` (the eventual flip),
# at which point a nested table reaches the cell raw and the producer
# recurses.  Until then it is exercised only by direct-feed unit tests.

# Nested-table mask token: ``\x03``-delimited so ``split_wiki_row``'s
# existing placeholder protection treats it as opaque content (it carries
# no ``|`` / newline, so the row/cell splitters can't fragment it).  The
# ``NT`` infix keeps it distinct from the walker's ``ELEM:`` placeholders.
_NESTED_SENTINEL = "\x03"


def _nested_token(i: int) -> str:
    return f"{_NESTED_SENTINEL}NT{i}{_NESTED_SENTINEL}"


def find_nested_table_spans(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` for each top-level balanced ``{|…|}`` span.

    Depth-counted over ``{|`` / ``|}`` token pairs, so a table nested
    inside a nested table is wholly contained within the OUTER span (the
    outer span alone is returned; whatever produces that span finds the
    inner one on its own next descent — recursion at the right layer).
    A degenerate unclosed ``{|`` runs the span to end-of-string.
    """
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("{|", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("{|", j):
                    depth += 1
                    j += 2
                elif text.startswith("|}", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


def _mask_nested_tables(text: str) -> tuple[str, list[str]]:
    """Replace each top-level balanced ``{|…|}`` with an opaque one-line
    token.  Returns ``(masked_text, raw_spans)`` where ``raw_spans[i]`` is
    the original bytes for ``_nested_token(i)``.  No-op (returns the input
    and an empty list) when there is no nested table — the production case.
    """
    spans = find_nested_table_spans(text)
    if not spans:
        return text, []
    raw_spans: list[str] = []
    out: list[str] = []
    last = 0
    for start, end in spans:
        out.append(text[last:start])
        out.append(_nested_token(len(raw_spans)))
        raw_spans.append(text[start:end])
        last = end
    out.append(text[last:])
    return "".join(out), raw_spans


def _restore_nested(text: str, raw_spans: list[str]) -> str:
    for i, raw in enumerate(raw_spans):
        text = text.replace(_nested_token(i), raw)
    return text


# ── Wiki-side row extractor ─────────────────────────────────────────────

_WIKI_ROW_SEP_RE = re.compile(r"(?:^|\n)\s*\|-([^\n]*)")
_WIKI_CAPTION_RE = re.compile(r"(?:^|\n)\s*\|\+\s*([^\n]+)")


def extract_wiki_rows(inner: str) -> tuple[str, list[Row]]:
    """Decompose a wikitable's inner text into `(caption, rows)`.

    `inner` is the source between the outer `{|<attrs>` and `|}`
    delimiters (the walker has already bounded these).  Caption is the
    `|+<text>` line if present (returned un-transformed; the caller runs
    it through `text_transform`).  Rows are produced by splitting on
    `|-<attrs>` separators; each row's cells come from
    :func:`split_wiki_row`.

    The pre-`|-` segment is included as a row only when it contains
    cell content (`|`/`!`-anchored lines).  A bare preamble (caption +
    blank space) becomes the empty rows list with the caption surfaced.
    """
    from britannica.pipeline.stages.elements._tables import split_wiki_row

    # Protect balanced nested ``{|…|}`` spans before row-splitting: the
    # outer ``|-`` row key and ``|+`` caption key would otherwise match a
    # nested table's own rows and fragment it.  Each span becomes one
    # opaque token, restored into the owning cell after splitting.  In
    # production ``masked == inner`` (no raw ``{|`` — see module note), so
    # this is byte-identical to the un-masked path.
    masked, raw_spans = _mask_nested_tables(inner)

    cap_m = _WIKI_CAPTION_RE.search(masked)
    caption = cap_m.group(1).strip() if cap_m else ""

    pieces = _WIKI_ROW_SEP_RE.split(masked)
    rows: list[Row] = []

    if len(pieces) == 1:
        cells = split_wiki_row(_drop_caption_lines(pieces[0]))
        if cells:
            rows.append(("", cells))
    else:
        preamble = _drop_caption_lines(pieces[0])
        if _has_cell_lines(preamble):
            cells = split_wiki_row(preamble)
            if cells:
                rows.append(("", cells))
        for i in range(1, len(pieces), 2):
            row_attrs = pieces[i].strip()
            body = pieces[i + 1] if i + 1 < len(pieces) else ""
            cells = split_wiki_row(_drop_caption_lines(body))
            if cells:
                rows.append((row_attrs, cells))

    if raw_spans:
        caption = _restore_nested(caption, raw_spans)
        rows = [
            (attrs, [
                (sep, attr, _restore_nested(content, raw_spans))
                for sep, attr, content in cells
            ])
            for attrs, cells in rows
        ]
    return caption, rows


def _drop_caption_lines(text: str) -> str:
    return re.sub(r"(?:^|\n)\s*\|\+[^\n]*", "", text)


def _has_cell_lines(text: str) -> bool:
    for ln in text.split("\n"):
        s = ln.lstrip()
        if s.startswith(("|", "!")) and s.strip() not in ("|", "!", "{|"):
            return True
    return False


# ── HTML-side row extractor ────────────────────────────────────────────

_HTML_TR_RE = re.compile(r"<tr([^>]*)>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_HTML_CELL_RE = re.compile(
    r"<(t[dh])([^>]*)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_HTML_CAPTION_RE = re.compile(
    r"<caption[^>]*>(.*?)</caption>", re.DOTALL | re.IGNORECASE)


def extract_html_rows(inner: str) -> tuple[str, list[Row]]:
    """Decompose an HTML table's inner content into `(caption, rows)`.

    `inner` is the source between `<table>` and `</table>` (walker-
    bounded).  Caption is the `<caption>…</caption>` element if present.
    Rows come from `<tr>` matches; cells from `<td>`/`<th>` inside each
    row.  Tables without explicit `<tr>` wrappers (HYDRAULICS-style
    `<table><td>…</td></table>`) become a single synthetic row.
    """
    cap_m = _HTML_CAPTION_RE.search(inner)
    caption = cap_m.group(1).strip() if cap_m else ""

    rows: list[Row] = []
    if _HTML_TR_RE.search(inner):
        for m in _HTML_TR_RE.finditer(inner):
            row_attrs = m.group(1).strip()
            cells = _html_cells(m.group(2))
            if cells:
                rows.append((row_attrs, cells))
    else:
        cells = _html_cells(inner)
        if cells:
            rows.append(("", cells))
    return caption, rows


def _html_cells(body: str) -> list[Cell]:
    cells: list[Cell] = []
    for m in _HTML_CELL_RE.finditer(body):
        tag = m.group(1).lower()
        attrs = m.group(2).strip()
        content = m.group(3)
        sep = "!" if tag == "th" else "|"
        cells.append((sep, attrs, content))
    return cells


# ── Cell producer ──────────────────────────────────────────────────────

def produce_cell(
    attr_part: str, content: str, text_transform: TextTransform,
    recurse: NestedTableProducer | None = None,
) -> tuple[list[str], str]:
    """Produce one cell: extract its styles, run its content through
    body-text.  Returns `(styles, body)`.

    The cell's `attr_part` is consumed entirely by `_cell_styles` —
    `{{Ts|…}}` codes, inline `style="…"`, `align="…"`/`valign="…"` all
    become CSS declarations.  Nothing from `attr_part` reaches
    `text_transform`.

    `content` is the cell body, possibly containing inline templates
    (`{{sc|…}}`, `{{sup|…}}`, foreign-script wrappers, …) and inline
    HTML (`<span>`, `<i>`, etc.).  These are body-text's concern;
    `text_transform` handles them uniformly with article prose.

    ``recurse`` enables producer-owned recursion: when supplied AND the
    content carries a raw nested ``{|…|}`` table, each nested table is
    masked out, the surrounding content is run through body-text, then
    each nested table's marker — produced by ``recurse(raw)`` — is
    substituted back in.  Masking keeps the marker clear of body-text's
    template/markup handling.  ``recurse=None`` (the default, and every
    production caller today) leaves content untouched; combined with the
    fact that production cell content never carries raw ``{|`` (it's a
    placeholder — see module note), this path is dormant until the flip.
    """
    from britannica.pipeline.stages.elements._tables import _cell_styles
    styles = _cell_styles(attr_part, content)
    if not content:
        return styles, ""
    raw_spans: list[str] = []
    if recurse is not None and "{|" in content:
        content, raw_spans = _mask_nested_tables(content)
    body = text_transform(content).strip(" \t")
    for i, raw in enumerate(raw_spans):
        body = body.replace(_nested_token(i), recurse(raw))
    return styles, body


# ── Assemblers ─────────────────────────────────────────────────────────

def assemble_wiki_marker(
    produced_rows: list[tuple[str, list[tuple[list[str], str]]]],
    caption: str,
    header: bool,
    table_styles: list[str],
) -> str:
    """Compose canonical `{{TABLE…:…}TABLE}` (or `{{TABLEH…:…}TABLE}`)
    marker output from the per-row produced cells.

    `produced_rows`: list of `(row_attr_part, list[(cell_styles,
    cell_body)])` — `row_attr_part` is preserved for future row-style
    carriage in the marker (today's format has no row-style slot, so it
    is currently dropped).  Each cell becomes one
    :func:`build_table_cell` token in the row string.

    `caption` is transformed-ready text or empty.  `header=True` selects
    the `TABLEH` variant.  `table_styles` populate the `[style:…]` slot.

    Style → alignment narrowing: the wiki marker format encodes only
    `align` (no full CSS per cell).  We extract the alignment from the
    cell-style list and pass it as `align=`; the rest of the styles
    list is currently dropped at the marker boundary (the renderer can
    only honour what the format carries).  HTML emission uses the full
    styles list via :func:`assemble_html_rows`.
    """
    from britannica.markers import build_table_cell
    from britannica.pipeline.stages.elements._tables import _emit_table_marker

    text_rows: list[str] = []
    if caption:
        text_rows.append("⟦+⟧" + caption)
    for _row_attrs, cells in produced_rows:
        text_rows.append(" | ".join(
            build_table_cell(body, align=_align_of(styles))
            for styles, body in cells
        ))
    return _emit_table_marker(text_rows, header=header, styles=table_styles)


def _align_of(styles: list[str]) -> str | None:
    for r in styles:
        if r.startswith("text-align:"):
            v = r.split(":", 1)[1].strip()
            return v if v in ("right", "center", "left") else None
    return None
