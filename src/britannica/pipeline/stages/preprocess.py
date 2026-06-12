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
from britannica.image_assets import CHART2_IMAGES, TREE_IMAGES
from britannica.pipeline.stages.quote_runs import _convert_quote_runs
from britannica.pipeline.stages.source_cleanup import (
    close_unclosed_attr_quotes,
    strip_html_comments,
    strip_noinclude_blocks,
)

# The 5 ``{{chart2/start}}…{{chart2/end}}`` genealogical-tree blocks render to an
# unusable mess, so each was manually cropped from the page scan
# (``CHART2_IMAGES``).  Substitute the block with the cropped image AT SOURCE
# (in ``make_stream``, which has the (volume, page) identity) — a source fix like
# a correction.  Any enclosing wrapper (the vol-21 caption table, the vol-23
# footnote text) is legitimate content and stays, so the figure / footnote
# producer renders the image in place.  Replaces the old post-walker injection
# loop in ``_transform_text_v2``.
_CHART2_BLOCK = re.compile(
    r"\{\{\s*chart2\s*/\s*start[\s\S]*?\{\{\s*chart2\s*/\s*end\s*\}\}", re.IGNORECASE)

# Sibling genealogical-tree macros (``{{familytree/start}}…{{familytree/end}}``,
# ``{{Tree chart/start}}…{{Tree chart/end}}``) — same crop-to-image treatment as
# chart2, keyed via ``TREE_IMAGES``.  NOTE: a familytree node can carry an inner
# ``<ref>`` footnote (COWPER's Alderman-Cooper name note); replacing the whole
# block drops that footnote text — a known minor loss, acceptable since the tree
# itself is the content and the alternative is the catch-all deleting everything.
# TODO(cleanup): chart2 + tree are one class — unify the two dicts + block regexes
# into a single genealogy-image substitution, and delete the now-dead
# ``_process_chart2`` / the chart2 family of the PAIRED_WRAPPER walker path
# (preprocess preempts it).
_TREE_BLOCK = re.compile(
    r"\{\{\s*(?:familytree|tree\s*chart)\s*/\s*start[\s\S]*?"
    r"\{\{\s*(?:familytree|tree\s*chart)\s*/\s*end\s*\}\}", re.IGNORECASE)
# A familytree node can annotate itself with an inner ``<ref>…</ref>`` footnote
# (COWPER's "Alderman Cooper thus spelt his name…" note).  Preserve it across the
# crop-to-image replacement: re-emit the ref after the IMG so the footnote
# producer renders it as a normal article footnote rather than losing it with the
# rest of the grid macro.
_TREE_INNER_REF = re.compile(r"<ref\b[^>]*>[\s\S]*?</ref>", re.IGNORECASE)


def _tree_to_img(block: str, fn: str) -> str:
    refs = "".join(_TREE_INNER_REF.findall(block))
    return f"{{{{IMG:{fn}|Genealogical table}}}}{refs}"

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

# ``{{nop}}`` is a MediaWiki no-op — invisible output, used in the source only to
# force block separation that our renderer doesn't need.  It is chrome; drop it
# WITH its own line (tag + trailing newline), so an alone-on-its-line nop can't
# strand its bracketing newlines as a ``\n\n`` once it renders to nothing.  (The
# tag-only strip in body_text.py — ``re.sub(r"\{\{nop\}\}", "")`` — is the bug
# that left the line behind; this supersedes it.)
_NOP = re.compile(r"[ \t]*\{\{\s*nop\s*\}\}[ \t]*\n?", re.IGNORECASE)

# ``<ref follow=X>body</ref>`` is a footnote CONTINUATION: its body folds into
# the named ref X (article-scoped, in ``resolve_ref_bodies``) and its ANCHOR
# renders to nothing.  When it sits ALONE ON ITS OWN LINE it is bracketed by two
# newlines; the empty anchor would then strand them as a ``\n\n``.  Collapse the
# pair to one by dropping the trailing newline — but ONLY when a leading newline
# is present (own-line), so an INLINE follow-ref (``word<ref follow>…</ref>`` ⏎
# ``PAGE``) keeps the single newline that is its only word separator.  Body left
# intact (non-greedy match stops at its own ``</ref>``).
_REF_FOLLOW_LINE = re.compile(
    r"(\n[ \t]*<ref\s+follow\b[^>]*>[\s\S]*?</ref>)[ \t]*\n", re.IGNORECASE)

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

# ``{{EB1911 Page Heading|page-no|running-title|…}}`` — pure page chrome (the
# scan's running header, emitted at the top of every page inside ``<noinclude>``).
# It carries NO article content; strip the whole template before the walker.  It
# is page furniture exactly like ``{{nop}}`` — a step-1 source-clean, no content
# decision.  Most are inside ``<noinclude>`` (already dropped) but the standalone
# survivors must not reach the walker.
_PAGE_HEADING = re.compile(
    r"\{\{\s*EB1911 Page Heading\b[^{}]*\}\}", re.IGNORECASE)

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
        fn = CHART2_IMAGES.get((p.volume, p.page_number))
        if fn:
            raw = _CHART2_BLOCK.sub(
                f"{{{{IMG:{fn}|Genealogical table}}}}", raw, count=1)
        fn = TREE_IMAGES.get((p.volume, p.page_number))
        if fn:
            raw = _TREE_BLOCK.sub(
                lambda m: _tree_to_img(m.group(0), fn), raw, count=1)
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


# Wikisource proofreading DELETE: `<del>` marks text the transcriber removed to repair
# an OCR/print error (`Feb<ins>r</ins>u<del>r</del>ary` → February).  Verified corpus-
# wide as ALWAYS editorial — every <del> is a correction, none carries genuine content
# or styling — so it is cut here, unconditionally, with no content decision (a correction
# is the sanctioned step-1 source-clean).  Its `<ins>` partner is the kept correction
# (a styler, lifted by the walker).
_EDITORIAL_DEL = re.compile(r"<del\b[^>]*>.*?</del>", re.IGNORECASE | re.DOTALL)


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
    stream = _NOP.sub("", stream)                          # {{nop}} no-op chrome + its line
    stream = _PAGE_HEADING.sub("", stream)                 # {{EB1911 Page Heading|…}} page chrome
    stream = _REF_FOLLOW_LINE.sub(r"\1", stream)           # <ref follow> chrome line
    stream = strip_html_comments(stream)
    stream = _EDITORIAL_DEL.sub("", stream)                # Wikisource <del> corrections
    # ── page-transition healing (only correct on the continuous stream) ──
    stream = heal_page_seams(stream)
    # Half-word templates the seam weld couldn't pair (split differently than the
    # page marker, or standing alone): emit the full word once at the start, drop
    # the end — the same "whole word once" semantics as the seam weld above.
    stream = _HWS_STANDALONE.sub(r"\1", stream)            # {{hws|frag|WORD}} → WORD
    stream = _HWE_STANDALONE.sub("", stream)               # {{hwe|frag|WORD}} → ""
    stream = _decode_entities(stream)             # presentational HTML entities → chars
    return stream
