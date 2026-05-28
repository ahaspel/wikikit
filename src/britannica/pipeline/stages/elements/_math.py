"""MATH family producers and predicates (inline).

Today: the inline-MATH family covers ``MATH_DUAL`` (a ``{{dual line|…}}``
whose content carries math signature — italic variables, math operators,
sub/sup on non-element content).  Block-level math (``<math>``,
``MATH_LAYOUT_*`` for table-shaped math) lives in `_leaf.py` and
`_math_layout.py` for historical reasons; this module is the home for
inline math work going forward.

Predicate:
  * ``is_math_dual_line(inner_text)`` — runs AFTER `is_chem_dual_line`
    rules out chem; looks for italic-variable spans (`«I»…«/I»`),
    math operators (`= + → ⟶ < >`), or sub/sup markup.  These signals
    only count as math because we've already excluded the chem case
    where the same markers ride on element formulae.

Producer:
  * ``_process_math_dual_line(inner, text_transform)`` — initially
    byte-identical with `_process_dual_line` (renders `A<br>B`).
    Lives here so future math-specific work (KaTeX integration,
    typography, equation alignment) has a single home.
"""
from __future__ import annotations

import re

from britannica.pipeline.stages.elements._dual_line import (
    _process_dual_line, _split_top_level_pipe)


_DUAL_LINE_NAME_PREFIX_RE = re.compile(
    r"^\s*dual\s+line\s*\|", re.IGNORECASE)

# Italic-variable span: `«I»…«/I»` produced by `prepare_wikitext`'s
# quote-run conversion from `''x''`.  An italic single-word or short run
# inside a dual_line is a strong math signal because plain prose in a
# dual_line is exceedingly rare; if there's italic markup, it's almost
# always a math variable (ALGEBRAIC FORMS / COMBINATORIAL ANALYSIS /
# MECHANICS / MENSURATION / CIRCLE / HYDRAULICS).
_ITALIC_SPAN_RE = re.compile(r"«I»[^«]+«/I»")
# HTML sub/sup wrappers — math exponents/subscripts on variables
# (already ruled out as chem because chem case caught its formula
# operands first).
_SUB_SUP_RE = re.compile(r"<su[pb]\b", re.IGNORECASE)


def is_math_dual_line(inner_text: str) -> bool:
    """True iff the dual_line's content has math signature.

    Caller MUST have ruled out chem first (`is_chem_dual_line`) — the
    discriminator between chem and math is whether sub/sup ride on
    element symbols (chem) or on italic variables (math), and chem
    formulae will themselves contain `<sub>`/`{{sub|N}}` and italic-
    free `=` operators, which would otherwise trip this predicate.
    """
    m = _DUAL_LINE_NAME_PREFIX_RE.match(inner_text)
    body = inner_text[m.end():] if m else inner_text
    if _ITALIC_SPAN_RE.search(body):
        return True
    if _SUB_SUP_RE.search(body):
        return True
    return False


def _process_math_dual_line(inner: str, text_transform) -> str:
    """Render a MATH_DUAL element.

    Initially byte-identical with `_process_dual_line` (the shared
    layout producer).  Lives here so future math-specific rendering
    (variable typography, equation alignment, KaTeX) has its natural
    home — the math family owns its math-shaped dual_lines even when
    the rendering hasn't specialized yet.
    """
    return _process_dual_line(inner, text_transform)
