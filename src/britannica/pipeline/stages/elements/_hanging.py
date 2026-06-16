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


def process_hanging_indent(raw: str, context) -> str:
    """Render the hanging indent at its stated (or default) width, recurse text."""
    from britannica.pipeline.stages.elements import (
        process_elements, _styled_br_to_marker)
    inner = re.sub(r"^\{\{", "", raw)
    inner = re.sub(r"\}\}\s*$", "", inner)
    bar = inner.find("|")
    if bar < 0:                                   # bare `{{hi}}` — no content
        return ""
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
        return ""
    # A hanging indent is a display block, so its own (top-level) `<br>` is a
    # meaningful line break — carry it as «BR» before recursing, exactly like the
    # styled-block producers (else the body producer eats it as a soft-wrap space).
    content = _styled_br_to_marker(max(content_slots, key=len))
    body = process_elements(content, context, _allow_figure=False).strip()
    if not body:
        return ""
    return (f"«DIV[style:padding-left:{width}; text-indent:-{width}]»"
            f"{body}«/DIV»")
