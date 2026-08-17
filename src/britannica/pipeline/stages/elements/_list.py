"""Lists — what the source marks as a list, carried as a list.

THREE spellings, ONE shape.  The raw states a list three ways and we carry all
three the same: `#` (wikitext ordered list, 79 lines / 13 articles), `<ol>`/`<ul>`
(ALBUMIN, GEOLOGY) and `{{ordered list|type=…}}` (GEOGRAPHY).  Each keeps its own
NESTING (a sublist rides inside its parent item, as the source writes it) and its
own NUMBERING (carried as `type`, rendered by the browser).

WHAT THIS REPLACES.  All three used to be funnelled into the OUTLINE decomposer:
`(depth, "label. text")` rows, densified back into nested `<ul class="outline">`
with `list-style-type: none` — so a source-stated NUMBERED list shipped
unnumbered with "I. "/"A. " minted into the item TEXT as characters.  The
numerals are in the scans (user-checked: GEOLOGY p658, ALBUMIN p553), so they are
the source's; minting them as prose put them beyond the reach of the search
index, the markdown export and any restyling, while the list itself lost the one
thing that made it a list.

The `#` scan is a line pre-pass, like `_indent.py`'s: a run of consecutive
`#`-marked lines is one list, and the MARK COUNT is the nesting depth (`##` is a
sublist of the `#` above it) — the source's statement, not an inferred profile.
An OCR line like TOOL's `#4 ' '* ' ' ' n` is a one-item list of garbage, which is
a faithful render of garbage ([[feedback_viewer_not_source_errors]]).
"""
from __future__ import annotations

import re
from typing import NamedTuple

from britannica.markers import ol_open
from britannica.pipeline.stages.elements._registry import (
    ElementRegistry, substitute_children)


class ListRow(NamedTuple):
    """One item, plus what the list CONTAINING it states about itself.

    `kind`/`ltype` ride on every row rather than being returned once for the
    root, because a list states its numbering at EVERY level: GEOGRAPHY nests
    upper-roman → lower-alpha → decimal → lower-roman, and a single root-level
    answer prints `1.` where the source says `a.`
    """
    depth: int
    text: str
    kind: str = "ol"        # "ol" | "ul"
    ltype: str = ""         # the stated `list-style-type`; "" = the default


# A `#`-marked line: the run of `#` is the depth, the rest is the item.
HASH_ITEM_RE = re.compile(r"^(#+)\s*(.*)$")


def is_hash_line(line: str) -> bool:
    return HASH_ITEM_RE.match(line) is not None and bool(line.strip("#").strip())


def extract_hash_lists(text: str, registry: ElementRegistry) -> str:
    """Replace each run of `#`-marked lines with a LIST placeholder, copying
    every other byte of ``text`` VERBATIM."""
    from britannica.pipeline.stages.elements._indent import _logical_line_end
    out: list[str] = []
    n = len(text)
    pos = 0
    while pos < n:
        le = _logical_line_end(text, pos)
        if is_hash_line(text[pos:le]):
            end = le
            p = le + 1 if le < n else n
            while p < n:                      # the run: consecutive `#` lines
                le2 = _logical_line_end(text, p)
                if not is_hash_line(text[p:le2]):
                    break
                end = le2
                p = le2 + 1 if le2 < n else n
            out.append(registry.add("LIST", text[pos:end]))
            pos = end
            continue
        seg_end = le + 1 if le < n else n
        out.append(text[pos:seg_end])
        pos = seg_end
    return "".join(out)


def hash_items(block: str) -> list[ListRow]:
    """One `ListRow` per `#`-marked line — depth = the mark count.

    Wikitext `#` states an ordered list and nothing about its numbering, so the
    type is the default at every level."""
    items: list[ListRow] = []
    for line in block.split("\n"):
        m = HASH_ITEM_RE.match(line)
        if m and m.group(2).strip():
            items.append(ListRow(len(m.group(1)), m.group(2).strip()))
    return items


def process_list(raw, inner, context, inner_registry) -> str:
    """LIST producer — the open/close pair around already-produced items.

    The list's own `type` rides on the classified node (`raw` holds it), so the
    producer states only what the source stated.  Empty list renders to nothing.
    """
    body = substitute_children(inner, inner_registry).strip()
    if not body:
        return ""
    kind, _, ltype = (raw or "ol").partition(":")
    if kind == "ul":
        return f"«UL»{body}«/UL»"
    return f"{ol_open(ltype)}{body}«/OL»"


def process_list_item(raw, inner, context, inner_registry) -> str:
    """LIST_ITEM producer — one `«LI»`, with any sublist already inside it."""
    body = substitute_children(inner, inner_registry).strip()
    if not body:
        return ""
    return f"«LI»{body}«/LI»"
