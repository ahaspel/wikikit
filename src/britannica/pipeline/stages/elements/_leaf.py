"""Leaf-level element handlers — no children, minimal logic.

``<score>`` (musical notation), ``<math>``, ``<poem>``, and structural
chemical-formula table rendering.  None of these handlers recurse into
nested elements.
"""

from __future__ import annotations

import html as html_mod
import re

from britannica.image_assets import SCORE_IMAGES, normalize_score_content


def _process_score(inner: str) -> str:
    """Replace a <score> tag's content with its pre-rendered Wikimedia
    image URL.  Content-addressable: looks up the whitespace-normalized
    LilyPond source in ``SCORE_IMAGES``.  Falls back to the literal
    text "[Musical notation]" if the content isn't in our pre-fetched
    table — only happens for newly-added EB1911 scores that haven't
    been registered yet."""
    key = normalize_score_content(inner)
    url = SCORE_IMAGES.get(key)
    if url:
        return f"{{{{IMG:{url}|Musical notation}}}}"
    return "[Musical notation]"


def _process_math(inner: str) -> str:
    """Convert math content to «MATH:...«/MATH» marker, preserving LaTeX.

    Looks up an offline-measured rendering hint
    (``fs=N`` or ``popout``) and bakes it into the marker so the
    viewer can render scaled or popped-out without runtime
    measurement.  See ``britannica.math_widths`` for the table; see
    ``tools/diagnostics/measure_math_widths.py`` for how it's built.
    """
    from britannica.math_widths import scale_hint
    inner = html_mod.unescape(inner.strip())
    # Canonicalise whitespace: collapse all runs of whitespace
    # (including newlines) to a single space.  LaTeX is whitespace-
    # insensitive, so this is safe — and it ensures the marker's
    # content is in the same form whether `_process_math` emits it
    # fresh or a later transform pass normalises it.  Without this,
    # the offline width-measurement (which keys by SHA256 of marker
    # content) misses cache hits because the emission and the
    # measured form differ.
    inner = re.sub(r"\s+", " ", inner).strip()
    hint = scale_hint(inner)
    if hint:
        return f"«MATH[{hint}]:{inner}«/MATH»"
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
