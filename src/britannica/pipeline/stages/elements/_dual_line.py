"""Producer for the DUAL_LINE element.

`{{dual line|A|B}}` is a pure layout primitive — stacks A and B as
two lines (emitted as `A<br>B`).  Content-agnostic: A and B can carry
any inline content (chem `C{{sub|6}}H{{sub|5}}`, layout `{{gap}}`,
refs `<ref>…</ref>`, math vars).  Used by math (60% of 611 corpus
instances — ALGEBRAIC FORMS, COMBINATORIAL ANALYSIS, HYDRAULICS,
MECHANICS, MENSURATION, CIRCLE), chemistry (9% — COUMARONES, ALCOHOLS,
INDIGO, PYRIDINE, AMINES, ISOMERISM, OXIMES), and pure layout (~22% —
table headers in POST / RUSSIA / IRON AND STEEL, figure-caption splits
in PHOTOGRAPHY, hyphenation wraps `Janu-|ary.`).

Walker recognizes the bracket pair (`_DUAL_LINE_RE` in `_walker.py`),
classifier labels it DUAL_LINE (structure-only).  ONE producer recurses
both lines, so chem/math content inside is produced by its own producer —
there is no chem/math-specific dual_line label (the old CHEM_DUAL /
MATH_DUAL split was speculative specificity, since removed).

Variants:
  * `{{dual line|A|B}}` — bare two-arg.
  * `{{dual line|style=…|A|B}}` — leading `style=…` decoration param
    (POST table headers); the style is line-height tweak we drop.
"""
from __future__ import annotations

import re


_DUAL_LINE_NAME_RE = re.compile(
    r"^\s*dual\s+line\s*\|", re.IGNORECASE)


_MATH_OPEN_RE = re.compile(r"<math\b", re.IGNORECASE)
_MATH_CLOSE = "</math>"


def _split_top_level_pipe(s: str) -> list[str]:
    """Split ``s`` on `|`, but only at depth-0 brace nesting — pipes INSIDE a
    nested ``{{…|…}}`` aren't separators.  A ``<math>…</math>`` block is opaque
    too: a LaTeX pipe (``\\left|…\\right|``, an absolute value, a matrix column)
    is content, never an arg boundary.  Pure utility; no caller-specific
    knowledge."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        if depth == 0 and s[i] == "<" and _MATH_OPEN_RE.match(s, i):
            end = s.lower().find(_MATH_CLOSE, i)
            if end != -1:
                end += len(_MATH_CLOSE)
                buf.append(s[i:end])     # the whole <math>…</math> rides through
                i = end
                continue
        if i + 1 < n and s[i] == "{" and s[i + 1] == "{":
            depth += 1
            buf.append("{{")
            i += 2
        elif i + 1 < n and s[i] == "}" and s[i + 1] == "}":
            depth = max(0, depth - 1)
            buf.append("}}")
            i += 2
        elif s[i] == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(s[i])
            i += 1
    parts.append("".join(buf))
    return parts


def _process_dual_line(inner: str, context) -> str:
    """Render a DUAL_LINE element as ``A<br>B``.

    A dual_line is a WRAPPER, not a leaf: each line is article markup that can
    carry inline stylers / sub-sup / refs / math vars ({{Polytonic}}, {{sub}},
    `<ref>`, …).  Recurse each line through ``process_elements`` — exactly the
    two-slot model `_process_fraction` follows — so that content renders instead
    of leaking as raw markup.  (Walker-lifted children arrive as opaque
    placeholders; they ride through the recursion untouched and are substituted
    by ``produce_tree`` after this producer returns.)

    ``inner`` is the content between ``{{`` and ``}}`` (e.g. ``"dual line|A|B"``).
    We strip the leading template name + pipe, split the remainder on top-level
    pipes, drop a leading style decoration if any, and join the recursed args
    with ``<br>``.

    The arg is `.strip()`-ed BEFORE recursing — while its entities are still
    ENCODED, so `.strip()` can't eat a `&numsp;` the way it would the decoded
    U+2007 figure space (ORDNANCE's `{{dual line|1·75|1·6&numsp;}}`, where the
    trailing figure space is part of the column's display content); the recursion
    then decodes it like the rest of the pipeline.

    Degenerate single-arg form falls back to a plain-space join.
    """
    from britannica.pipeline.stages.elements import process_elements
    recurse = lambda s: process_elements(s, context, _allow_figure=False)
    m = _DUAL_LINE_NAME_RE.match(inner)
    body = inner[m.end():] if m else inner
    parts = _split_top_level_pipe(body)
    if parts and parts[0].lstrip().lower().startswith("style="):
        parts = parts[1:]
    if len(parts) < 2:
        return " ".join(recurse(p.strip()) for p in parts).strip()
    a = recurse(parts[0].strip())
    b = recurse("|".join(parts[1:]).strip())
    return f"{a}<br>{b}"
