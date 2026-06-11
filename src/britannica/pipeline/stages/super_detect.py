"""Super-walker article assembly — the honest replacement for the per-page
``detect_boundaries`` parser.

``super_detect_boundaries(volume)`` turns ``super_walk``'s boundaries into the
same ``DetectedArticle``/``SegmentInfo`` shape ``persist_articles`` consumes —
but built from RAW segment slices (recognize-on-view, carry-raw):

  * boundaries come from ``super_walk`` (which consumes nothing — it recognizes
    article-opening headings on the raw volume stream);
  * the article's content is the raw slice between consecutive boundaries;
  * the title is NOT extracted here (MOVE 2): the title rides UNSTRIPPED in
    segment 0 and is produced in exactly one place downstream —
    ``transform_articles.preprocess_article`` (which runs ``produce_title``);
  * per-page ``SegmentInfo``s fall out by splitting that raw content on the
    ``«PAGE»`` markers — segments serve only page assembly + page numbers.

No per-page parsing, no continuation-merge, no source transformation.
"""
from __future__ import annotations

import re

import britannica.pipeline.stages.super_walker as SW
from britannica.db.models import SourcePage
from britannica.db.session import SessionLocal
from britannica.pipeline.stages.detect_boundaries import (
    DetectedArticle,
    SegmentInfo,
    _split_out_plates,
)

_PAGE_RE = re.compile(r"\x01PAGE:(\d+)\x01")
_SECBEGIN = re.compile(r'<section\s+begin\s*=\s*"?([^">]*)"?\s*/?>', re.IGNORECASE)
# Section tags drop AFTER detection (the spec): detection has already read
# `<section begin>` for the stable-ID name (`section_at`), so the tags are now
# consumed transclusion chrome.  Drop each tag WITH its own line — a chrome
# construct on its own line takes its trailing newline with it, so removing it
# can't leave a blank line behind.  (Dropping only the tag is the bug that
# strands the newline as a `\n\n` once the empty SECTION element renders.)
_SECTAG_DROP = re.compile(
    r"[ \t]*<section\s+(?:begin|end)\b[^>]*?/?>[ \t]*\n?", re.IGNORECASE)


def detect_boundaries(volume: int) -> list[DetectedArticle]:
    session = SessionLocal()
    try:
        pages = (session.query(SourcePage)
                 .filter(SourcePage.volume == volume)
                 .order_by(SourcePage.page_number).all())
    finally:
        session.close()
    plates, art_pages = _split_out_plates(pages)
    art_pages = [p for p in art_pages if (p.wikitext or "").strip()]
    pid = {p.page_number: p.id for p in art_pages}

    # The CLEAN stream: preprocess(make_stream(...)) — the SAME single stream
    # super_walk slices (no second independent assembly).  This IS the article
    # content; boundaries slice it, segments fall out of the markers.
    raw_stream = SW.volume_stream(volume)
    stream = raw_stream                      # section_at reads the same stream

    arts = SW.super_walk(volume)
    sec_tags = [(m.start(), (m.group(1) or "").strip())
                for m in _SECBEGIN.finditer(stream)]

    def section_at(off: int) -> str:
        name = ""
        for so, nm in sec_tags:
            if so <= off:
                name = nm
            else:
                break
        return name

    # super_walk already stopped at each article's boundary: ``a.start`` IS the
    # byte offset (in this same ``volume_stream``) and ``a.page_start`` IS the leaf
    # it sits on (the «PAGE» marker before ``a.start``).  Use them.  We do NOT
    # re-find the article by searching the stream for its own headword — the
    # headword isn't unique (collisions — the per-page cursor was that admission),
    # it can be rewritten before the search, and on a miss the old code FABRICATED
    # a boundary from the previous article's end.  A boundary known at the walk is
    # carried, never re-derived.
    bounds = [(a.start, section_at(a.start), a.page_start) for a in arts]

    out: list[DetectedArticle] = list(plates)
    for i, (bpos, sec, pstart) in enumerate(bounds):
        end = bounds[i + 1][0] if i + 1 < len(bounds) else len(raw_stream)
        content = raw_stream[bpos:end]               # the article's raw content
        content = _SECTAG_DROP.sub("", content)      # drop consumed section chrome
        # MOVE 2: detection no longer extracts the title.  The title is
        # produced in EXACTLY ONE place — `transform_articles.preprocess_article`,
        # which runs `produce_title` on the assembled segments.  So here the
        # title rides UNSTRIPPED in segment 0; we split the full `content`
        # (not a title-stripped `body`) on the «PAGE» markers.
        body = content
        segs: list[SegmentInfo] = []
        seq = 0
        # Slap the current leaf onto each page-fragment AS WE CUT IT — the «PAGE»
        # marker is materialized HERE, where the leaf is known (``pstart`` for the
        # opening fragment, the split's own page number after each seam).
        # ``preprocess_article`` then only concatenates; it never re-stamps the leaf
        # at the article level.
        parts = _PAGE_RE.split(body)                 # [t0, pn1, t1, pn2, t2, …]
        if parts[0].strip():
            segs.append(SegmentInfo(pid[pstart], pstart, seq,
                                    f"\x01PAGE:{pstart}\x01{parts[0]}"))
            seq += 1
        for k in range(1, len(parts), 2):
            cpn = int(parts[k])
            if parts[k + 1].strip() and cpn in pid:
                segs.append(SegmentInfo(pid[cpn], cpn, seq,
                                        f"\x01PAGE:{cpn}\x01{parts[k + 1]}"))
                seq += 1
        if not segs:
            continue
        out.append(DetectedArticle(
            title="", volume=volume,
            page_start=segs[0].page_number, page_end=segs[-1].page_number,
            article_type="article", segments=segs, section_name=sec,
            title_raw=""))
    return out
