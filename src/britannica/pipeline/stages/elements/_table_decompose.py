"""Table decomposition leaf: produce rows → cells, reassemble the marker.

The chop — one nesting-aware recognizer for both `{|` and `<table>`
syntaxes — lives in `_table_fold.recognize_table`; the attribute slot is
recursed (not whitelisted) by `_table_fold.fold_cell_styles`.  This module
holds what wraps that chop:

  * `produce_table_rows` — `recognize_table` + per-cell span/header
    detection and the content recurse (`produce_cell`).
  * `produce_cell` — `fold_cell_styles` for the attr slot, `cell_recurse`
    (`process_elements`) for the body.  A cell is prose in a box: its
    content recurses through the ONE dispatch, each nested element handled
    by its own producer exactly as article prose — no "cell context" mode.
  * `assemble_html_rows` — reassemble `«HTMLTABLE:<table>…</table>«/HTMLTABLE»`.

It also carries `split_wiki_rows_raw`, a `|-` row-splitter used by `_tables`'
chem `_table_grid` (the table producer proper chops via `recognize_table`).
"""
from __future__ import annotations

import re
from typing import Callable

TextTransform = Callable[[str], str]


# ── Wiki row split (utility for `_tables`' chem `_table_grid`) ───────────

_WIKI_ROW_SEP_RE = re.compile(r"(?:^|\n)\s*\|-([^\n]*)")


def split_wiki_rows_raw(inner: str) -> list[tuple[str, str]]:
    r"""Split a wikitable's inner text into ``[(row_attr, raw_row_body)]`` on
    the canonical ``|-`` row separator.

    Line-anchored AND indent-tolerant (``(?:^|\n)\s*\|-``): it splits a
    ``|-`` (or ``  |-``) that begins a line and captures that line's tail as
    the row attribute, but does NOT split on a ``|-`` mid-content.  The
    pre-first-``|-`` segment is the first entry, with empty ``row_attr``.
    Caption (``|+``) lines are NOT dropped — the caller applies its own
    ``|+`` policy.

    A row-shaped view used by `_tables`' chem `_table_grid`; the table
    producer proper chops via `_table_fold.recognize_table`.
    """
    parts = _WIKI_ROW_SEP_RE.split(inner)  # [pre, attr1, body1, attr2, body2…]
    rows: list[tuple[str, str]] = [("", parts[0])]
    for k in range(1, len(parts), 2):
        body = parts[k + 1] if k + 1 < len(parts) else ""
        rows.append((parts[k], body))
    return rows


# ── Decompose → produce → assemble (the table leaf) ─────────────────────
#
# A `ParsedCell` is `(tag, rowspan, colspan, body, styles)`:
#   * `tag` — `'td'`/`'th'` (header-ness resolved from the cell's `sep`).
#   * `rowspan`/`colspan` — integer span counts (1 when absent).
#   * `body` — the produced cell content (recursed through the dispatch).
#   * `styles` — CSS declarations from `fold_cell_styles` (full per-cell list).

ParsedCell = tuple[str, int, int, str, list[str]]
ParsedRow = list[ParsedCell]

_ROWSPAN_RE = re.compile(r'rowspan\s*=\s*"?(\d+)"?', re.IGNORECASE)
_COLSPAN_RE = re.compile(r'colspan\s*=\s*"?(\d+)"?', re.IGNORECASE)


# A producer-supplied cell-body strategy: `(attr_part, content) ->
# (styles, body)`.  Lets a producer keep a genuinely irreducible per-cell
# branch (e.g. an image cell that becomes `{{IMG:…}}` and must skip the
# content recursion) while still sharing this one decomposition loop.
# Default `None` uses the canonical `produce_cell` (= `fold_cell_styles` +
# content recursion), the uniform leaf.
CellBody = Callable[[str, str], "tuple[list[str], str]"]

ProducedRow = tuple[str, ParsedRow]  # (row_attr_part, cells)


def produce_cell(
    attr_part: str, content: str,
    cell_recurse: "Callable[[str], str] | None" = None,
) -> tuple[list[str], str]:
    """Produce one cell: extract its styles, recurse its content through the
    dispatch.  Returns `(styles, body)`.

    The cell's `attr_part` is consumed entirely by `fold_cell_styles` —
    `{{Ts|…}}` codes, inline `style="…"`, `align=`/`valign=`/`width=`/… all
    recursed to CSS declarations, nothing dropped.  Nothing from `attr_part`
    reaches the content.

    `content` is the cell body.  With `cell_recurse` set (production) it
    recurses through `process_elements` — each inline template / styled
    wrapper / nested table handled by its OWN producer, exactly as article
    prose.  Without it, content passes through stripped of edge whitespace.
    """
    from britannica.pipeline.stages.elements._table_fold import fold_cell_styles
    styles = fold_cell_styles(attr_part)
    if not content:
        return styles, ""
    if cell_recurse is not None:
        return styles, cell_recurse(content).strip(" \t")
    return styles, content.strip(" \t")


def produce_table_rows(
    inner: str,
    *,
    cell_preclean: TextTransform | None = None,
    cell_body: CellBody | None = None,
    cell_recurse: "Callable[[str], str] | None" = None,
) -> tuple[str, list[ProducedRow], bool, bool]:
    """Decompose a table's inner source into produced rows — the single
    row/cell split + span/header detection loop every table producer shares.

    Returns `(caption, rows, has_header, has_span)` where `rows` is a list
    of `(row_attr_part, ParsedRow)`.  The chop is `recognize_table` (one
    nesting-aware recognizer, both syntaxes); this loop adds span/header
    detection and the per-cell produce.

    `cell_preclean`, when given, runs on each cell's raw content BEFORE the
    content recursion.  `cell_body`, when given, computes `(styles, body)`
    per cell instead of the default `produce_cell` path (see `CellBody`).
    """
    from britannica.pipeline.stages.elements._table_fold import recognize_table
    caption, rows = recognize_table(inner)

    has_header = False
    has_span = False
    produced: list[ProducedRow] = []
    for row_attr, cells in rows:
        parsed: ParsedRow = []
        for sep, cell_attrs, cell_content in cells:
            tag = "th" if sep == "!" else "td"
            if sep == "!":
                has_header = True
            rs = _ROWSPAN_RE.search(cell_attrs)
            cs = _COLSPAN_RE.search(cell_attrs)
            rowspan = int(rs.group(1)) if rs else 1
            colspan = int(cs.group(1)) if cs else 1
            if rowspan > 1 or colspan > 1:
                has_span = True
            if cell_body is not None:
                styles, body = cell_body(cell_attrs, cell_content)
            else:
                cleaned = (cell_preclean(cell_content) if cell_preclean
                           else cell_content)
                styles, body = produce_cell(
                    cell_attrs, cleaned, cell_recurse=cell_recurse)
            parsed.append((tag, rowspan, colspan, body, styles))
        if parsed:
            produced.append((row_attr, parsed))
    return caption, produced, has_header, has_span


def assemble_html_rows(parsed_rows: list[ProducedRow]) -> str:
    """Compose `«HTMLTABLE:<table>…</table>«/HTMLTABLE»` from parsed rows.

    Used when any cell carries a `rowspan`/`colspan` — the wiki
    `{{TABLE:}TABLE}` marker can carry only align + colspan, so spanned
    tables emit literal HTML with full per-cell `style="…"` preserved.

    The per-row attribute slot (`{|`'s `|-<attrs>` / `<tr ...>`) is carried
    onto `<tr style="…">` the same way each cell carries its own styling —
    `fold_cell_styles` parses the row blob (`{{Ts|…}}`, `style="…"`,
    align/valign) identically to a cell attr.
    """
    from britannica.pipeline.stages.elements._tables import emit_html_cell
    from britannica.pipeline.stages.elements._table_fold import fold_cell_styles
    html_rows: list[str] = []
    for row_attr, parsed in parsed_rows:
        cells_html = [
            emit_html_cell(tag, content,
                           rowspan=rowspan, colspan=colspan, styles=styles)
            for tag, rowspan, colspan, content, styles in parsed
        ]
        row_styles = fold_cell_styles(row_attr) if row_attr else []
        open_tr = (f'<tr style="{";".join(row_styles)}">'
                   if row_styles else "<tr>")
        html_rows.append(open_tr + "".join(cells_html) + "</tr>")
    return ("«HTMLTABLE:<table>" + "".join(html_rows) +
            "</table>«/HTMLTABLE»")
