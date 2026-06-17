"""Brace producer — `{{brace2|N|dir}}`, a row-spanning curly brace.

EB1911 tables group rows with a large curly brace, written in the source with the
Wikisource `{{brace2|height|side}}` template: `height` is the number of rows the
brace spans, `side` is `l` (a `{` whose point faces LEFT, grouping the cells to
its right) or `r` (a `}` facing RIGHT); `l` is the default.  The brace is EB1911
presentation we CARRY — render the glyph, stretched to its row span — not
editorial furniture to drop, and certainly not its raw `N|dir` arguments to leak
(4012 of them across 440 pages did exactly that).
"""
from __future__ import annotations


def process_brace(raw: str) -> str:
    """Render `{{brace2|N|dir}}` as a vertically-stretched `{` / `}` glyph."""
    parts = [p.strip() for p in raw.strip().strip("{}").split("|")][1:]
    rows = next((int(p) for p in parts if p.isdigit()), 2)
    side = next((p.lower() for p in parts if p.lower() in ("l", "r")), "l")
    glyph = "}" if side == "r" else "{"
    # The brace lives in a row-spanning cell; stretch the glyph to the span so it
    # reads as one tall brace rather than a single-line bracket.
    return (f"«SPAN[style:display:inline-block; transform:scaleY({rows}); "
            f"transform-origin:center]»{glyph}«/SPAN»")
