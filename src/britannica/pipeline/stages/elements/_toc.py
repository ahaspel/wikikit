"""TOC-row producer — ``{{Dotted TOC line}}`` / ``{{Dotted TOC page listing}}``
/ ``{{TOC line}}``.

Each renders a dotted-leader row: a left label (an optional number + the entry)
and a right-aligned value (a page or a figure).  Their cells are TEMPLATE
PARAMETERS, not divider-delimited fields — positional ``col1|col2|col3``, or
named ``entrytext=…|pagetext=…`` for the ``page listing`` variant — so the table
chopper can't chop them (it splits on ``|``/``|-``, and here the ``|`` is shared
with layout params like ``spaces=2`` / ``col3-width``).  So we read the params
here and render the row, recursing each side through ``process_elements`` (so
hyphenation, ``{{ditto}}`` and inline markup are handled there).  The surrounding
wrapper — block-center, a ``{|`` cell, or a ``<div>`` — does the stacking.
"""
from __future__ import annotations

import re

from britannica.pipeline.stages.elements._link import _split_top_pipes

_NAMED_RE = re.compile(r"^\s*([A-Za-z_][\w\-]*)\s*=(.*)$", re.DOTALL)


def process_toc_row(raw: str, context) -> str:
    from britannica.pipeline.stages.elements import process_elements
    inner = re.sub(r"^\{\{", "", raw)
    inner = re.sub(r"\}\}\s*$", "", inner)
    parts = _split_top_pipes(inner)
    positional: list[str] = []
    named: dict[str, str] = {}
    for arg in parts[1:]:                       # parts[0] is the template name
        m = _NAMED_RE.match(arg)
        if m:
            named[m.group(1).lower()] = m.group(2)
        else:
            positional.append(arg)             # keep empties — they hold column position

    if "entrytext" in named:                   # the `page listing` variant
        col1, col2, col3 = "", named.get("entrytext", ""), named.get("pagetext", "")
    else:                                       # col1|col2|col3 — number | entry | value
        col1 = positional[0] if len(positional) > 0 else ""
        col2 = positional[1] if len(positional) > 1 else ""
        col3 = positional[2] if len(positional) > 2 else ""

    def rec(s: str) -> str:
        return process_elements(s, context, _allow_figure=False).strip() if s.strip() else ""

    left = rec((col1 + " " + col2).strip())
    right = rec(col3)
    if not left and not right:
        return ""
    if not right:                               # no value column (col3 empty / col3-width=0)
        return left                             # plain content — the entry sits in its cell/line
    leader = "«SPAN[style:flex:1;border-bottom:1px dotted;margin:0 .35em]»«/SPAN»"
    return (f"«DIV[style:display:flex;align-items:baseline]»"
            f"«SPAN[style:text-align:left;padding-left:1.5em;text-indent:-1.5em]»{left}«/SPAN»"
            f"{leader}"
            f"«SPAN[style:text-align:right]»{right}«/SPAN»«/DIV»")
