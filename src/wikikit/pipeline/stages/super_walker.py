"""The volume stream — the single input to article detection AND the walker.

A volume is a closed interval on article boundaries (its first byte is an
article's start, its last byte is an article's end; no article straddles a
volume boundary).  So article detection is: lay out the whole volume's pages
as one stream and cut at each article start.  A continuation page (no new
article) just flows into the current article.  No per-page parsing, no
continuation-merge.

This module builds that stream.  WHERE articles start is the book's question
(``Corpus.article_starts``) — EB1911's typographic title-block scan lives in
eb1911/boundaries.py since wikikit step 5.
"""

from __future__ import annotations

import bisect

from wikikit.db.models import SourcePage
from wikikit.db.session import SessionLocal
from wikikit.pipeline.stages.detect_boundaries import _split_out_plates
from wikikit.pipeline.stages.preprocess import stream_with_keys
from wikikit.volumes import article_ws_range


def _page_before(page_keys: list[tuple[int, int]], pos: int) -> int:
    """The page `pos` sits on — the last key at or before it.

    Was a backwards scan for the nearest `\\x01PAGE:` token.  The token is gone
    from the stream (page position is carried as KEYS now,
    [[project_page_position_out_of_band]]), so this is a bisect over data that is
    already computed — same answer, and it cannot be fooled by a token that a
    producer moved or ate.
    """
    if not page_keys:
        return 0
    i = bisect.bisect_right(page_keys, (pos, float("inf"))) - 1
    return page_keys[i][1] if i >= 0 else 0


def _volume_pages(session, volume: int) -> list:
    """The volume's SourcePages constrained to its article-leaf range
    (``Corpus.article_pages``): front matter and back matter never enter the
    gather, so step 1 is article/plate ONLY — not "gather every page and let the
    front matter fall off as the unclaimed lead before the first heading".  A
    volume with no recorded range (e.g. vol 29) admits every page."""
    q = session.query(SourcePage).filter(SourcePage.volume == volume)
    rng = article_ws_range(volume)
    if rng is not None:
        q = q.filter(SourcePage.page_number >= rng[0],
                     SourcePage.page_number <= rng[1])
    return q.order_by(SourcePage.page_number).all()


def volume_stream(
    volume: int,
) -> tuple[str, list[tuple[int, int]], list[tuple[int, str]]]:
    """The clean, frozen stream for ``volume`` plus its KEYS — the single input
    to boundary detection AND the element walker.

    Returns ``(stream, page_keys, section_keys)``.  The stream carries NO
    positional chrome: no ``\\x01PAGE:N\\x01`` markers and no ``<section …>``
    tags.  Both were only ever annotations about WHERE things are, and both are
    now keys — ``[(offset, page_number)]`` and ``[(offset, section_name)]``
    ([[project_page_position_out_of_band]]).

    That makes the stream FINAL: nothing downstream edits it, so every offset
    stays valid, and no recognizer can trip over a marker that isn't content.
    Page-split words are still NOT rejoined here — they reconstruct downstream in
    the split-word producer.
    """
    session = SessionLocal()
    try:
        all_pages = _volume_pages(session, volume)
        # Plates are lifted first; article detection runs over the plate-free
        # pages.  Front matter never reaches here — `_volume_pages` constrained
        # the gather to the article-leaf range.
        _plates, pages = _split_out_plates(all_pages)
        return stream_with_keys(pages, volume)
    finally:
        session.close()
