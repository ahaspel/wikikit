"""Leaf-level element handlers — minimal logic.

TRUE leaves, whose content is a foreign language we do NOT descend into:
``<score>`` (musical notation → image) and ``<math>`` (LaTeX).  These read
their content flat, by design.

``<poem>`` / ``{{ppoem}}`` are NOT leaves: verse lines are article prose that
carry inline stylers / links / footnotes ({{sc}}, {{em}}, «I», `[[…]]`).  They
RECURSE their content via ``process_elements`` like any wrapper — every producer
transforms its outer and recurses its inner.  A wrapper that reads its content
flat is a failure-to-recurse bug; the leaking inner markup is the tell.
"""

from __future__ import annotations

import html as html_mod
import re

from britannica.image_assets import SCORE_IMAGES, normalize_score_content
from britannica.pipeline.stages.elements._dual_line import _split_top_level_pipe


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


# Display-vs-inline is the AUTHOR's choice, carried in the source — a
# `<math display="block">` tag, or a LaTeX environment (`\begin{…}`) which is
# inherently block-level.  We carry it in the marker so the viewer renders
# `displayMode` MECHANICALLY (exactly like an image's carried `align`), instead
# of guessing from paragraph context.
_MATH_DISPLAY_RE = re.compile(r"""display\s*=\s*["']?\s*block""", re.IGNORECASE)
_LATEX_ENV_RE = re.compile(r"\\begin\s*\{")


def _process_math(raw: str, inner: str) -> str:
    """Convert math content to «MATH[hints]:...«/MATH» marker, preserving LaTeX.

    The optional ``[hints]`` slot carries comma-separated tokens: ``display``
    (the author-carried block-vs-inline distinction, read off the ``<math
    display="block">`` tag or a ``\\begin{…}`` environment) and an offline-
    measured width hint (``fs=N`` / ``popout``).  The viewer renders display
    mode and scaling from these — no runtime guessing.  See
    ``britannica.math_widths`` and ``tools/diagnostics/measure_math_widths.py``.
    """
    from britannica.math_widths import scale_hint
    display = bool(_MATH_DISPLAY_RE.search(raw)) or bool(_LATEX_ENV_RE.search(inner))
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
    tokens = ["display"] if display else []
    hint = scale_hint(inner)
    if hint:
        tokens.append(hint)
    if tokens:
        return f"«MATH[{','.join(tokens)}]:{inner}«/MATH»"
    return f"«MATH:{inner}«/MATH»"


def _process_poem(inner: str, context) -> str:
    """Convert poem content to {{VERSE:...}VERSE}.

    A poem is a WRAPPER, not a leaf: its lines are article prose that can
    carry inline stylers / links / footnotes ({{sc}}, {{em}}, «I», `[[…]]`).
    Recurse the content so those render instead of leaking as raw markup; the
    verse line structure (newlines) rides through ``process_elements``
    untouched, so the marker's block shape is unchanged.

    The marker is block-level: <poem> blocks are virtually always block-level
    in EB1911 (block-quoted verse, classical citation, inscription).  The
    viewer's paragraph-level VERSE handler (``^{{VERSE:…}VERSE}$``) keys on
    that; the inline-VERSE fallback renders the whole verse as a continuation
    of the prose line (MOLECULE p684's Lucretius quote was the canonical case).
    """
    from britannica.pipeline.stages.elements import process_elements
    content = process_elements(inner, context)
    return "{{VERSE:" + content + "}VERSE}"


# `{{ppoem|…}}` — Wikisource preformatted-poem template, the verse analog of
# `<poem>`.  Its pipe-separated args carry optional control params
# (`start=`/`end=` stanza-frame hints, `class=`/`style=`) plus the verse itself
# (a bare positional arg, or the `1=…` named arg).  Strip the control params,
# take the verse, and route it through the SAME ``{{VERSE:…}VERSE}`` producer as
# `<poem>`.  Without this the whole template falls to body-text's catch-all and
# the verse content vanishes (SHAKESPEARE sonnet, VILLANELLE, HUCHOWN, …).
_PPOEM_CTRL_RE = re.compile(r"^\s*(?:start|end|class|style)\s*=", re.IGNORECASE)
_PPOEM_NUMBERED_RE = re.compile(r"^\s*1\s*=")


def _process_ppoem(inner: str, context) -> str:
    segs = _split_top_level_pipe(inner)[1:]   # drop the "ppoem" template name
    verse: list[str] = []
    for seg in segs:
        if _PPOEM_CTRL_RE.match(seg):
            continue                          # stanza-frame control param — drop
        m = _PPOEM_NUMBERED_RE.match(seg)
        if m:
            seg = seg[m.end():]               # `1=<verse>` → take the verse
        verse.append(seg)
    # Rejoin on `|` in the rare case a verse line legitimately held a top-level
    # pipe (split above would have severed it); strip the frame newlines.
    content = "|".join(verse).strip("\n")
    if not content.strip():
        return ""
    return _process_poem(content, context)


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
    """Render a structural formula table as paragraphs of its cell lines.

    Historically wrapped in a `«PRE:…«/PRE»` marker so a monospace
    `<pre>` block preserved the spatial alignment of the source.  In a
    responsive web viewport with proportional fonts the alignment can't
    survive, so the PRE wrap was dead weight (and a leak source: cell
    transforms emitted `«CTR»` inside, which leaked as text when the
    PRE renderer escapeHtml'd its content — ARACHNIDA Fig 7 case).
    Marker dropped per [[preserved-markup-is-a-contract]]: emit each
    line as its own paragraph.
    """
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("|"):
            line = line[1:].strip()
        if line and line not in ("|-", "}", "{|"):
            lines.append(line)
    if not lines:
        return ""
    return "\n".join(lines)
