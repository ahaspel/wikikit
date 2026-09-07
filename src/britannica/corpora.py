"""Which book are we building?

A new corpus is a different BUILD, not a different codebase — its closest
analogue here is the EPUB build or the markdown build.  One repository, one
pipeline, two books.

THE SEAM IS THE DATABASE, NOT THE CALL CHAIN.  The corpus is selected where the
database is already selected, in ``Settings``, and every site that needs a
corpus-specific value asks ``current_corpus()`` for the one value it needs.  The
alternative — threading a ``Corpus`` through ``detect_boundaries`` →
``walk_article`` → ``process_elements`` → the producers — would change the
signature of exactly the shared code that builds 37,000 live EB1911 articles,
which is the blast radius this phase exists to avoid.  A build run reads one
database and therefore one book; making the corpus more configurable than the
data it reads would buy flexibility nothing needs.

WHAT BELONGS HERE.  Only values that genuinely differ between books.  Most
`EB1911 *` strings in the tree are the Britannica's own template vocabulary —
``EB1911 fine print``, ``EB1911 sfrac`` — which the DNB simply never matches;
parameterising those would be inventing a difference rather than recording one.

WHAT DOES NOT BELONG HERE.  Recognition.  The DNB ends its bold headword at the
comma (``«B»JOHNSON,«/B» SAMUEL``) where EB1911 carries the whole headword
inside the bold.  That is `_title_span` needing to RECOGNISE MORE, not a policy
switch, and it has to be EB1911-neutral on its own merits.
[[feedback_recursion_is_recognition]]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from britannica.settings import settings

_ROMAN = {1: "I", 2: "II", 3: "III"}


# --- how many scanned pages each volume has ----------------------------------
# The fetch range.  EB1911's numbers were a bash array inside fetch_all.sh, a
# poor home for a manifest: it could not be tested, imported, or checked against
# anything.  They are reproduced here VERBATIM, not re-derived from
# ARTICLE_WS_RANGE, which is a DIFFERENT fact — the span holding articles — and
# disagrees for volumes 20 and 29 (volume 29, the index, is not in it at all).
#
# The DNB's were measured from the DjVu files themselves through the Wikisource
# API, all 71 volumes resolving, 33,824 pages in total.

_EB1911_PAGES = {
    1: 1029, 2: 1027, 3: 1015, 4: 1031, 5: 1002, 6: 1017,
    7: 1008, 8: 1027, 9: 997, 10: 967, 11: 968, 12: 985,
    13: 985, 14: 953, 15: 994, 16: 1016, 17: 1039, 18: 1000,
    19: 1034, 20: 1054, 21: 1019, 22: 993, 23: 1069, 24: 1100,
    25: 1090, 26: 1104, 27: 1092, 28: 1091, 29: 982,
}

_DNB_PAGES = {
    1: 504, 2: 472, 3: 474, 4: 472, 5: 462, 6: 490,
    7: 465, 8: 466, 9: 474, 10: 466, 11: 482, 12: 463,
    13: 462, 14: 467, 15: 468, 16: 430, 17: 458, 18: 454,
    19: 453, 20: 454, 21: 452, 22: 460, 23: 460, 24: 468,
    25: 468, 26: 454, 27: 441, 28: 450, 29: 463, 30: 452,
    31: 454, 32: 451, 33: 453, 34: 456, 35: 454, 36: 453,
    37: 476, 38: 461, 39: 461, 40: 457, 41: 463, 42: 470,
    43: 462, 44: 464, 45: 472, 46: 461, 47: 456, 48: 449,
    49: 504, 50: 468, 51: 473, 52: 424, 53: 491, 54: 453,
    55: 494, 56: 459, 57: 467, 58: 477, 59: 465, 60: 475,
    61: 482, 62: 457, 63: 466, 64: 500, 65: 468, 66: 542,
    67: 678, 68: 702, 69: 738, 70: 650, 71: 314,
}


@dataclass(frozen=True)
class Corpus:
    """One book's worth of difference.

    ``scan_name`` maps a volume number to the DjVu file the pages live in.  It
    is a FUNCTION, not a format string, because the DNB's 71 volumes are named
    five different ways — 63 zero-padded, three Roman-numbered supplements,
    three arabic ones, a supplement with no number at all, and an errata volume.
    A format string would have fit the main series and quietly mis-addressed
    every supplement.
    """

    key: str
    title: str
    scan_name: Callable[[int], str]
    #: How articles are separated: EB1911 by typography, the DNB by explicit
    #: ``<section>`` runs its transcribers marked.
    boundary_style: str
    #: volume -> how many scanned pages it has.  Read by the fetch orchestrator.
    pages: dict[int, int]
    #: where this book's fetched pages live on disk.  EB1911's is a legacy
    #: name — 'wikisource', from when there was only one book — and is kept
    #: because 29,688 files already sit there and renaming them would buy
    #: tidiness at the cost of a needless mass move.
    raw_dir: str

    # A FIELD ARRIVES WHEN ITS CONSUMER DOES.  Every field here is read by
    # working code; none is a placeholder for a later phase.  A declared-but-
    # unread setting invites the next reader to believe it does something, and
    # the phase that finally wires it inherits a decision nobody tested.
    #
    # Contributor binding is the clearest example and is NOT here yet.  EB1911
    # puts the name INSIDE `{{EB1911 footer initials|Name|Initials}}`; the DNB
    # puts it in the template's NAME, `{{DNB AWW}}`, and looks it up in a
    # roster.  That is a different mechanism, not a different pattern — a regex
    # swap would hand the DNB a reader hunting a field it does not have — so it
    # arrives in Phase 3 with the roster lookup that reads it.
    #
    # `page_head_re` is absent for the opposite reason: there is no difference to
    # record.  The running-head pattern is already the union
    # `rh|running header|eb1911 page heading`, the DNB uses the first two, and
    # the third simply never occurs in its text.  A narrower copy for the DNB
    # would invent a difference and leave two patterns to drift apart.

    @property
    def volumes(self) -> list[int]:
        """Every volume in this book, in order."""
        return sorted(self.pages)

    def page_title(self, volume: int, page: int) -> str:
        """The Wikisource ``Page:`` title for one scanned page."""
        return f"Page:{self.scan_name(volume)}/{page}"


# --- the Britannica -----------------------------------------------------------
# Every value below is the constant that was already in the tree, moved here
# unchanged.  Phase 0's gate is a full rebuild that diffs to ZERO bytes, and
# that is only provable if this profile reproduces the old behaviour exactly.

EB1911 = Corpus(
    key="eb1911",
    title="Encyclopædia Britannica, Eleventh Edition",
    scan_name=lambda v: f"EB1911 - Volume {v:02d}.djvu",
    boundary_style="typographic",
    pages=_EB1911_PAGES,
    raw_dir="wikisource",
)


# --- the Dictionary of National Biography -------------------------------------
# Volume numbers are OURS, not the book's, because the pipeline is keyed on
# `volume: int` throughout and the DNB's own numbering restarts at each
# supplement.  The mapping is the corpus profile's whole job.
#
#    1-63  the original 1885-1900 series
#   64-66  the 1901 supplement          (the book calls them Sup. Vol I-III)
#   67-69  the 1912 supplement          (the book calls it the Second Supplement)
#      70  the 1927 supplement          (the Third Supplement, one volume)
#      71  the 1904 Errata
#
# The errata volume is a source in its own right: 314 transcribed pages holding
# 3,469 corrections, each already keyed to its article by a `<section begin=>`
# its transcribers wrote.  We append it as apparatus exactly as Wikisource does
# — the 1885 text stays as printed and the correction travels beside it.
# Rewriting the body would destroy the distinction between what the first
# edition said and what its editors later corrected.

def _dnb_scan(volume: int) -> str:
    if 1 <= volume <= 63:
        return f"Dictionary of National Biography volume {volume:02d}.djvu"
    if 64 <= volume <= 66:
        return f"Dictionary of National Biography. Sup. Vol {_ROMAN[volume - 63]} (1901).djvu"
    if 67 <= volume <= 69:
        return f"Dictionary of National Biography, Second Supplement, volume {volume - 66}.djvu"
    if volume == 70:
        return "Dictionary of National Biography, Third Supplement.djvu"
    if volume == 71:
        return "Dictionary of National Biography. Errata (1904).djvu"
    raise ValueError(f"the DNB has volumes 1-71; got {volume}")


DNB = Corpus(
    key="dnb",
    title="Dictionary of National Biography",
    scan_name=_dnb_scan,
    boundary_style="sections",
    pages=_DNB_PAGES,
    raw_dir="dnb",
)


_REGISTRY = {c.key: c for c in (EB1911, DNB)}


def current_corpus() -> Corpus:
    """The book this process is building.

    Set with ``BRITANNICA_CORPUS`` (or ``corpus`` in ``.env``), alongside the
    database URL that selects the same book's pages.  Defaults to the
    Britannica, so nothing that does not opt in can change behaviour.
    """
    try:
        return _REGISTRY[settings.corpus]
    except KeyError:
        raise ValueError(
            f"unknown corpus {settings.corpus!r}; "
            f"expected one of {', '.join(sorted(_REGISTRY))}"
        ) from None
