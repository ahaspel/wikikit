"""Readings worth a second look in a page's mathematics — no model call.
Handed to the desk (`wikikit.scan.desk.bundle`, `flags=`), which marks each
strip and says why.  A flag is a pointer, not a verdict.

  lookalike  A Greek letter and its Latin look-alike carrying the SAME
             subscript on one page (α₁₁ and a₁₁): in type an upright α and an
             italic a are near twins, and each strip is read alone, so one
             symbol read two ways is nearly always one misreading — ALGEBRAIC
             FORMS, where a display split across two strips came back half α,
             half a.  The fewer side is flagged (both when they tie).
"""
from __future__ import annotations

import re
from collections import defaultdict

#: Greek letter -> the Latin letter it is mistaken for.
LOOKALIKES = {"alpha": "a", "nu": "v", "iota": "i", "kappa": "k", "rho": "p",
              "omega": "w", "chi": "x", "upsilon": "u"}
_MATH = re.compile(r"<math>(.*?)</math>", re.S)
_SYMBOL = re.compile(r"(\\(?:" + "|".join(LOOKALIKES) + r")(?![A-Za-z])|(?<![\\A-Za-z])[" +
                     "".join(LOOKALIKES.values()) + r"])\s*_\s*(\{[^{}]*\}|[A-Za-z0-9])")


def lookalikes(payload: dict) -> dict[str, list[dict]]:
    """{strip name: [{"text", "start", "end", "reason"}]} for one supplied page."""
    seen = defaultdict(list)          # (latin letter, subscript) -> [(greek?, strip, start, text)]
    for strip, text in payload["provenance"]["readings"].items():
        for m in _MATH.finditer(text or ""):
            for s in _SYMBOL.finditer(m.group(1)):
                sym, sub = s.group(1), s.group(2).strip("{}").replace(" ", "")
                greek = sym.startswith("\\")
                latin = LOOKALIKES[sym[1:]] if greek else sym
                seen[(latin, sub)].append((greek, strip, m.start(1) + s.start(), s.group(0)))
    flags: dict[str, list[dict]] = {}
    for (latin, sub), uses in seen.items():
        n_greek = sum(g for g, *_ in uses)
        n_latin = len(uses) - n_greek
        if not n_greek or not n_latin:
            continue
        greek_name = next(k for k, v in LOOKALIKES.items() if v == latin)
        for greek, strip, start, text in uses:
            if (greek and n_greek <= n_latin) or (not greek and n_latin <= n_greek):
                flags.setdefault(strip, []).append({
                    "text": text, "start": start, "end": start + len(text),
                    "reason": f"read both as Greek {greek_name} and Latin {latin} with subscript "
                              f"{sub} on this page ({n_greek} and {n_latin}): one symbol, one reading"})
    return flags
