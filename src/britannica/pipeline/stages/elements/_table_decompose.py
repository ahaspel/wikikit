"""Table tag-emit + the chem row-splitter — the two utilities left now that the
table is a COMPOSITE.

`_tag` wraps one structural level's recursed inner in its `<td>`/`<th>`/`<tr>`/`<table>`
tag, folding the raw attr-slot AT the wrap (`fold_cell_attrs`).  The composite's
producers (ROW / TD / TH in `_PRODUCER_DISPATCH`) call it — one tag per level.

`split_wiki_rows_raw` is a `|-` row-splitter used by `_tables`' chem `_table_grid`.

Recognition (table → rows → cells) lives in `_table_fold.recognize_table`, called at
CLASSIFY time by `_classify_table_composite`; the old produce-time `produce_table`
assembler this module used to carry is GONE — the composite's TD/TH/ROW/TABLE
producers do that job now, one per structural level.
"""
from __future__ import annotations

import re

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


# ── Tag emitter (one per table structural level: td / th / tr / table) ──


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
