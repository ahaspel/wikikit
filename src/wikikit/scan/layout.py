"""What a book knows about a page's layout, declared rather than guessed from
the ink — `Corpus.page_layout(volume, page)`.  The cutter (`wikikit.scan.cut`)
measures the ink only where the book declares nothing.

Facts about print in general belong in the cutter (type never touches the
scan's border; type across the columns crosses a gutter).  Facts about one
book belong here: they hold for that book's pages and no other's, and a book
knows them from its own printing — Anthon prints a one-column preface and a
two-column dictionary; the DNB's lists of writers print two columns whose
leader dots mislead the gutter finder.

A field is added here when the cutter has a use for it, not before."""
from __future__ import annotations

from dataclasses import dataclass

from wikikit.corpora import current_corpus


@dataclass(frozen=True)
class PageLayout:
    #: how many columns the page prints; None: as the ink shows
    columns: int | None = None
    #: each column read in one call, not in strips: type large enough to read
    #: at the scale a whole column arrives at (`wikikit.scan.cut`)
    whole_columns: bool = False
    #: the book reads only pages set purely in columns: a page with anything
    #: across them (`cut.Plan.crossing`) is filed for special handling, never
    #: read — every way of reading across columns has failed one way or another
    columns_only: bool = False
    #: the book prints in two sizes of type: the smaller is measured from the
    #: line pitch and marked beside it in the image read (`cut.small_type`);
    #: the book's page notes say what the mark means
    marks_small_type: bool = False

    @property
    def max_h(self) -> int | None:
        """The tallest strip the cutter cuts (`cut.plan`): None, the column."""
        from wikikit.scan.cut import MAX_H
        return None if self.whole_columns else MAX_H


def layout_for(volume: int, page: int) -> PageLayout:
    """The book's declaration for one page, or nothing declared."""
    hook = current_corpus().page_layout
    return hook(volume, page) if hook else PageLayout()
