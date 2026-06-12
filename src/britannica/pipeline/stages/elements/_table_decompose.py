"""Table leaf: recurse the grid to the ground and emit, in one descent.

`recognize_table` (in `_table_fold`) chops table → rows → cells — that's the
recognition.  Everything past it is recursion: each cell's content recurses to
the leaf through the ONE `process_elements` dispatch — content-agnostic, the
cell is prose in a box, and whatever's inside (an image, a nested table, a
footnote) is its own producer's problem.  `recurse(content)` comes back
finished, so we wrap it and move on — no parsed-structure intermediate to hold.

Every structural level wraps its recursed inner in a tag whose raw attr-slot
folds AT the wrap (`_tag` → `fold_cell_attrs`): transform-outer + recurse-inner,
top to bottom, nothing materialised in between.

Also carries `split_wiki_rows_raw`, a `|-` row-splitter used by `_tables`' chem
`_table_grid` (the table producer proper chops via `recognize_table`).
"""
from __future__ import annotations

import re
from typing import Callable

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
    """
    parts = _WIKI_ROW_SEP_RE.split(inner)  # [pre, attr1, body1, attr2, body2…]
    rows: list[tuple[str, str]] = [("", parts[0])]
    for k in range(1, len(parts), 2):
        body = parts[k + 1] if k + 1 < len(parts) else ""
        rows.append((parts[k], body))
    return rows


# ── Recurse → emit (the table leaf) ─────────────────────────────────────


def _tag(name: str, attr_slot: str, inner: str,
         *, table_level: bool = False) -> str:
    """Wrap ``inner`` in ``<name …>…</name>``, folding the RAW ``attr_slot``
    onto the tag at the wrap — the style bits become one ``style="…"``, every
    other attribute (``colspan``, ``class``, ``cellpadding``…) a real HTML attr
    (`fold_cell_attrs`).  The one tag-emitter for every table level (`td`/`th`/
    `tr`/`table`); the renderer owns the attr split, made where the tag is
    built — never threaded down from the chop."""
    from britannica.pipeline.stages.elements._table_fold import (
        fold_cell_attrs, format_html_attrs)
    css, html = fold_cell_attrs(attr_slot, table_level)
    style = f' style="{";".join(css)}"' if css else ""
    return f"<{name}{format_html_attrs(html)}{style}>{inner}</{name}>"


def produce_table(inner: str, recurse: "Callable[[str], str]") -> tuple[str, str]:
    """Recurse a table to the ground and emit, in one descent.

    Returns ``(caption, «HTMLTABLE:<table>…</table>«/HTMLTABLE»)``; the caller
    stamps the opener attrs onto the bare ``<table>`` and recurses the caption.

    Each cell's content recurses through ``recurse`` to the leaf, then wraps in
    its ``<td>``/``<th>``; each row wraps its cells in ``<tr>``.  `_tag` folds
    the raw attr-slot at every wrap.  No intermediate: ``recurse`` already
    returns the finished cell body, so the table is just recognition + glue.
    """
    from britannica.pipeline.stages.elements._table_fold import recognize_table
    caption, rows = recognize_table(inner)
    out: list[str] = []
    for row_attr, cells in rows:
        if not cells:
            continue
        cells_html = "".join(
            _tag("th" if sep == "!" else "td", attr,
                 recurse(content).strip(" \t"))
            for sep, attr, content in cells)
        out.append(_tag("tr", row_attr, cells_html))
    if not out:
        return caption, ""
    return caption, "«HTMLTABLE:<table>" + "".join(out) + "</table>«/HTMLTABLE»"
