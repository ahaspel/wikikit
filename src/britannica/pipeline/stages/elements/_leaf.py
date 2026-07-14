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
    # LaTeX is whitespace-insensitive EXCEPT `\ ` (backslash-space) — an explicit
    # control space, a real token.  The strip above clips a trailing one to a bare
    # `\`, a dangling command KaTeX rejects (renders red).  An ODD run of trailing
    # backslashes means the last lost its space; restore it.  (An even run is a `\\`
    # line break, which needs no trailing space.)  Source `104s. \ ` → `104s. \ `.
    m = re.search(r"\\+$", inner)
    if m and len(m.group(0)) % 2 == 1:
        inner += " "
    tokens = ["display"] if display else []
    hint = scale_hint(inner)
    if hint:
        tokens.append(hint)
    if tokens:
        return f"«MATH[{','.join(tokens)}]:{inner}«/MATH»"
    return f"«MATH:{inner}«/MATH»"


# <poem> — a WRAPPER, not a leaf: generic-decomposed, so its `inner` is already the poem body as
# placeholderized child nodes (stylers / links / footnotes are REAL nodes in the one tree), and
# `produce_tree` substitutes their markers after the producer returns.  The producer just wraps the
# recursed inner in the block-level `{{VERSE:…}VERSE}` marker — a `_MARKER_WRAP` data row now, no
# hand-written function.  The verse line structure (newlines) rides through in the BODY children.
# The marker is block-level (EB1911 <poem> is virtually always block-quoted verse / classical
# citation / inscription); the viewer's paragraph-level VERSE handler (`^{{VERSE:…}VERSE}$`) keys on
# that, the inline fallback renders it as a continuation of the prose line (MOLECULE p684's Lucretius).


# `{{ppoem|…}}` — Wikisource preformatted-poem template, the verse analog of
# `<poem>`.  Its pipe-separated args carry optional control params
# (`start=`/`end=` stanza-frame hints, `class=`/`style=`) plus the verse itself
# (a bare positional arg, or the `1=…` named arg).  Strip the control params,
# take the verse, and route it through the SAME ``{{VERSE:…}VERSE}`` producer as
# `<poem>`.  Without this the whole template falls to body-text's catch-all and
# the verse content vanishes (SHAKESPEARE sonnet, VILLANELLE, HUCHOWN, …).
_PPOEM_CTRL_RE = re.compile(r"^\s*(?:start|end|class|style)\s*=", re.IGNORECASE)
_PPOEM_NUMBERED_RE = re.compile(r"^\s*1\s*=")


def _ppoem_verse(inner: str) -> str:
    """Peel `{{ppoem|…}}`'s verse from its args: drop the template name + the stanza-frame
    control params (start=/end=/class=/style=), take the positional / `1=` verse, rejoin on
    `|` (a verse line may legitimately hold a top-level pipe), strip the frame newlines.
    Shared so `_classify_ppoem_composite` recurses the SAME verse the producer wraps."""
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
    # pipe (split above would have severed it); strip the frame newlines, then
    # recognize the verse line breaks (top-level `\n` → `<br>`) at this read point.
    return _verse_lines_to_br("|".join(verse).strip("\n"))


def _verse_lines_to_br(body: str) -> str:
    """Mark a verse body's line breaks, letting the WALKER draw the nesting line.  Walk the raw
    body once: its top-level text lands in SHAPE_BODY runs — the walker has already lifted every
    nested construct (``<ref>``, ``{{…}}``, ``[[…]]``, a styled span) into its own extract — so a
    ``\\n`` in a BODY run is, by construction, top-level.  Mark those: a single ``\\n`` → ``<br>``
    (the walker then owns it as «BR»); a ``\\n{2,}`` gap is left intact for the body producer to
    render as «P».  Every nested extract rides back verbatim, its own ``\\n`` untouched.  The
    caller decomposes the reassembled body, so the marks become real BR / «P» nodes.

    The nesting line is the walker's, not a re-derivation: no ``_construct_end`` skip that can
    diverge from what the walker actually extracts (a bare ``<span>`` it leaves transparent)."""
    from britannica.pipeline.stages.elements._walker import walk
    from britannica.pipeline.stages.elements._shapes import SHAPE_BODY
    ph_text, extracts = walk(body.strip("\n"))
    out = ph_text
    for ph, shape, raw in extracts:
        if shape == SHAPE_BODY:
            raw = re.sub(r"(?<!\n)\n(?!\n)", "<br>", raw)   # single \n → break; \n{2,} → «P»
        out = out.replace(ph, raw)
    return out


def _process_ppoem(raw, inner, context, inner_registry) -> str:
    """PPOEM producer — `{{ppoem|…}}`, the preformatted-poem template (verse analog of
    `<poem>`).  A COMPOSITE: `_classify_ppoem_composite` peeled the verse (`_ppoem_verse`)
    and decomposed it into child nodes; we substitute their markers and wrap in the SAME
    `{{VERSE:…}VERSE}` marker as `<poem>`.  An all-control / empty ppoem renders to nothing."""
    content = inner   # verse line breaks already recognized as «BR» at the read point (_ppoem_verse)
    if inner_registry is not None:
        for _ in range(5):
            changed = False
            for ph in list(inner_registry.elements):
                if ph in content:
                    content = content.replace(
                        ph, inner_registry.markers.get(ph, ""))
                    changed = True
            if not changed:
                break
    if not content.strip():
        return ""
    return "{{VERSE:" + content + "}VERSE}"
