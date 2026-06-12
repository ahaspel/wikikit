"""Atomic LEAF producers — spacers, rules, and Wikisource char-escapes.  No content
recurses; each template maps to a glyph/char or a viewer marker.  Out of body-text's
flat re.subs: `{{em}}`/`{{gap}}`→space, `{{ditto}}`→″, `{{dhr}}`→«DHR»,
`{{rule}}`/`{{bar}}`→rule marker, `{{clear}}`/`{{anchor}}`→nothing, and the char-escapes
(`{{=}}`,`{{(}}`,`{{...}}`,`{{shy}}`, …) Wikisource uses to embed literal characters that
would otherwise parse as markup → their literal char.  Recognized, emit, nothing recurses.
"""

from __future__ import annotations

import re

_LEAF_RE = re.compile(r"\{\{\s*(.*?)\s*(?:\|([^{}]*))?\}\}", re.DOTALL)
# Literal-character escapes: template name IS the char (Wikisource convention).
_ESCAPE = {"=": "=", "(": "(", ")": ")", "'": "'", "!": "|", "*": "*", "–": "–"}


def process_spacer(raw: str) -> str:
    m = _LEAF_RE.match(raw)
    if not m:
        return ""
    name = m.group(1).strip()
    arg = (m.group(2) or "").strip()
    low = name.lower()
    if low in ("em", "gap"):
        return " "
    if low == "ditto":
        return "″"
    if low == "dhr":
        return f"«DHR[{arg}]»" if arg else "«DHR»"
    if low in ("rule", "bar"):
        mn = re.match(r"(\d+)", arg)
        if mn:
            return f"«BAR[{mn.group(1)}]»"
        return "———" if low == "rule" else "«BAR»"
    if name in ("...", "…", ". . ."):
        return "..."
    if name == "***":
        return "***"
    if low == "shy":
        return "­"  # soft hyphen
    if low == "ae":
        return "æ"  # æ ligature glyph
    if name in _ESCAPE:
        return _ESCAPE[name]
    # clear / anchor / unknown leaf → nothing in linear flow.
    return ""
