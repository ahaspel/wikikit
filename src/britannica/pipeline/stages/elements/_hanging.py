"""Hanging-indent producer — `{{hi|W|text}}` / `{{hanging indent|W|text}}`.

The hanging indent is content the source states explicitly: a first-line outdent
of width `W` (the Wikisource `{{hi}}` default is 2em when no width is given).  We
RENDER it — `padding-left:W; text-indent:-W` IS the hanging indent — we do not
drop it: dropping the width discards a layout instruction the source carries, and
flattens the list it formats (the CHESS tournament rolls).  `W` is the first
positional arg WHEN it is a CSS length; otherwise that arg is content and the 2em
default applies.  The text recurses through the body producer.
"""
from __future__ import annotations

import re

from britannica.pipeline.stages.elements._link import _split_top_pipes

# A CSS length — `3.5em`, `.6em`, `2em`, `40px`, `100%`, … — i.e. the indent width.
_MEASURE_RE = re.compile(r"^-?\d*\.?\d+\s*(?:em|ex|px|pt|rem|%|cm|mm|in)$",
                         re.IGNORECASE)
_DEFAULT_WIDTH = "2em"   # the Wikisource `{{hi}}` default when no width arg is given


def _hanging_peel(raw: str) -> tuple[str, str]:
    """Peel `{{hi|W|text}}` → (width, content).  W is the first positional arg WHEN it is a CSS
    length (else the 2em default); content is the longest content slot with its top-level `<br>`
    carried as «BR».  Bare `{{hi}}` / no content → (width, "").  Shared so
    `_classify_hanging_composite` recurses the SAME content the producer wraps at the SAME width."""
    from britannica.pipeline.stages.elements import _styled_br_to_marker
    inner = re.sub(r"^\{\{", "", raw)
    inner = re.sub(r"\}\}\s*$", "", inner)
    bar = inner.find("|")
    if bar < 0:                                   # bare `{{hi}}` — no content
        return _DEFAULT_WIDTH, ""
    parts = _split_top_pipes(inner[bar:])
    positional = [
        p for p in parts
        if p != "" and not re.match(r"^\s*[A-Za-z_][\w\- ]*\s*=", p)
    ]
    width = _DEFAULT_WIDTH
    if len(positional) >= 2 and _MEASURE_RE.match(positional[0].strip()):
        width = positional[0].strip()
        content_slots = positional[1:]
    else:
        content_slots = positional
    if not content_slots:
        return width, ""
    # A hanging indent is a display block, so its own (top-level) `<br>` is a
    # meaningful line break — carry it as «BR» before recursing, exactly like the
    # styled-block producers (else the body producer eats it as a soft-wrap space).
    return width, _styled_br_to_marker(max(content_slots, key=len))


def process_hanging_indent(raw, inner, context, inner_registry) -> str:
    """HANGING_INDENT producer — `{{hi|W|text}}` / `{{hanging indent|…}}` / `{{outdent|…}}`.
    The source states a first-line outdent of width W (default 2em); we RENDER it
    (`padding-left:W; text-indent:-W`), never drop it (dropping flattens the list it formats —
    the CHESS tournament rolls).  A COMPOSITE: `_classify_hanging_composite` decomposed the
    content into child nodes; we substitute their markers, re-derive the width from raw
    (`_hanging_peel`), and wrap.  Empty content renders to nothing."""
    width, _content = _hanging_peel(raw)
    body = inner
    if inner_registry is not None:
        for _ in range(5):
            changed = False
            for ph in list(inner_registry.elements):
                if ph in body:
                    body = body.replace(
                        ph, inner_registry.markers.get(ph, ""))
                    changed = True
            if not changed:
                break
    body = body.strip()
    if not body:
        return ""
    return (f"«DIV[style:padding-left:{width}; text-indent:-{width}]»"
            f"{body}«/DIV»")
