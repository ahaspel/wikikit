"""Recognizer for `{{ordered list|…}}` — a degenerate OUTLINE.

`{{ordered list|type=…|item|item|{{ordered list|…}}|…}}` is a Wikisource nested
numbered-list macro.  Its only corpus use is GEOGRAPHY (vol 11)'s "Richthofen's
Classification of Mountains" — a 4-level taxonomy (upper-roman → lower-alpha →
decimal → lower-roman), each item a «I»German term«/I»—English gloss, with a
sub-list folded into its parent item's arg after a newline.

An ordered list IS an outline: the same nested-item structure, recognized by an
explicit `{{…}}` delimiter instead of `:`-indent, its items pre-labelled with the
`type=` numbering (I/II, a/b, i/ii, 1/2).  `_walk` parses the raw template into the
same `(depth, "label. text")` rows a `:`-block yields; the classifier
(`_classify_ordered_list_composite`) runs them through the ONE outline decomposer,
so it produces `«OUTLINE»«OLI»` and renders through `build_outline_ul` like any
other outline — no separate producer, no separate marker format.
"""
from __future__ import annotations

import re

from britannica.pipeline.stages.elements._dual_line import _split_top_level_pipe

_OL_OPEN = re.compile(r"\{\{\s*ordered\s+list\b", re.IGNORECASE)
_TYPE_ARG = re.compile(r"^\s*type\s*=\s*([\w-]+)\s*$", re.IGNORECASE)

# `type=` → (numbering kind, uppercase?).  Default (no/unknown type) is decimal.
_OL_TYPE: dict[str, tuple[str, bool]] = {
    "upper-roman": ("roman", True), "lower-roman": ("roman", False),
    "upper-alpha": ("alpha", True), "lower-alpha": ("alpha", False),
    "upper-latin": ("alpha", True), "lower-latin": ("alpha", False),
    "decimal": ("decimal", False),
}

_ROMAN = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"),
          (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"),
          (5, "v"), (4, "iv"), (1, "i")]


def _to_roman(n: int) -> str:
    out = []
    for v, s in _ROMAN:
        while n >= v:
            out.append(s)
            n -= v
    return "".join(out)


def _label(kind: str, upper: bool, n: int) -> str:
    if kind == "roman":
        s = _to_roman(n)
        return s.upper() if upper else s
    if kind == "alpha":
        c = chr(ord("a") + (n - 1) % 26)
        return c.upper() if upper else c
    return str(n)


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


def _walk(block: str, depth: int, out: list[tuple[int, str]]) -> None:
    """Recursively emit ``(depth, "label. text")`` rows for one
    `{{ordered list|…}}` block and its nested sub-lists."""
    m = _OL_OPEN.match(block.strip())
    inner = block.strip()
    inner = inner[m.end():] if m else inner
    inner = inner.lstrip("|")
    if inner.endswith("}}"):
        inner = inner[:-2]
    kind, upper = "decimal", False
    items: list[str] = []
    for arg in _split_top_level_pipe(inner):
        tm = _TYPE_ARG.match(arg)
        if tm:
            kind, upper = _OL_TYPE.get(tm.group(1).lower(), ("decimal", False))
            continue
        items.append(arg)
    n = 0
    for arg in items:
        nest = _OL_OPEN.search(arg)               # a sub-list folded into this arg
        if nest:
            text = arg[:nest.start()]
            nested = arg[nest.start():_balanced_end(arg, nest.start())]
        else:
            text, nested = arg, None
        text = text.strip()
        if text:
            n += 1
            out.append((depth, f"{_label(kind, upper, n)}. {text}"))
        if nested:
            _walk(nested, depth + 1, out)


# ── HTML-list form: `<ol style="list-style-type:…"><li>…</li>…</ol>` ──────────
#
# The HTML twin of `{{ordered list}}` (GEOLOGY's A/B/C sections, ALBUMIN's
# roman-numbered protein taxonomy).  Same target — the ONE outline decomposer —
# so `_walk_html_list` emits the SAME `(depth, "label. text")` rows `_walk`
# does.  Recognition is the walker's (a balanced `<ol>…</ol>` is a SHAPE_HTML_TAG
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


def _walk_html_list(block: str, depth: int, out: list[tuple[int, str]]) -> None:
    """Emit `(depth, "label. text")` rows for one `<ol>`/`<ul>` block and its nested
    lists — the HTML twin of `_walk`.  `list-style-type` sets the numbering (default
    decimal); a `<ul>` is unlabelled.  Each `<li>`'s own text is one row — recursed
    downstream by `_outline_items`, so an item's `«I»`/`{{sub}}`/nested markup stays a
    real child — and a nested list folded into the item recurses at ``depth+1``."""
    block = block.strip()
    mo = _HTML_LIST_OPEN.match(block)
    if not mo:
        return
    ordered = mo.group(1).lower() == "ol"
    kind, upper = "decimal", False
    stm = _LIST_STYLE_TYPE.search(mo.group(2))
    if stm:
        kind, upper = _OL_TYPE.get(stm.group(1).lower(), ("decimal", False))
    inner = block[mo.end():]
    last = None                                  # drop the outer close (the LAST list-close)
    for last in _HTML_LIST_CLOSE.finditer(inner):
        pass
    if last:
        inner = inner[:last.start()]
    n = 0
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
            n += 1
            label = f"{_label(kind, upper, n)}. " if ordered else ""
            out.append((depth, f"{label}{text}"))
        if nest_at is not None:
            _walk_html_list(body[nest_at:_html_list_end(body, nest_at)],
                            depth + 1, out)


