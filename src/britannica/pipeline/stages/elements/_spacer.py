"""Atomic LEAF producers — spacers, rules, and Wikisource char-escapes.  No content
recurses; each template maps to a glyph/char or a viewer marker.  Out of body-text's
flat re.subs: `{{em}}`/`{{gap}}`→space, `{{ditto}}`→″, `{{dhr}}`→«DHR»,
`{{rule}}`/`{{bar}}`→rule marker, `{{clear}}`/`{{anchor}}`→nothing, and the char-escapes
(`{{=}}`,`{{(}}`,`{{...}}`,`{{shy}}`, …) Wikisource uses to embed literal characters that
would otherwise parse as markup → their literal char.  Recognized, emit, nothing recurses.
"""

from __future__ import annotations

import re

# The classifier's SPACER vocabulary — the names it labels SPACER expecting this
# producer to render them.  We consult it as the single source of truth for the
# content-less CONTROL markers (below).  (Moral owner is this producer; the clean
# version relocates the set here and has the classifier import it — queued.)
from britannica.pipeline.stages.elements._classifier import _SPACER_NAMES

# Name is the token up to the first `|` (never a pipe/brace); the arg is
# everything after, nested braces ALLOWED — `{{ditto|(20)|{{nbsp}}}}` carries a
# nested `{{nbsp}}` the old `[^{}]*` arg couldn't span, which made the backtrack
# swallow the name and drop the whole ditto to "".
_LEAF_RE = re.compile(r"\{\{\s*([^|{}]*?)\s*(?:\|(.*))?\}\}", re.DOTALL)
# Literal-character escapes: template name IS the char (Wikisource convention).
_ESCAPE = {"=": "=", "(": "(", ")": ")", "'": "'", "!": "|", "*": "*", "–": "–"}


def process_spacer(raw: str) -> str:
    m = _LEAF_RE.match(raw)
    if not m:
        return raw  # malformed leaf — leak it raw, never sweep to ""
    name = m.group(1).strip()
    arg = (m.group(2) or "").strip()
    low = name.lower()
    if low in ("em", "gap"):
        # A fixed-width horizontal space -- {{gap}} is MediaWiki's 2em inline
        # gap, {{em}} an em-space.  Emit em-spaces (U+2003, NON-collapsing), not
        # a plain " " (HTML collapses that, dropping the width -- AUSTRIA's
        # `{{gap}}{{gap}}{{gap}}Total` lost its ~6em indent).  `|N` / `|Nem`
        # overrides the width.
        mn = re.match(r"([\d.]+)", arg)
        width = float(mn.group(1)) if mn else (2.0 if low == "gap" else 1.0)
        return " " * max(1, round(width))
    if low == "ditto":
        return "″"
    if low == "dhr":
        return f"«DHR[{arg}]»" if arg else "«DHR»"
    if low in ("rule", "bar"):
        mn = re.match(r"(\d+)", arg)
        if mn:
            return f"«BAR[{mn.group(1)}]»"
        return "———" if low == "rule" else "«BAR»"
    if name in ("...", "…", ". . ."):
        return "..."
    if name == "***":
        return "***"
    if low == "shy":
        return "­"  # soft hyphen
    if low == "ae":
        return "æ"  # æ ligature glyph
    if name in _ESCAPE:
        return _ESCAPE[name]
    if low == "nbsp":
        return " "  # non-breaking space (U+00A0), not a plain space
    if low in ("spaces", "pad thin", "fsp"):
        return " "                       # whitespace spacers → a space
    if low == "br":
        return "«BR»"                    # line break
    if low == "clear":
        # {{clear}} is a float-CLEARING element (clear:both), NOT a no-op: it
        # has no visible content but a real LAYOUT effect.  Dropping it let a
        # `{{float right}}` signature float beside the NEXT section's heading
        # instead of ending the previous one (AFRICA's `(E. He.; F. R. C.)`
        # rendered beside "Geology").  Emit the clearer.
        side = arg.lower() if arg.lower() in ("left", "right") else "both"
        return f"«DIV[style:clear:{side}]»«/DIV»"
    # Explicitly empty in linear flow — a DECISION listed by name, not a catch-all:
    # nop/nopt/nopf are no-ops.  (`anchor` is NOT here — it's a link target,
    # carried by the ANCHOR producer.)
    if low in ("nop", "nopt", "nopf"):
        return ""
    # Content-less CONTROL markers the classifier already routed here as SPACER
    # (its `_SPACER_NAMES` vocabulary): layout-frame halves and unpaired wrapper
    # `/s`·`/e` — `div end`, `multicol-end`, `plainlist/e`, `stack end`, and an
    # ORPHANED `EB1911 fine print/s` whose partner the SOURCE dropped (balanced
    # pairs never reach here — the walker folds them into a PAIRED_WRAPPER node).
    # They carry no glyph and no content → nothing.  The classifier raises on any
    # name it cannot route, so a name arriving here IN `_SPACER_NAMES` is vetted
    # empty, not a guess — and the glyph/DIV branches above claim `clear`/`em`/…
    # first, so this never empties a content-bearing leaf.  (This restores the
    # graceful drop the pre-walker strip did before the paired-wrapper migration.)
    if low in _SPACER_NAMES:
        return ""
    # Any other leaf is one this producer does NOT handle.  LEAK it (render raw)
    # so it surfaces in the audit — never sweep to "".  Sweeping inside a producer
    # is no more legitimate than sweeping the whole corpus: smaller blast radius,
    # same crime.
    return raw
