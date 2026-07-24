"""Whole-volume preprocess — the single source-cleaning stage, run on the
continuous volume stream (see ``docs/canonical_path.md`` §1 step 2).

Linear pipe::

    make_stream(pages) -> raw continuous volume stream (page markers carried)
    preprocess(stream) -> clean FROZEN stream

After ``preprocess`` the source is frozen: nothing mutates it until producer
time, and ``\\x01PAGE:N\\x01`` markers survive as pure page-number bookkeeping
([[feedback_segments_two_purposes]]).  It runs whole-volume because the continuous
stream is the only context where both sides of a page transition are visible at
once — a cross-page table keeps its ``{|``…``|}`` together (per-page it would
shatter), and corrections / quote-run run once over the joined text.  Page-split
words are NOT rejoined here: they reach the walk as raw markers and reconstruct in
the split-word producer.

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
from britannica.pipeline.stages.quote_runs import _convert_quote_runs
from britannica.pipeline.stages.source_cleanup import (
    close_unclosed_attr_quotes,
    strip_html_comments,
    strip_noinclude_blocks,
)

# Page chrome inside ``<noinclude>`` (running headers, pagequality, smallrefs,
# rules, AND any ``{|``/``|}`` table delimiters — a cross-page table's
# standalone-view close/reopen, or a 2-column page-layout wrapper) is furniture:
# MediaWiki does not transclude it, so the mainspace article is raw_text MINUS
# noinclude.  ``strip_noinclude_blocks`` drops the block whole; the mainspace
# table stays one continuous span across pages, paired by the whole-volume
# balanced matcher (INDIANS, NORTH AMERICAN's 13-page table).  The old
# keep-table-markers rescue is deleted — J1 of docs/sweeper_removal.md.

# NOTE: the print-economy small-type block wrappers (`{{fine block/s}}…/e}}`,
# `{{EB1911 fine print/s}}`, `{{smaller block/s}}`) are NO LONGER stripped here.
# They are STYLERS (font-size:83% blocks); dropping them lost the styling and hid
# the styler work from the leak audit.  They now ride the CENTER paired-wrapper
# family (`{{NAME/s}}…{{NAME/e}}`), carried via the shared `_TEMPLATE_STYLE_WRAPPERS`
# registry — same as their pipe-form siblings.  Likewise `{{word-spacing|N|X}}` is a
# `word-spacing` styler (the param-styler registry), not a strip.

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


# (`<bdo dir=X>`, `<small>`, `<big>` are no longer converted here — they are
# TAG-IMPLIED stylers the walker lifts directly (`_TAG_STYLER_RE`) and the
# HTML_STYLE producer styles; J3/J4 of docs/sweeper_removal.md.)


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


# (`{{{name|default}}}` param-defaults are no longer resolved here — every
# article-space instance sits in a table/cell attr slot, so the decode lives in
# `_table_fold.fold_cell_attrs`, the producer that owns the slot; J5 of
# docs/sweeper_removal.md.  The front-matter instances — vol 1's title page
# `VOLUME {{{vol|I}}}`, ws pages 3–4 — are outside ARTICLE_WS_RANGE and never
# enter this chain.)


# Page furniture the article body carries but that renders nothing: the running
# page heading, the `{{pagenum}}` folio (redundant with our «PAGE» markers), the
# Wikisource maintenance `{{Ambox}}` notices ("proofreading cheats"), and
# `{{Hidden text}}` (deliberately `display:none` sort keys).  Pure layout/editorial
# noise no path needs — stripped here, balanced, like the noinclude chrome.  Their
# CONTENT-bearing siblings (`{{suspect}}` / `{{main other}}` / `{{lps}}`) are NOT
# stripped: they reach the walk and lift their text.
_CHROME_FURNITURE = re.compile(
    r"\{\{\s*(?:eb1911 page heading|ambox|hidden text|pagenum)\s*[|}/]", re.I)


def _strip_chrome_furniture(stream: str) -> str:
    """Remove each furniture template whole (balanced `{{…}}`, so a multi-line
    `{{Ambox|…}}` or a nested arg can't truncate the span)."""
    out: list[str] = []
    i, n = 0, len(stream)
    for m in _CHROME_FURNITURE.finditer(stream):
        if m.start() < i:                 # opener inside an already-removed template
            continue
        out.append(stream[i:m.start()])
        depth, j = 0, m.start()
        while j < n - 1:
            two = stream[j:j + 2]
            if two == "{{":
                depth += 1
                j += 2
                continue
            if two == "}}":
                depth -= 1
                j += 2
            else:
                j += 1
            if depth == 0:
                break
        i = j
    out.append(stream[i:])
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


# A whitespace-only line IS a blank line; strip trailing spaces/tabs so a
# space-polluted blank line ("\n \n") reads as the "\n\n" paragraph break the
# author meant (ABBEY Fig. 1 legend C/D; ~326 occurrences corpus-wide). Lossless:
# trailing whitespace carries nothing in prose, cells, verse, or tables.
_TRAILING_WS = re.compile(r"[ \t]+(?=\r?\n)")


def _clean_and_heal(stream: str) -> str:
    """The re-appliable half of the pass — source-cleans + page-seam heals, i.e.
    everything EXCEPT the once-only raw→canonical conversions (corrections,
    quote-run).  Split out because these are idempotent and safe to re-apply: the
    transform-snapshot fixtures were captured post-quote-run but pre-clean, so
    that test applies THESE, not full ``preprocess`` (which would re-run quote-run
    on already-converted markup)."""
    # ── source cleaning — drop chrome but PRESERVE load-bearing table markers ──
    stream = _TRAILING_WS.sub("", stream)         # whitespace-only line -> clean blank line
    stream = close_unclosed_attr_quotes(stream)   # repair `<span style="…;>` etc.
    stream = strip_noinclude_blocks(stream)
    stream = strip_html_comments(stream)
    stream = _strip_chrome_furniture(stream)               # running head / pagenum / ambox / hidden-text
    stream = _EDITORIAL_DEL.sub("", stream)                # <del> correction: drop error + tags
    stream = _EDITORIAL_INS.sub("", stream)                # <ins> correction: keep text, drop tags
    # Page-split words (`{{hws}}`/`{{hwe}}`/`{{lps}}`/`{{lpe}}`) are NOT
    # reconstructed here — they reach the walk as raw templates and are rejoined by
    # recognition (the SPLIT_WORD producer): start marker → the whole word, end
    # marker → nothing.
    stream = _decode_entities(stream)             # presentational HTML entities → chars
    return stream
