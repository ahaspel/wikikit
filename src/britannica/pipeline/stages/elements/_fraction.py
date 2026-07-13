"""Fraction production — the `{{sfrac|n|d}}` family's output, for the FRACTION element.

A fraction DECOMPOSES: `_fraction_parse` chops the slots (numerator / denominator, plus an
optional whole part), the classifier recurses each to a CELL node, and `_process_fraction`
reassembles the fraction from the cell markers — vulgar-Unicode where available (½, ¾, …),
else `n/d`.  The producer assembles its OWN output from its recursed parts (a `{{Greek}}` /
`{{sub}}` / `<math>` in a numerator is a real child node), not an across-the-board transform
of inner content.  `_split_top_pipes` is the canonical parse primitive from `_link` (it
splits, never strips).
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


def _fraction_parse(raw: str) -> tuple[str, list[str]]:
    """``(form, slots)`` — the reassembly FORM and the recurse-SLOTS of a fraction.

    A fraction decomposes: chop the slots, recurse each to a CELL node, and the producer
    reassembles vulgar-Unicode-or-``n/d`` from the cell markers.  This is the CHOP — the
    single parse the composite (for the slots) and the producer (for the form) both call:

      * ``'piped'``  — ``{{sfrac|n|d}}`` / ``{{sfrac|whole|n|d}}``: the positional slots
        (``name=value`` styling args dropped), count selects mixed / num-den / 1-n.
      * ``'binom'``  — a ``{{binom|n|d}}`` grouped pair; same slots, producer wraps in parens.
      * ``'over'``   — the bar-less ``num \\over den`` form (`{{1\\over 2}}` / `{{\\it a \\over
        b}}`); ``[num, den]``, the producer strips a leading ``\\it``/``\\rm`` font directive.
      * ``'bare'``   — a bare ``{{sfrac}}`` with no slots; producer echoes the name (defensive)."""
    inner = re.sub(r"\}\}\s*$", "", re.sub(r"^\{\{", "", raw))
    bar = inner.find("|")
    if bar < 0:
        if r"\over" in inner:
            num, _sep, den = inner.partition(r"\over")
            return "over", [num, den]
        return "bare", []
    parts = _split_top_pipes(inner[bar:])     # slot string begins with the leading `|`
    if parts and parts[0] == "":              # empty slot before the first `|`
        parts = parts[1:]
    slots = [p for p in parts if not re.match(r"^[a-zA-Z_-]+=", p)]
    form = "binom" if inner[:bar].strip().lower() == "binom" else "piped"
    return form, slots


_LATEX_FONT_RE = re.compile(r"^\\(?:it|rm|bf|mathit|mathrm|textstyle)\s+")


def _strip_latex_font(slot: str) -> str:
    """Drop a leading ``\\it``/``\\rm``/… LaTeX font directive (no Unicode form here);
    keep the bare slot content."""
    return _LATEX_FONT_RE.sub("", slot)
