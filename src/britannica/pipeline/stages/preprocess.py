"""Whole-volume preprocess — the single source-cleaning + page-transition-
healing stage (see ``docs/canonical_path.md`` §1 step 2).

Linear pipe::

    make_stream(pages) -> raw continuous volume stream (page markers carried)
    preprocess(stream) -> clean FROZEN stream

After ``preprocess`` the source is frozen: nothing mutates it until producer
time, and ``\\x01PAGE:N\\x01`` markers survive as pure page-number bookkeeping
([[feedback_segments_two_purposes]]).  Cross-page tables / hyphen splits /
wrapped sentences heal AT THE SEAM here because the continuous stream is the
only context where both sides of a page transition are visible at once — doing
it per-page or per-article structurally leaks.

Corrections + quote-run → ``«B»``/``«I»`` are applied HERE, first
(``apply_corrections`` + ``_convert_quote_runs``), on the joined RAW stream —
formerly the per-page ``prepare_wikitext`` stage, folded in so this is the ONE
source-prep step (canonical_path §1).  ``make_stream`` consumes RAW
``page.wikitext``.  Section tags are NOT stripped here — ``detect_boundaries``
consumes ``<section begin>`` for stable-ID names; they drop after detection.
"""
from __future__ import annotations

import html
import re

from britannica.corrections import apply_corrections
from britannica.cleaners.hyphenation import fix_hyphenation
from britannica.pipeline.stages.quote_runs import _convert_quote_runs
from britannica.pipeline.stages.source_cleanup import (
    close_unclosed_attr_quotes,
    strip_html_comments,
    strip_noinclude_blocks,
)

# Page chrome inside ``<noinclude>`` (running headers, pagequality, smallrefs,
# rules) is furniture — but a noinclude block can ALSO carry the ONLY ``{|``/
# ``|}`` markers of a multi-page table: INDIANS, NORTH AMERICAN wraps each
# page's rows of its 3-page table in a standalone ``<noinclude>{|…|}</noinclude>``,
# and that table renders correctly in current local + production.  Those markers
# are NOT noise — wholesale-stripping them turned the table to prose.  Heuristic:
# KEEP everything unless we're REALLY sure it's noise.  So ``strip_noinclude_blocks``
# drops the chrome but PRESERVES the table markers.

# NOTE: the print-economy small-type block wrappers (`{{fine block/s}}…/e}}`,
# `{{EB1911 fine print/s}}`, `{{smaller block/s}}`) are NO LONGER stripped here.
# They are STYLERS (font-size:83% blocks); dropping them lost the styling and hid
# the styler work from the leak audit.  They now ride the CENTER paired-wrapper
# family (`{{NAME/s}}…{{NAME/e}}`), carried via the shared `_TEMPLATE_STYLE_WRAPPERS`
# registry — same as their pipe-form siblings.  Likewise `{{word-spacing|N|X}}` is a
# `word-spacing` styler (the param-styler registry), not a strip.

# A page seam is welded here, in the continuous stream.  The ONLY job is
# rejoining a WORD split across the break (``con-`` ⏎ ``tinuation`` or the
# ``{{hws}}``/``{{hwe}}`` pair).  A sentence that merely WRAPS needs nothing (the
# viewer renders a lone ``\n`` as a space).  Section tags are TRANSPARENT to the
# word-join (bridged so the join closes up) and otherwise left in place for
# ``detect``; their own-line whitespace is the SECTION construct's concern, not
# something to sweep out of the seam.
_SEC_TAG = r"<section\s+(?:begin|end)\b[^>]*?/?>"
# Bridge for a WORD-join across the seam — whitespace + section tags, with ≥1
# newline before the marker.
_BRIDGE_PRE = r"(?:[ \t]|" + _SEC_TAG + r")*\n(?:[ \t\n]|" + _SEC_TAG + r")*"
_BRIDGE_POST = r"(?:[ \t\n]|" + _SEC_TAG + r")*"
_SEC_FIND = re.compile(_SEC_TAG)


def _bridge_tags(bridge: str) -> str:
    """Keep the section tags from a seam bridge (in order), drop the whitespace
    — so the tags stay inline for detect while the word-join closes up."""
    return "".join(_SEC_FIND.findall(bridge))


# Cross-page hyphenation: ``tran-`` ⏎ [tags] ``␞PAGE␞`` [tags] ``slation`` →
# rejoin (drop the hyphen, no space), keeping page marker + section tags inline.
_XPAGE_HYPHEN = re.compile(
    r"(\w)-(" + _BRIDGE_PRE + r")(\x01PAGE:\d+\x01)(" + _BRIDGE_POST + r")(\w)")

# Cross-page WORD split via Wikisource half-word templates:
# ``{{hws|top|topmost}}`` ⏎ [tags] ``␞PAGE␞`` [tags] ``{{hwe|most|topmost}}`` —
# the same word "topmost" broken across the page.  The full word is param 2 of
# each.  Weld it: emit the full word once, keep page marker + section tags, drop
# both templates and the break.  (Long-form ``hyphenated word start/end`` are
# aliases.)  The word is hidden inside the template until producer expansion, so
# the literal-text hyphen/reflow rules above can't see it — this rule must run
# FIRST so the welded word becomes a plain ``\w`` before the marker.
_BRIDGE_WS_SEC = r"(?:[ \t\n]|" + _SEC_TAG + r")*"
_HWS = r"\{\{\s*(?:hws|hyphenated\s+word\s+start)\s*\|[^{}|]*\|([^{}]*)\}\}"
_HWE = r"\{\{\s*(?:hwe|hyphenated\s+word\s+end)\s*\|[^{}|]*\|[^{}]*\}\}"
_HWS_HWE_SEAM = re.compile(
    _HWS + r"(" + _BRIDGE_WS_SEC + r")(\x01PAGE:\d+\x01)(" + _BRIDGE_WS_SEC + r")"
    + _HWE, re.IGNORECASE)

# Survivor half-word templates — an ``{{hws}}``/``{{hwe}}`` pair that the seam
# rule above could NOT weld (the two halves don't straddle a single ``\x01PAGE``
# marker because the OCR split them differently, or one half stands alone).  The
# full word is param 2 of EACH, so the faithful render is: emit the whole word
# ONCE at the START (``{{hws|appear|appearances}}`` → ``appearances``) and drop
# the END (``{{hwe|ances|appearances}}`` → ``""``, the whole word already shown
# by its start).  Same "emit the full word once" semantics as the seam weld.
_HWS_STANDALONE = re.compile(_HWS, re.IGNORECASE)
_HWE_STANDALONE = re.compile(_HWE, re.IGNORECASE)


def make_stream(pages) -> str:
    """Join prepared (corrected, quote-run-converted) article pages into one
    continuous volume stream, page breaks riding as ``\\x01PAGE:N\\x01``.

    ``pages`` is the plate-free, page-ordered ``SourcePage`` list (the caller
    owns plate-splitting and ordering).
    """
    parts = []
    for p in pages:
        raw = (p.wikitext or "").strip()
        if not raw:
            continue
        parts.append(f"\x01PAGE:{p.page_number}\x01{raw}")
    return "\n".join(parts)


def heal_page_seams(text: str) -> str:
    """Heal page-transition artifacts at ``\\x01PAGE:N\\x01`` seams.

    The ONE definition of seam healing, called at every point where page
    fragments are assembled: by ``preprocess`` on the continuous volume stream,
    and by the per-article re-join in ``transform_articles`` (a transitional
    duplicate that disappears once the article body is taken as a slice of the
    clean stream rather than a re-join of stored segments).

    One job only — rejoin a WORD split across the break (hws/hwe templates;
    cross-page hyphen; within-page hyphen).  Everything else at a seam is
    render-neutral (a lone ``\\n`` is a space to the viewer) and untouched; the
    spurious ``\\n\\n`` that an empty-rendering construct (``<section/>`` /
    ``<ref follow>`` / ``{{nop}}``) would leave is NOT cured here — that is the
    construct's own line, dealt with where the construct is handled, not swept
    out of the seam.
    """
    text = _HWS_HWE_SEAM.sub(                           # cross-page split word
        lambda m: (f"{m.group(1)}{_bridge_tags(m.group(2))}{m.group(3)}"
                   f"{_bridge_tags(m.group(4))}"),
        text)
    text = _XPAGE_HYPHEN.sub(                           # cross-page hyphen
        lambda m: (f"{m.group(1)}{_bridge_tags(m.group(2))}{m.group(3)}"
                   f"{_bridge_tags(m.group(4))}{m.group(5)}"),
        text)
    text, _ = fix_hyphenation(text)                    # within-page hyphen
    return text


# Wikisource proofreading corrections — `<del>`/`<ins>` are a MIRROR PAIR marking
# a repair of an OCR/print error (`Feb<ins>r</ins>u<del>r</del>ary` → February):
# `<del>` is the discarded original text, `<ins>` the inserted correction.  Verified
# corpus-wide as ALWAYS editorial — every one is a correction, none carries genuine
# content or styling — so both are cut here, unconditionally, with no content decision
# (the sanctioned step-1 source-clean).  They are mirrors at the CONTENT level:
#   * `<del>` drops the tags AND the inner content (the discarded error).
#   * `<ins>` drops ONLY the tags, KEEPING the inner content — it IS the corrected
#     text ("February"), not a styler to underline.
_EDITORIAL_DEL = re.compile(r"<del\b[^>]*>.*?</del>", re.IGNORECASE | re.DOTALL)
_EDITORIAL_INS = re.compile(r"</?ins\b[^>]*>", re.IGNORECASE)


# Presentational HTML entities (`&nbsp;`, `&mdash;`, `&alpha;`, `&ldquo;`, `&emsp;`,
# …) are display sugar the SOURCE spells as entities; carried verbatim, the uniform-
# escaping viewer turns `&name;` into a visible `&name;` leak.  Decode them to their
# Unicode char here, mechanically (`html.unescape`) — no content decision, a step-1
# source-clean like the `<del>` corrections.  KEEP ONLY the tag-forging escapers
# `&lt;`/`&gt;` (incl. numeric/hex `<`/`>`) literal: decoding those would forge a tag.
# `&amp;`/`&quot;`/`&apos;` DO decode — the viewer re-escapes a raw `&`/`"`/`'`
# correctly, and that fixes their leak too.
_KEEP_ENTITY = re.compile(
    r"&(?:lt|gt|#0*(?:60|62)|#[xX]0*3[ce]);", re.IGNORECASE)


def _decode_entities(text: str) -> str:
    out, last = [], 0
    for m in _KEEP_ENTITY.finditer(text):
        out.append(html.unescape(text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(html.unescape(text[last:]))
    return "".join(out)


def preprocess(stream: str, volume: int = 0) -> str:
    """The single preprocessing step: corrections + quote-run + source-clean +
    page-transition heal on the continuous RAW stream; return the frozen clean
    stream.

    Corrections + quote-run run FIRST — formerly the per-page ``prepare_wikitext``
    stage, now folded in.  They're leaf-local, so running them on the joined
    stream is identical to per-page, and folding them here makes preprocess the
    one true source-prep step (canonical_path §1).  ``volume`` keys the
    corrections (0 ⇒ none — for diagnostic / test callers without a volume;
    production passes the real volume via ``volume_stream``)."""
    stream = apply_corrections(stream, volume)     # data/corrections.json typo fixes
    stream = _convert_quote_runs(stream)           # '''/''/<b>/<i>/{{bold|}} → «B»/«I»
    return _clean_and_heal(stream)


def _clean_and_heal(stream: str) -> str:
    """The re-appliable half of the pass — source-cleans + page-seam heals, i.e.
    everything EXCEPT the once-only raw→canonical conversions (corrections,
    quote-run).  Split out because these are idempotent and safe to re-apply: the
    transform-snapshot fixtures were captured post-quote-run but pre-clean, so
    that test applies THESE, not full ``preprocess`` (which would re-run quote-run
    on already-converted markup)."""
    # ── source cleaning — drop chrome but PRESERVE load-bearing table markers ──
    stream = close_unclosed_attr_quotes(stream)   # repair `<span style="…;>` etc.
    stream = strip_noinclude_blocks(stream)
    stream = strip_html_comments(stream)
    stream = _EDITORIAL_DEL.sub("", stream)                # <del> correction: drop error + tags
    stream = _EDITORIAL_INS.sub("", stream)                # <ins> correction: keep text, drop tags
    # ── page-transition healing (only correct on the continuous stream) ──
    stream = heal_page_seams(stream)
    # Half-word templates the seam weld couldn't pair (split differently than the
    # page marker, or standing alone): emit the full word once at the start, drop
    # the end — the same "whole word once" semantics as the seam weld above.
    stream = _HWS_STANDALONE.sub(r"\1", stream)            # {{hws|frag|WORD}} → WORD
    stream = _HWE_STANDALONE.sub("", stream)               # {{hwe|frag|WORD}} → ""
    stream = _decode_entities(stream)             # presentational HTML entities → chars
    return stream
