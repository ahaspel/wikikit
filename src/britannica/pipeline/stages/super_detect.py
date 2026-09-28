"""Super-walker article assembly — the honest replacement for the per-page
``detect_boundaries`` parser.

``detect_boundaries(volume)`` turns the book's article starts into the
same ``DetectedArticle``/``SegmentInfo`` shape ``persist_articles`` consumes —
but built from RAW segment slices (recognize-on-view, carry-raw):

  * boundaries come from the book's hook, ``Corpus.article_starts`` (which
    consumes nothing — EB1911's recognizes article-opening headings on the raw
    volume stream, britannica/books/eb1911/boundaries.py);
  * the article's content is the raw slice between consecutive boundaries;
  * the title is NOT extracted here (MOVE 2): the title rides UNSTRIPPED in
    segment 0 and is produced in exactly one place downstream —
    ``transform_articles.preprocess_article`` (which runs ``produce_title``);
  * the article's PAGE KEYS fall out by clipping the volume's page keys to the
    article's span — the body is never cut up, so it can never be rejoined
    wrongly ([[project_page_position_out_of_band]]).

No per-page parsing, no continuation-merge, no source transformation.
"""
from __future__ import annotations

import bisect

import britannica.pipeline.stages.super_walker as SW
from britannica.db.models import SourcePage
from britannica.db.session import SessionLocal
from britannica.corpora import current_corpus
from britannica.pipeline.stages.detect_boundaries import (
    DetectedArticle,
    SegmentInfo,
    _split_out_plates,
)

# `_PAGE_RE`, `_SECBEGIN` and `_SECTAG_DROP` are GONE.  Page tokens and section
# tags never enter the stream now — `stream_with_keys` lifts both into keys — so
# there is nothing here to split on, scan for, or drop afterwards.


def detect_boundaries(volume: int) -> list[DetectedArticle]:
    # WHICH BOOK.  Where articles START is the book's question: EB1911 by
    # typography (a bold headword at the head of a block), the DNB by its
    # explicit `<section>` runs.  A book that names no detector is refused
    # loudly — running another book's reader over it would not crash, it would
    # return plausible wrong boundaries.
    book = current_corpus()
    if book.article_starts is None:
        raise NotImplementedError(
            f"{book.key} names no article detector (Corpus.article_starts); "
            "see docs/dnb_project.md, Phase 2")
    session = SessionLocal()
    try:
        pages = SW._volume_pages(session, volume)
    finally:
        session.close()
    plates, art_pages = _split_out_plates(pages)
    art_pages = [p for p in art_pages if (p.wikitext or "").strip()]
    pid = {p.page_number: p.id for p in art_pages}

    # The CLEAN stream and its KEYS, built ONCE: the book's detector reads this
    # stream and the offsets it returns index into it (no second independent
    # assembly).  This IS the article content: boundaries slice it and nothing
    # else touches it.
    raw_stream, page_keys, section_keys = SW.volume_stream(volume)

    # One offset per article start, from the book; ordered and de-duplicated
    # here, because a boundary SET has no duplicates whatever the detector (a
    # heading can fall under two overlapping <section begin> tags).
    starts = sorted(set(book.article_starts(raw_stream, page_keys, section_keys)))

    def section_at(off: int) -> str:
        """The section `off` falls in — the last section key at or before it.

        Was a scan of the `<section begin>` tags in the stream.  They are keys
        now, so this is a bisect over data already computed."""
        i = bisect.bisect_right(section_keys, (off, "￿")) - 1
        return section_keys[i][1] if i >= 0 else ""

    # The detector already stopped at each article's boundary: each start IS the
    # byte offset (in this same ``volume_stream``), and the page it sits on is the
    # last page key at or before it.  Use them.  We do NOT
    # re-find the article by searching the stream for its own headword — the
    # headword isn't unique (collisions — the per-page cursor was that admission),
    # it can be rewritten before the search, and on a miss the old code FABRICATED
    # a boundary from the previous article's end.  A boundary known at the walk is
    # carried, never re-derived.
    bounds = [(s, section_at(s), SW._page_before(page_keys, s)) for s in starts]

    out: list[DetectedArticle] = list(plates)
    for i, (bpos, sec, pstart) in enumerate(bounds):
        end = bounds[i + 1][0] if i + 1 < len(bounds) else len(raw_stream)
        # The article's body: ONE slice of the clean stream, taken as-is.  No
        # section-chrome drop (the tags never entered the stream) and no cutting
        # into per-page pieces — the body is already whole and correct here, and
        # cutting it up was what broke the seams.
        content = raw_stream[bpos:end]
        # MOVE 2: detection does not extract the title.  It rides UNSTRIPPED at
        # the head of the body and is produced in exactly one place downstream —
        # `transform_articles.preprocess_article`, via `produce_title`.
        #
        # The article's PAGE KEYS: which pages it spans and where each begins in
        # its own body.  Derived from the volume keys by clipping to this
        # article's span — no splitting of the text, which stays whole.
        #
        # The first key is the page the article STARTS on, at offset 0: an article
        # usually begins mid-page, so its first interior key is its SECOND page.
        # A volume key landing exactly on `bpos` is that same page, so it is not
        # added twice.
        if pstart not in pid:
            continue
        keys: list[SegmentInfo] = [SegmentInfo(pid[pstart], pstart, 0, 0)]
        for off, pg in page_keys:
            if off <= bpos:
                continue
            if off >= end:
                break
            if pg in pid:
                keys.append(SegmentInfo(pid[pg], pg, len(keys), off - bpos))
        out.append(DetectedArticle(
            title="", volume=volume,
            page_start=keys[0].page_number, page_end=keys[-1].page_number,
            article_type="article", body=content, segments=keys,
            section_name=sec))
    return out
