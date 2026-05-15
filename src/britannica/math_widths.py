"""Lookup table for measured math widths.

The companion build-time tool
``tools/diagnostics/measure_math_widths.py`` renders every unique
display-mode `«MATH:` marker in the corpus through KaTeX in a headless
browser and records the resulting pixel width plus the smallest
font-size that fits the target body-text column.  The output JSON is
keyed by SHA256(latex)[:16] and lives at ``data/derived/math_widths.json``.

At pipeline time, ``_process_math`` (and the math-layout emitter) call
:func:`scale_hint` for each LaTeX expression and bake the resulting
hint into the marker:

    «MATH:plain«/MATH»                      — no hint, renders normally
    «MATH[fs=80]:expr«/MATH»                — render at 80% font-size
    «MATH[popout]:expr«/MATH»               — unscalable; viewer pops
                                              out to a separate panel

The hint is purely informational — viewer respects it or ignores it
gracefully.  No coupling to the LaTeX itself.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

_CACHE_PATH = Path("data/derived/math_widths.json")
_LOOKUP: dict | None = None

# Minimum readable in-column font-size.  KaTeX renders display math
# at body-text size; scaling it down with CSS `font-size: <N>%` makes
# the glyphs proportionally smaller, but below ~80% the text becomes
# painful to read.  Anything that would need a smaller scale to fit
# the column is treated as `unscalable` — the viewer routes those to
# the click-to-pop-out modal where the user gets natural-size math
# with a horizontal scrollbar.
_READABLE_FS_FLOOR = 80


def _hash(latex: str) -> str:
    return hashlib.sha256(latex.encode("utf-8")).hexdigest()[:16]


def _load() -> dict:
    global _LOOKUP
    if _LOOKUP is not None:
        return _LOOKUP
    if _CACHE_PATH.exists():
        try:
            _LOOKUP = json.load(open(_CACHE_PATH, encoding="utf-8"))
        except Exception:
            _LOOKUP = {}
    else:
        _LOOKUP = {}
    return _LOOKUP


def scale_hint(latex: str) -> str | None:
    """Return ``None`` (no hint needed), ``"fs=N"`` for a font-size
    scale, or ``"popout"`` for the unscalable case.  Missing entries
    return None — display-candidate markers that haven't been measured
    yet just render at natural size."""
    table = _load()
    entry = table.get(_hash(latex))
    if not entry:
        return None
    if entry.get("unscalable"):
        return "popout"
    fs = entry.get("best_fs")
    if fs is None or fs >= 100:
        return None
    # Below the readability floor, in-column scaling produces math
    # the user can't actually read.  Route to popout instead.
    if fs < _READABLE_FS_FLOOR:
        return "popout"
    return f"fs={fs}"
