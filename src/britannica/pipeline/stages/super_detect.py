"""Super-walker article assembly — the honest replacement for the per-page
``detect_boundaries`` parser.

``super_detect_boundaries(volume)`` turns ``super_walk``'s boundaries into the
same ``DetectedArticle``/``SegmentInfo`` shape ``persist_articles`` consumes —
but built from RAW segment slices (recognize-on-view, carry-raw):

  * boundaries come from ``super_walk`` (which consumes nothing — it recognizes
    article-opening headings on the raw volume stream);
  * the article's content is the raw slice between consecutive boundaries;
  * the title comes from the title producer (``produce_title``), which also
    hands back the raw heading span for the downstream ``title_display``;
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
from britannica.pipeline.stages.elements._title import produce_title

_PAGE_RE = re.compile(r"\x01PAGE:(\d+)\x01")
_SECBEGIN = re.compile(r'<section\s+begin\s*=\s*"?([^">]*)"?\s*/?>', re.IGNORECASE)
_DROPINITIAL = re.compile(
    r"\{\{\s*(?:drop\s*initial|di)\s*[|}]", re.IGNORECASE)


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

    # The RAW stream: raw pages joined with «PAGE» markers.  This IS the article
    # content; boundaries slice it, segments fall out of the markers.
    raw_stream = "\n".join(
        f"\x01PAGE:{p.page_number}\x01{(p.wikitext or '').strip()}"
        for p in art_pages)
    pagepos = {int(m.group(1)): m.end() for m in _PAGE_RE.finditer(raw_stream)}

    stream = SW.volume_stream(volume)        # raw view — only for section_at
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

    page_markers = sorted(pagepos.items(), key=lambda kv: kv[1])

    def page_of(pos: int) -> int:
        pg = page_markers[0][0]
        for pn2, mp in page_markers:
            if mp <= pos:
                pg = pn2
            else:
                break
        return pg

    # Per-page content span [start, end) — bounds the heading search to its page.
    _marks = list(_PAGE_RE.finditer(raw_stream))
    page_span = {
        int(m.group(1)): (m.end(),
                          _marks[k + 1].start() if k + 1 < len(_marks)
                          else len(raw_stream))
        for k, m in enumerate(_marks)}

    # Anchor each heading WITHIN its page via a per-page cursor: two same-page
    # articles sharing a headword take successive occurrences; page-bounded so a
    # stray bold elsewhere can't drag the anchor off.  `\s*` after the link pipe
    # tolerates a heading whose «B» sits on the next line ([[Author:…|\n«B»…]]).
    bounds: list[tuple[int, str]] = []
    prev_pg, pc, pe = None, 0, 0
    for a in arts:
        pg = a.page_start
        if pg not in page_span:
            continue
        if pg != prev_pg:
            pc, pe = page_span[pg]
            prev_pg = pg
        pat = re.compile(
            r"(?:\[\[[^\]|]*\|\s*)?«B»" + re.escape(a.raw_heading) + r"«/B»")
        m = pat.search(raw_stream, pc, pe)
        if m:
            bpos, pc = m.start(), m.end()
        else:
            dm = _DROPINITIAL.search(raw_stream, pc, pe)
            bpos = dm.start() if dm else pc
            pc = bpos + 1
        bounds.append((bpos, section_at(a.start)))

    out: list[DetectedArticle] = list(plates)
    for i, (bpos, sec) in enumerate(bounds):
        end = bounds[i + 1][0] if i + 1 < len(bounds) else len(raw_stream)
        content = raw_stream[bpos:end]               # the article's raw content
        pstart = page_of(bpos)
        title, body, title_raw = produce_title(content)
        # Segments fall out by splitting the title-STRIPPED body on
        # «PAGE» markers.  Splitting raw `content` instead would put
        # the title-bold back into segs[0].text, duplicating it (since
        # `title` is already stored in Article.title): downstream
        # consumers would then see both the bold and the title field
        # and we'd be tempted to reach for a sweeper.  The title is
        # the chop-up's product — never re-pack it into the body.
        segs: list[SegmentInfo] = []
        seq = 0
        parts = _PAGE_RE.split(body)                 # [t0, pn1, t1, pn2, t2, …]
        if parts[0].strip():
            segs.append(SegmentInfo(pid[pstart], pstart, seq, parts[0]))
            seq += 1
        for k in range(1, len(parts), 2):
            cpn = int(parts[k])
            if parts[k + 1].strip() and cpn in pid:
                segs.append(SegmentInfo(pid[cpn], cpn, seq, parts[k + 1]))
                seq += 1
        if not segs:
            continue
        out.append(DetectedArticle(
            title=title, volume=volume,
            page_start=segs[0].page_number, page_end=segs[-1].page_number,
            article_type="article", segments=segs, section_name=sec,
            title_raw=title_raw))
    return out
