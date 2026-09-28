"""Per-volume article ranges — which pages of a volume hold its articles.

The span is the BOOK's fact (``Corpus.article_pages``): EB1911's table, verified
leaf by leaf against the scans, lives in its profile.  The walk processes ONLY
the article span, so front matter never enters the element walk and no
downstream pass has to neutralise it.  A volume with no recorded span (e.g.
EB1911's vol 29, the Index) admits every page.
"""
from __future__ import annotations

from britannica.corpora import current_corpus


def article_ws_range(volume: int) -> tuple[int, int] | None:
    """The inclusive ``(first_ws, last_ws)`` article span for ``volume``,
    or ``None`` for a volume with no recorded range (e.g. vol 29, the Index)."""
    return current_corpus().article_pages.get(volume)


def in_article_range(volume: int, page_number: int) -> bool:
    """True if ``page_number`` is within ``volume``'s article span.  A volume
    with no recorded range admits every page (no front/back matter to exclude)."""
    span = current_corpus().article_pages.get(volume)
    if span is None:
        return True
    return span[0] <= page_number <= span[1]
