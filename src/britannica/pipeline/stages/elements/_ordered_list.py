"""Parsers for the two DELIMITED list forms — `{{ordered list|…}}` and `<ol>`.

`{{ordered list|type=…|item|item|{{ordered list|…}}|…}}` is a Wikisource nested
numbered-list macro.  Its only corpus use is GEOGRAPHY (vol 11)'s "Richthofen's
Classification of Mountains" — a 4-level taxonomy (upper-roman → lower-alpha →
decimal → lower-roman), each item a «I»German term«/I»—English gloss, with a
sub-list folded into its parent item's arg after a newline.

Both state a LIST outright, so both are carried as one (`_list.py`): these
functions only parse an already-bounded raw into `(depth, text)` rows plus the
list's stated kind/`type=`.  The classifier (`_classify_list`) rebuilds the
source's own nesting from the depths; the render numbers the items.
"""
from __future__ import annotations

import re

from britannica.pipeline.stages.elements._dual_line import _split_top_level_pipe
from britannica.pipeline.stages.elements._list import ListRow

_OL_OPEN = re.compile(r"\{\{\s*ordered\s+list\b", re.IGNORECASE)
_TYPE_ARG = re.compile(r"^\s*type\s*=\s*([\w-]+)\s*$", re.IGNORECASE)


def _balanced_end(text: str, start: int) -> int:
    """Index one past the `}}` that balances the `{{` at ``start``."""
    depth, i, n = 0, start, len(text)
    while i < n - 1:
        two = text[i:i + 2]
        if two == "{{":
            depth += 1
            i += 2
        elif two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return n


def _walk(block: str, depth: int, out: "list[ListRow]") -> None:
    """Recursively emit one `ListRow` per item of a `{{ordered list|…}}` block.

    Each row carries the `type=` of the list it belongs to, so a nested list
    keeps ITS OWN numbering — GEOGRAPHY states one at all four levels.

    The numeral is NOT minted into the text: `type=` is a statement about how the
    list numbers itself, so it is carried to the render as the list's own
    property and the browser numbers the items ([[project_outline_arc]]).  Minting
    it made the numeral prose — indistinguishable from content to the search
    index and the markdown export."""
    m = _OL_OPEN.match(block.strip())
    inner = block.strip()
    inner = inner[m.end():] if m else inner
    inner = inner.lstrip("|")
    if inner.endswith("}}"):
        inner = inner[:-2]
    ltype = ""
    items: list[str] = []
    for arg in _split_top_level_pipe(inner):
        tm = _TYPE_ARG.match(arg)
        if tm:
            ltype = tm.group(1).strip().lower()
            continue
        items.append(arg)
    for arg in items:
        nest = _OL_OPEN.search(arg)               # a sub-list folded into this arg
        if nest:
            text = arg[:nest.start()]
            nested = arg[nest.start():_balanced_end(arg, nest.start())]
        else:
            text, nested = arg, None
        text = text.strip()
        if text:
            out.append(ListRow(depth, text, "ol", ltype))
        if nested:
            _walk(nested, depth + 1, out)


# ── HTML-list form: `<ol style="list-style-type:…"><li>…</li>…</ol>` ──────────
#
# The HTML twin of `{{ordered list}}` (GEOLOGY's A/B/C sections, ALBUMIN's
# roman-numbered protein taxonomy) — the same LIST in another syntax, so
# `_walk_html_list` emits the same `(depth, text)` rows `_walk` does.
# Recognition is the walker's (a balanced `<ol>…</ol>` is a SHAPE_HTML_TAG
# element); this only parses the already-bounded raw into rows.
_HTML_LIST_OPEN = re.compile(r"<\s*(ol|ul)\b([^>]*)>", re.IGNORECASE)
_HTML_LIST_CLOSE = re.compile(r"</\s*(?:ol|ul)\s*>", re.IGNORECASE)
_LI_OPEN = re.compile(r"<\s*li\b[^>]*>", re.IGNORECASE)
_LI_CLOSE_RE = re.compile(r"</\s*li\s*>", re.IGNORECASE)
_LIST_STYLE_TYPE = re.compile(r"list-style-type\s*:\s*([\w-]+)", re.IGNORECASE)


def _html_list_end(text: str, start: int) -> int:
    """Index one past the `</ol>`/`</ul>` that balances the list opening at
    ``start`` — depth-counted so a nested list's close can't end the outer."""
    depth, i, n = 0, start, len(text)
    while i < n:
        mo = _HTML_LIST_OPEN.match(text, i)
        if mo:
            depth += 1
            i = mo.end()
            continue
        mc = _HTML_LIST_CLOSE.match(text, i)
        if mc:
            depth -= 1
            i = mc.end()
            if depth == 0:
                return i
            continue
        i += 1
    return n


def _split_top_li(inner: str) -> list[str]:
    """Chop a list's inner into one chunk per TOP-LEVEL `<li>` (each chunk runs to
    the next top-level `<li>` or the end).  A nested `<ol>`/`<ul>` is skipped whole,
    so its own `<li>`s never split the outer — the chunk carries the sublist along
    for the caller to recurse."""
    starts: list[int] = []
    i, n = 0, len(inner)
    while i < n:
        mo = _HTML_LIST_OPEN.match(inner, i)
        if mo:                                   # skip a nested list whole
            i = _html_list_end(inner, i)
            continue
        mli = _LI_OPEN.match(inner, i)
        if mli:
            starts.append(i)
            i = mli.end()
            continue
        i += 1
    return [inner[s:(starts[k + 1] if k + 1 < len(starts) else n)]
            for k, s in enumerate(starts)]


def _walk_html_list(block: str, depth: int, out: "list[ListRow]") -> None:
    """Emit one `ListRow` per `<li>` of an `<ol>`/`<ul>` block — the HTML twin of
    `_walk`.  Each row carries ITS OWN list's kind and `list-style-type`, so a
    nested list keeps the numbering the source gave it.

    Neither the numeral nor the bullet is minted into the text: `<ol>` vs `<ul>`
    and `list-style-type` are the source's statements about how the list marks
    itself, carried to the render as the list's own properties.  Each `<li>`'s
    text is one row — recursed downstream, so an item's `«I»`/`{{sub}}`/nested
    markup stays a real child — and a nested list recurses at ``depth+1``."""
    block = block.strip()
    mo = _HTML_LIST_OPEN.match(block)
    if not mo:
        return
    kind = mo.group(1).lower()
    stm = _LIST_STYLE_TYPE.search(mo.group(2))
    ltype = stm.group(1).strip().lower() if stm else ""
    inner = block[mo.end():]
    last = None                                  # drop the outer close (the LAST list-close)
    for last in _HTML_LIST_CLOSE.finditer(inner):
        pass
    if last:
        inner = inner[:last.start()]
    for chunk in _split_top_li(inner):
        m = _LI_OPEN.match(chunk)
        body = chunk[m.end():] if m else chunk
        nest_at = None                           # a sublist folded into this item
        i, L = 0, len(body)
        while i < L:
            if _HTML_LIST_OPEN.match(body, i):
                nest_at = i
                break
            i += 1
        text = body[:nest_at] if nest_at is not None else body
        text = _LI_CLOSE_RE.sub("", text).strip()
        if text:
            out.append(ListRow(depth, text, kind, ltype))
        if nest_at is not None:
            _walk_html_list(body[nest_at:_html_list_end(body, nest_at)],
                            depth + 1, out)


