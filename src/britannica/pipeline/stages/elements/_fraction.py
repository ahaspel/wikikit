"""Fraction production — the `{{sfrac|n|d}}` family's output, for the FRACTION element.

Out of body_text (deleted).  `_process_fraction` parses its slots (nested elements ride
through as walker-extracted placeholders), then calls
`_render_fraction` to PRODUCE the fraction from them — vulgar-Unicode where available (½,
¾, …), else `n/d`.  This is the producer assembling its OWN output from its recursed
parts, not an across-the-board transform of inner content.  `_split_top_pipes` is the
canonical parse primitive from `_link` (it splits, never strips).
"""

from __future__ import annotations

import re

from britannica.pipeline.stages.elements._link import _split_top_pipes

_VULGAR_FRACTIONS = {
    ("1", "2"): "½", ("1", "4"): "¼", ("3", "4"): "¾",
    ("1", "3"): "⅓", ("2", "3"): "⅔",
    ("1", "5"): "⅕", ("2", "5"): "⅖",
    ("3", "5"): "⅗", ("4", "5"): "⅘",
    ("1", "6"): "⅙", ("5", "6"): "⅚",
    ("1", "8"): "⅛", ("3", "8"): "⅜",
    ("5", "8"): "⅝", ("7", "8"): "⅞",
}


def _frac(num: str, den: str) -> str:
    return _VULGAR_FRACTIONS.get((num.strip(), den.strip()),
                                 f"{num.strip()}/{den.strip()}")


def _render_fraction(args: str, recurse) -> str:
    """Produce one fraction from its ``|``-joined slot string (which begins with the
    leading ``|``).  Drops ``name=value`` styling args; each positional slot is RETURNED
    through the loop via ``recurse`` (so a `{{Greek}}`/`{{sub}}`/`<math>` in a numerator
    is produced, not left raw); the positional count selects mixed / num-den / 1-n."""
    parts = _split_top_pipes(args)
    if parts and parts[0] == "":          # empty slot before the first `|`
        parts = parts[1:]
    pos = [recurse(p) for p in parts if not re.match(r"^[a-zA-Z_-]+=", p)]
    if len(pos) >= 3:
        return f"{pos[0].strip()}{_frac(pos[1], pos[2])}"
    if len(pos) == 2:
        return _frac(pos[0], pos[1])
    if len(pos) == 1:
        return _frac("1", pos[0])
    return ""


def render_over_fraction(inner: str, recurse) -> str:
    """Produce one fraction from the bar-less ``num \\over den`` form (the LaTeX-ish
    ``{{1\\over 2}}`` / ``{{\\it a \\over b}}`` / ``{{\\kappa\\over\\kappa'}}`` family —
    no ``name|`` token, the whole inner is ``num \\over den``).

    Splits on the literal ``\\over`` token, recurses each slot through ``recurse``
    (so a nested element / glyph in a numerator is produced, not left raw), and
    renders via the same vulgar-Unicode-or-``n/d`` rule the piped form uses.  A
    leading ``\\it``/``\\rm`` LaTeX font directive on a slot is a presentation hint
    with no Unicode equivalent here — dropped, the bare variable kept."""
    num, _sep, den = inner.partition(r"\over")
    if not _sep:                          # no \over token → nothing to do
        return inner
    num = _strip_latex_font(recurse(num).strip())
    den = _strip_latex_font(recurse(den).strip())
    return _frac(num, den)


_LATEX_FONT_RE = re.compile(r"^\\(?:it|rm|bf|mathit|mathrm|textstyle)\s+")


def _strip_latex_font(slot: str) -> str:
    """Drop a leading ``\\it``/``\\rm``/… LaTeX font directive (no Unicode form here);
    keep the bare slot content."""
    return _LATEX_FONT_RE.sub("", slot)
