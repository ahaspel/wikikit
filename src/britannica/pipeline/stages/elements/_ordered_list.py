"""Producer for the ORDERED_LIST element.

`{{ordered list|type=…|item|item|{{ordered list|…}}|…}}` is a Wikisource nested
numbered-list macro.  Its only corpus use is GEOGRAPHY (vol 11)'s "Richthofen's
Classification of Mountains" — a 4-level taxonomy (upper-roman → lower-alpha →
decimal → lower-roman), each item a «I»German term«/I»—English gloss, with a
sub-list folded into its parent item's arg after a newline.

It is a LEAF shape (`_shapes.SHAPE_ORDERED_LIST`): the producer reads the raw
template and owns the recursion, so the classifier doesn't split each nested
level into its own element.  Output is ONE depth-encoded `«OUTLINE:…«/OUTLINE»`
marker (reusing the existing outline rendering — nested, indented), with each
level's `type=` label (I/II, a/b, i/ii, 1/2) preserved as the item-text prefix.
The printed source shows both the indentation and the labels.
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


def _walk(block: str, depth: int, text_transform, out: list[tuple[int, str]]) -> None:
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
            out.append((depth, f"{_label(kind, upper, n)}. {text_transform(text)}"))
        if nested:
            _walk(nested, depth + 1, text_transform, out)


def _process_ordered_list(raw: str, text_transform) -> str:
    rows: list[tuple[int, str]] = []
    _walk(raw, 0, text_transform, rows)
    if not rows:
        return ""
    body = "\n".join(f"{d}|{c}" for d, c in rows)
    return f"«OUTLINE:\n{body}\n«/OUTLINE»"
