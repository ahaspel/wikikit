"""Spacer / layout-primitive LEAF producers — atomic glyphs and markers with no content
to recurse.  Out of `_apply_markup`'s flat re.subs: `{{em}}`/`{{gap}}` are a space,
`{{ditto}}` is ″, `{{dhr}}`/`{{rule}}` are horizontal-rule markers the viewer renders,
and `{{clear}}`/`{{anchor}}` are layout/target primitives with no glyph in linear flow.
A leaf: recognized, emit the char/marker, nothing recurses.
"""

from __future__ import annotations

import re

_SPACER_RE = re.compile(
    r"\{\{\s*([A-Za-z]+)\s*(?:\|([^{}]*))?\}\}", re.IGNORECASE | re.DOTALL)


def process_spacer(raw: str) -> str:
    m = _SPACER_RE.match(raw)
    if not m:
        return ""
    name = m.group(1).lower()
    arg = (m.group(2) or "").strip()
    if name in ("em", "gap"):
        return " "
    if name == "ditto":
        return "″"  # ″ ditto mark
    if name == "dhr":  # display horizontal rule (vertical spacer marker)
        return f"«DHR[{arg}]»" if arg else "«DHR»"
    if name == "rule":
        mn = re.match(r"(\d+)\s*em", arg)
        return f"«BAR[{mn.group(1)}]»" if mn else "———"
    # clear (float clear) / anchor (link target) — no glyph in linear flow.
    return ""
