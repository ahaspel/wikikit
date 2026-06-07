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
classifier labels DUAL_LINE (structure-only — content predicates for
chem / math will re-label CHEM_DUAL / MATH_DUAL once #74/#75 land).
The bounded element is what makes those predicates possible (without
walker-level recognition the classifier sees only the containing
prose paragraph and can't apply per-cluster judgments).

Variants:
  * `{{dual line|A|B}}` — bare two-arg.
  * `{{dual line|style=…|A|B}}` — leading `style=…` decoration param
    (POST table headers); the style is line-height tweak we drop.
"""
from __future__ import annotations

import re


_DUAL_LINE_NAME_RE = re.compile(
    r"^\s*dual\s+line\s*\|", re.IGNORECASE)


def _split_top_level_pipe(s: str) -> list[str]:
    """Split ``s`` on `|`, but only at depth-0 brace nesting — pipes
    INSIDE nested ``{{…|…}}`` aren't separators.  Pure utility; no
    dual-line-specific knowledge."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(s)
    while i < n:
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


def _process_dual_line(inner: str) -> str:
    """Render a DUAL_LINE element as ``A<br>B``.

    ``inner`` is the content between ``{{`` and ``}}`` (e.g.
    ``"dual line|A|B"``) with nested elements (refs, sub-templates that
    the walker would lift, etc.) ALREADY replaced by placeholder
    strings — those will be substituted back to their final markers by
    ``produce_tree`` after this producer returns.  We strip the leading
    template name + pipe, split the remainder on top-level pipes, drop
    a leading style decoration if any, and join the args with ``<br>``
    (their inner placeholders ride through to ``produce_tree``'s substitution).

    Degenerate single-arg form falls back to a plain-space join.
    """
    m = _DUAL_LINE_NAME_RE.match(inner)
    body = inner[m.end():] if m else inner
    parts = _split_top_level_pipe(body)
    if parts and parts[0].lstrip().lower().startswith("style="):
        parts = parts[1:]
    # Strip the RAW args, whose entities are still ENCODED — `.strip()`
    # can't eat a `&numsp;` the way it would the decoded U+2007 figure
    # space.  ORDNANCE has `{{dual line|1·75|1·6&numsp;}}` where the
    # trailing figure space is part of the column's display content.
    if len(parts) < 2:
        return " ".join(p.strip() for p in parts).strip()
    a = parts[0].strip()
    b = "|".join(parts[1:]).strip()
    return f"{a}<br>{b}"
