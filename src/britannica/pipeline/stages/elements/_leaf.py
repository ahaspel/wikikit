"""Leaf-level element handlers — no children, minimal logic.

``<score>`` (musical notation), ``<math>``, ``<poem>``, and structural
chemical-formula table rendering.  None of these handlers recurse into
nested elements.
"""

from __future__ import annotations

import html as html_mod
import re


def _process_score(raw: str, context: dict) -> str:
    """Replace <score> tag with pre-rendered Wikimedia image URL."""
    from britannica.pipeline.stages.clean_pages import _SCORE_IMAGES, _SCORE_TAG

    vol = context.get("volume")
    page = context.get("page_number")
    if vol is not None and page is not None:
        # Try to match by content against the static map
        matches = list(_SCORE_TAG.finditer(raw))
        if matches:
            for (v, p, idx), url in _SCORE_IMAGES.items():
                if v == vol and p == page:
                    return f"{{{{IMG:{url}|Musical notation}}}}"
    return "[Musical notation]"


def _process_math(inner: str) -> str:
    """Convert math content to «MATH:...«/MATH» marker, preserving LaTeX."""
    inner = html_mod.unescape(inner.strip())
    # Collapse blank lines — they break LaTeX math environments
    inner = re.sub(r"\n{2,}", "\n", inner)
    return f"«MATH:{inner}«/MATH»"


def _process_poem(inner: str, text_transform) -> str:
    """Convert poem content to {{VERSE:...}VERSE}.

    Wrap with paragraph breaks: <poem> blocks are virtually always
    block-level in EB1911 (block-quoted verse, classical citation,
    inscription). Without the ``\\n\\n`` boundaries the surrounding
    prose paragraph absorbs the marker and the viewer's paragraph-
    level VERSE handler (``^{{VERSE:…}VERSE}$``) misses, falling
    back to the inline-VERSE handler which renders the whole verse
    as a continuation of the prose line (MOLECULE p684's Lucretius
    quote was the canonical case).
    """
    content = text_transform(inner)
    return "\n\n{{VERSE:" + content + "}VERSE}\n\n"


def _is_structural_formula(text: str) -> bool:
    """Detect tables that represent structural chemical formulas.

    Structural formulas use spatial arrangement of short cells with
    dashes, dots, pipes, and chemical symbols.  They typically have
    many rows, very short cell content, and characters like ─ │ ╲ ╱.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < 6:
        return False
    # Need many short pipe-cells AND structural characters
    short_cells = sum(1 for l in lines
                      if l.startswith("|") and len(l) < 8)
    has_structural = bool(re.search(r"[─│╲╱\\\/\.\-]{3,}", text))
    return short_cells > len(lines) * 0.6 and has_structural


def _format_structural_formula(text: str) -> str:
    """Convert a structural formula table to a PRE block."""
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("|"):
            line = line[1:].strip()
        if line and line not in ("|-", "}", "{|"):
            lines.append(line)
    content = "\n".join(lines)
    return f"«PRE:{content}«/PRE»"
