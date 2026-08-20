"""Whole-volume preprocess — the single source-cleaning stage, run on the
continuous volume stream (see ``docs/canonical_path.md`` §1 step 2).

Linear pipe — ``stream_with_keys`` is the entry point; the rest is internal::

    make_stream(pages)   -> raw volume stream, page tokens as SCAFFOLDING
    preprocess(stream)   -> clean stream (source-clean; token untouched)
    stream_with_keys(…)  -> (FROZEN clean stream, page keys, section keys)

After ``stream_with_keys`` the source is frozen and carries NO positional chrome:
the ``\\x01PAGE:N\\x01`` tokens and the ``<section …>`` tags are both gone, lifted
out as ``[(offset, page)]`` and ``[(offset, name)]``.  Both were only ever
annotations about WHERE things are, and page-marker work happens in exactly two
places — here, and at render ([[project_page_position_out_of_band]]).  Nothing
downstream edits the stream, so every offset stays exact forever.

It runs whole-volume because the continuous stream is the only context where both
sides of a page transition are visible at once — a cross-page table keeps its
``{|``…``|}`` together (per-page it would shatter), corrections / quote-run run
once over the joined text, and the page seam itself is normalised to exactly one
newline where both pages' cleaned text is finally visible.

Page-split words (``{{hws}}``/``{{hwe}}``) are NOT rejoined here: they reach the
walk as raw templates and reconstruct by RECOGNITION in the split-word producer.

Corrections + quote-run → ``«B»``/``«I»`` are applied HERE, first
(``apply_corrections`` + ``_convert_quote_runs``), on the joined RAW stream —
formerly the per-page ``prepare_wikitext`` stage, folded in so this is the ONE
source-prep step (canonical_path §1).  ``make_stream`` consumes RAW
``page.wikitext``.
"""
from __future__ import annotations

import html
import re

from britannica.corrections import apply_corrections
from britannica.pipeline.stages.quote_runs import _convert_quote_runs
from britannica.pipeline.stages.source_cleanup import (
    strip_html_comments,
    strip_noinclude_blocks,
    strip_includeonly_tags,
)
from britannica.wikitext import template_end

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

_PAGE_KEY_RE = re.compile(r"\x01PAGE:(\d+)\x01")
# Positional chrome of the SECOND kind: `<section begin="NAME"/>` names the
# article a stretch of stream belongs to, and `<section end=…/>` closes it.
# Detection reads them and then deletes them — exactly what it does with the page
# markers — so they are keys, not content.  The trailing `[ \t]*\n?` is for a tag
# that occupies a whole LINE mid-page: dropping it must not leave a blank line
# behind.  (It was written to match the deleted `super_detect._SECTAG_DROP`
# verbatim; that copied `\n?` also ate the page-join newline, which the seam rule
# in `stream_with_keys` now re-establishes.)
_SECTION_TAG_RE = re.compile(
    r"[ \t]*<section\s+(?:begin|end)\b[^>]*?/?>[ \t]*\n?", re.IGNORECASE)
_SECTION_NAME_RE = re.compile(
    r'<section\s+begin\s*=\s*"?([^">]*)"?\s*/?>', re.IGNORECASE)


_SEAM_WS = " \t\n"


def _rstrip_emitted(out: list[str]) -> int:
    """Drop whitespace already emitted at the end of `out`; return how much."""
    removed = 0
    while out:
        chunk = out[-1]
        kept = chunk.rstrip(_SEAM_WS)
        removed += len(chunk) - len(kept)
        if kept:
            out[-1] = kept
            break
        out.pop()
    return removed


def stream_with_keys(
    pages, volume: int = 0,
) -> tuple[str, list[tuple[int, int]], list[tuple[int, str]]]:
    """The clean volume stream, plus its KEYS — ``[(offset, page)]`` and
    ``[(offset, section_name)]``.

    The returned stream is FINAL: nothing downstream edits it, so every offset
    stays exact forever.  That is the whole point of extracting both kinds of
    chrome here.  Section tags used to be dropped later, per article
    (``super_detect._SECTAG_DROP``), which shifted every page key computed before
    it — "exact until the next edit" is not exact.

    Page-marker work happens in exactly TWO places in this pipeline: HERE, where
    the keys are created, and at RENDER, where they are read.  Everything in
    between has no concept of a page marker; for that code page markers do not
    exist ([[project_page_position_out_of_band]]).

    The ``\\x01PAGE:N\\x01`` token is INTERNAL SCAFFOLDING and never escapes this
    function.  It exists only because ``preprocess`` rewrites the stream and
    changes lengths, so an offset computed before it would be stale by the time
    anyone could use it; a token rides along with the text instead.  We then read
    the offsets out of the processed stream and strip the tokens, so the caller
    receives clean source and exact boundaries.

    This works because ``preprocess`` contains NO code that touches the token —
    it mentions it only in docstrings.  That is an observed fact, not a hope,
    which is why the count is ASSERTED below: if a pass ever does disturb one,
    the offsets are no longer exact and we fail loudly here rather than shipping
    a page marker silently shifted into the wrong paragraph.
    """
    scaffolded = make_stream(pages)
    expected = scaffolded.count("\x01PAGE:")
    processed = preprocess(scaffolded, volume)

    # ONE pass over both chrome kinds, so their offsets are computed in the same
    # coordinate system — a section tag removed before a page token shortens the
    # text that token's offset is measured against, and vice versa.
    page_keys: list[tuple[int, int]] = []
    section_keys: list[tuple[int, str]] = []
    out: list[str] = []
    clean_len = 0
    last = 0
    both = sorted(
        [(m.start(), m.end(), "page", m.group(1)) for m in _PAGE_KEY_RE.finditer(processed)]
        + [(m.start(), m.end(), "section", m.group(0)) for m in _SECTION_TAG_RE.finditer(processed)]
    )
    strip_lead = False                   # a page key just closed; drop its
                                         # incoming page's leading whitespace
    for start, end, kind, payload in both:
        if start < last:                 # a page token inside a section tag's
            continue                     # whitespace run — already consumed
        chunk = processed[last:start]
        if strip_lead:
            kept = chunk.lstrip(_SEAM_WS)
            if kept:                     # only stop stripping once real content
                strip_lead = False       # has been emitted, not at an empty gap
            chunk = kept
        out.append(chunk)
        clean_len += len(chunk)
        if kind == "page":
            # A page break is exactly ONE line break: a printed page cannot end
            # mid-line, and it cannot end mid-paragraph invisibly either —
            # Wikisource writes `{{nop}}` when a paragraph genuinely breaks
            # across a page (192 of 2,949 seams in vols 1/8/22 do).
            #
            # `make_stream` already says this: it `.strip()`s each page and
            # joins with "\n".  But it runs while the `<noinclude>` wrapper is
            # still attached, so whitespace the noinclude removal later EXPOSES
            # was never strippable — 284 of those seams kept a leading newline
            # and read as a paragraph break mid-sentence ("Prester John, and
            # various ¶ expeditions had been sent").  The other direction is
            # `_SECTION_TAG_RE`'s trailing `[ \t]*\n?` eating the join newline
            # outright, which glued "difference" to "between" at 10,385 seams.
            #
            # So this is not a repair bolted onto the join — it IS the join,
            # finished at the one point where both pages' text is known and
            # chrome is gone.  `{{nop}}` is not whitespace, so it survives and
            # keeps carrying the paragraph a page break cannot show by itself.
            clean_len -= _rstrip_emitted(out)
            if clean_len:                # the volume's first page opens the
                out.append("\n")         # stream; there is no seam before it
                clean_len += 1
            page_keys.append((clean_len, int(payload)))
            strip_lead = True
        else:
            nm = _SECTION_NAME_RE.search(payload)
            if nm:                        # `<section end>` names nothing — it only closes
                section_keys.append((clean_len, (nm.group(1) or "").strip()))
        last = end
    tail = processed[last:]
    out.append(tail.lstrip(_SEAM_WS) if strip_lead else tail)

    if len(page_keys) != expected:
        raise ValueError(
            f"page-key scaffolding was disturbed by preprocess (volume {volume}): "
            f"{expected} tokens in, {len(page_keys)} out.  The boundaries are no longer "
            f"exact — fix the pass that consumed one rather than relaxing this check.")
    return "".join(out), page_keys, section_keys


def make_stream(pages) -> str:
    """Join RAW article pages into one continuous volume stream, page breaks
    riding as ``\\x01PAGE:N\\x01``.

    INTERNAL to ``stream_with_keys`` — the token is scaffolding for computing the
    page keys across ``preprocess``, and is stripped before anything downstream
    sees the stream.  Callers outside this module want ``stream_with_keys``.

    ``pages`` is the plate-free, page-ordered ``SourcePage`` list (the caller
    owns plate-splitting and ordering).  Pages whose raw text is empty are
    skipped entirely — they carry no content and so hold no boundary.
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
#
# `&vert;` IS THE SAME HAZARD ONE DELIMITER OVER, and it took PURIN to notice.
# A `|` inside a wikitable cell is a CELL SEPARATOR, so `&vert;` is exactly the
# escape a Wikisource editor writes to put a literal bar in a cell — the pipe
# analogue of `&lt;`.  Decoding it here, before the table is parsed, forges
# separators out of content: PURIN's chemistry diagrams draw their bonds with
# `&vert;`, and the decoded bars shattered the rows so `rowspan=3` and a column
# of loose `|` came out as visible text.  (That is why PURIN and not FULMINIC
# ACID: same table producer, only one of them draws bonds this way.)
#
# The pages are unproofread (ws679/680 are pagequality=1), but the tables there
# are well-formed — `{|` and `|}` balance on every PURIN page — so this is not a
# transcription defect ([[feedback_source_is_the_only_excuse]]).
_KEEP_ENTITY = re.compile(
    r"&(?:lt|gt|vert|verbar|VerticalLine|#0*(?:60|62|124)|#[xX]0*(?:3[ce]|7c));",
    re.IGNORECASE)


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
    i = 0
    for m in _CHROME_FURNITURE.finditer(stream):
        if m.start() < i:                 # opener inside an already-removed template
            continue
        end = template_end(stream, m.start())
        if end is None:
            # Never closes: keep it.  The walk this replaces ran to the end of
            # the stream and removed everything after the opener.
            continue
        out.append(stream[i:m.start()])
        i = end
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
    return _source_clean(stream)


# A whitespace-only line IS a blank line; strip trailing spaces/tabs so a
# space-polluted blank line ("\n \n") reads as the "\n\n" paragraph break the
# author meant (ABBEY Fig. 1 legend C/D; ~326 occurrences corpus-wide). Lossless:
# trailing whitespace carries nothing in prose, cells, verse, or tables.
_TRAILING_WS = re.compile(r"[ \t]+(?=\r?\n)")


def _source_clean(stream: str) -> str:
    """The re-appliable half of the pass — source-cleans + page-seam heals, i.e.
    everything EXCEPT the once-only raw→canonical conversions (corrections,
    quote-run).  Split out because these are idempotent and safe to re-apply: the
    transform-snapshot fixtures were captured post-quote-run but pre-clean, so
    that test applies THESE, not full ``preprocess`` (which would re-run quote-run
    on already-converted markup)."""
    # ── source cleaning — drop chrome but PRESERVE load-bearing table markers ──
    stream = _TRAILING_WS.sub("", stream)         # whitespace-only line -> clean blank line
    stream = strip_noinclude_blocks(stream)
    stream = strip_includeonly_tags(stream)        # transclusion chrome, like noinclude
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
