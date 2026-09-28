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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from britannica.settings import settings



# --- the book's data files ----------------------------------------------------
# EVERY per-book data file the ENGINE knows how to use.  A book declares which
# of these it has; the engine asks for one by name and never builds a path.
#
# DECLARED, NOT DISCOVERED (the user's rule).  Before this, each reader built
# `Path("data/…")` itself and two of them treated a missing file as "no data" —
# so a misnamed corrections file looked exactly like a book with no
# corrections, and the DNB, sharing the path, would have been fed EB1911's.
# Now both ends are checked: a book may declare only names on this list, the
# engine may ask only for names on this list, a declared file must exist, and
# an undeclared one means the feature is off for that book.
KNOWN_DATA = frozenset({
    "corrections.json",          # source typos, applied as the page is read
    "hyphen_map.json",           # corpus-frequency dehyphenation
    "contributor_aliases.json",  # contributor name adjudications
    "xref_adjudications.json",   # cross-reference picks made by hand
    "maps.json",                 # the map-plate bundle's manifest
    "link_exceptions.json",      # audited source links with no destination
    "genealogy_images.json",     # family-tree templates -> their scan crops
    "mdx_sample.json",           # the dictionary's compatibility sample
    # PROSE THE BOOK WRITES, as templates the engine fills (see `template`).
    # Each line names the $placeholders the engine supplies.
    "templates/tei_readme.md",   # $article_count $site $slug $concept_doi
    "templates/tei_source.xml",  # $volume $pages — the printed book, per article
    "templates/tei_corpus_source.xml",  # (none) — the printed book, for the catalogue
    "epub_cover.jpg",            # the EPUB's cover: a finished image, used as-is
    "templates/mdx_description.html",         # the reader's Dictionary info:
    "templates/mdx_description_sample.html",  #   $articles $contributors $illustrations $headwords $site
    "templates/mdx_about.html",        # the help page's paragraph about the book: $site $host
    "templates/mdx_help_sample.html",  # the sample's help heading + intro: $short_name
    "mdx_phrases.json",          # the book's own nouns inside the engine's README sentences
    "fake_recursion_exceptions.json",  # findings the recursion audit acknowledges
    "reference_link_overrides.json",   # the viewer's Reader's Guide link overrides
})


_TEMPLATES: dict[tuple[str, str], str] = {}


def _check_known(name: str) -> None:
    if name not in KNOWN_DATA:
        raise KeyError(f"{name!r} is not a book data file the engine knows "
                       f"(known: {', '.join(sorted(KNOWN_DATA))})")


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
    #: volume -> how many scanned pages it has.  Read by the fetch orchestrator.
    pages: dict[int, int]
    #: where this book's fetched pages live on disk.  EB1911's is a legacy
    #: name — 'wikisource', from when there was only one book — and is kept
    #: because 29,688 files already sit there and renaming them would buy
    #: tidiness at the cost of a needless mass move.
    raw_dir: str
    #: WHERE ARTICLES START — the book's boundaries hook.  Given one volume's
    #: stream and its keys (``[(offset, page)]``, ``[(offset, section)]``), the
    #: offsets at which an article begins, in any order.  The engine builds the
    #: stream, orders and de-duplicates the offsets, and does everything after.
    #: EB1911 answers by typography (a bold headword at the head of a block);
    #: the DNB will answer from its explicit ``<section>`` runs.  None = the
    #: book has no detector yet, and detection refuses it loudly.
    article_starts: Callable[[str, list, list], list[int]] | None = None
    #: PLATES — the inserts hook, a classifier and its producer.  ``is_plate``
    #: reads one raw leaf and says whether it is a plate insert (a page that
    #: stands apart from the running text as its own article); ``plate_title``
    #: names it, given the leaf, its volume and page.  Both or neither: a book
    #: that names no plate test has no plates, and every page is text.
    is_plate: Callable[[str], bool] | None = None
    plate_title: Callable[[str, int, int], str] | None = None
    #: ALIASES — the book's extra names for its articles.  Each source returns
    #: ``{alias: canonical title}``; the resolver's overlay merges them IN ORDER
    #: (a later source wins a shared alias) and abstains on an alias that lands
    #: on two articles.  The engine offers one generic source,
    #: ``xrefs.alias_table.build_section_alias_map``; a book lists it if it wants
    #: it.  Empty = no overlay beyond the article titles themselves.
    alias_sources: tuple[Callable[[], dict[str, str]], ...] = ()
    #: CONTRIBUTORS — who wrote which article.  ``bind_contributors(session,
    #: payloads)`` fills each payload's ``contributors`` IN MEMORY and writes the
    #: roster (``contributors.json``); it returns False to tell the post-export
    #: pass to write nothing (a dry run).  Coarse on purpose: EB1911 binds from
    #: initials-only signatures and two indices of its own, the DNB names its
    #: contributor in the template, and nothing about the procedure is shared.
    #: None = no bylines; the export's empty lists stand.
    bind_contributors: Callable[[object, dict], bool] | None = None
    #: FRONT MATTER — the book's pages beyond its articles: an Introduction
    #: group, an optional guide tree, the images they use and the files they
    #: are built from (``epub.front_matter.Ancillary``).  Read by the EPUB and
    #: the dictionary through ``epub.front_matter.book_pages``.  None = none.
    ancillary: Callable[[], object] | None = None
    #: volume -> the inclusive (first, last) page span that holds ARTICLES, in
    #: the book's page space (``SourcePage.page_number``).  Pages outside it are
    #: front or back matter and never enter the walk.  A volume not listed
    #: admits every page.  Read through ``volumes.article_ws_range``.
    article_pages: dict[int, tuple[int, int]] = field(default_factory=dict)
    #: Records the SOURCE leaves empty, each with the reason recorded for it.
    #: The dictionary excludes them, and its build refuses to proceed unless
    #: the empty records are exactly these (a new empty record is news).
    empty_records: dict[str, str] = field(default_factory=dict)
    #: The volume published as the free single-volume EPUB sampler (deploy.sh).
    sampler_volume: int | None = None
    #: the book's data files, from ``KNOWN_DATA``, and the folder they live in.
    data_files: frozenset[str] = field(default_factory=frozenset)
    data_dir: str = "data"
    #: Branding — the book's NAMES for itself, as values the engine fills in.
    #: None until a book has one (the DNB has no domain yet: an open decision),
    #: and reading an absent one raises through `need`, so a missing name can
    #: never print as "None" in a published file.
    site: str | None = None         # "https://britannica11.org"
    short_name: str | None = None   # "Britannica 11"  — the dictionary's name
    file_stem: str | None = None    # "Britannica11"   — dictionary file names
    slug: str | None = None         # "eb1911"         — archives, CSS scope, EPUB name
    key_prefix: str | None = None   # "EB1911:"        — dictionary's internal keys
    urn: str | None = None          # "urn:britannica11" — EPUB identifiers
    source_url: str | None = None   # the work's Wikisource page — EPUB dc:source
    search_name: str | None = None  # "Britannica title search" — the reader's
                                    # label for the enhanced edition's title search
    concept_doi: str | None = None  # the edition's Zenodo CONCEPT DOI — TEI, EPUB
    #: Single-volume builds the cover image is right for.  EB1911's cover is a
    #: photograph of VOLUME I's title page, so it serves the complete edition
    #: and the vol-1 sampler, and would mislabel any other volume's book.
    cover_volumes: frozenset[int] = field(default_factory=frozenset)
    #: Where this book's OUTPUTS go (the export, bundles, reports, caches) and
    #: where its images are stored.  No default: two books sharing a root is
    #: exactly how a DNB export would overwrite EB1911's articles, so each book
    #: must name its own.  Read through `derived()` / `images()`, never spelled.
    derived_dir: str | None = None
    images_dir: str | None = None
    #: The work's own subtitle and years, and how many of its volumes hold
    #: articles (EB1911: 28; its 29th is the index) — the EPUB title page.
    subtitle: str | None = None
    years: str | None = None
    article_volumes: int | None = None

    def data_path(self, name: str) -> Path:
        """Where a declared data file LIVES, whether or not it exists yet — for
        the tool that writes it (build_hyphen_map writes hyphen_map.json).
        Readers use `data()`, which also requires the file to be there."""
        _check_known(name)
        if name not in self.data_files:
            raise LookupError(f"{self.key} does not declare {name}")
        return Path(self.data_dir) / name

    def template(self, name: str, **values) -> str:
        """Fill one of the book's declared prose templates.

        The BOOK writes the words; the engine supplies the values.  Placeholders
        are `$name` (string.Template), not `{name}`: these texts quote BibTeX,
        JSON and CSS, all made of braces.  Substitution is STRICT — a
        placeholder the engine does not supply raises instead of leaving a hole
        in a published file — and the file is read VERBATIM, so it ends exactly
        where the text ends.  A literal dollar sign is written `$$`."""
        from string import Template
        cache_key = (self.key, name)
        text = _TEMPLATES.get(cache_key)
        if text is None:
            text = _TEMPLATES[cache_key] = self.data(name).read_text(encoding="utf-8")
        return Template(text).substitute(values)

    def derived(self, *parts) -> Path:
        """A path under this book's output root — `book.derived("articles")`."""
        return Path(self.need("derived_dir"), *parts)

    def images(self, *parts) -> Path:
        """A path under this book's image store."""
        return Path(self.need("images_dir"), *parts)

    def need(self, name: str) -> str:
        """A branding value this book must have for the artifact asking."""
        value = getattr(self, name)
        if value is None:
            raise LookupError(f"{self.key} has no {name} — it cannot build what asked for one")
        return value

    def __post_init__(self):
        unknown = set(self.data_files) - KNOWN_DATA
        if unknown:
            raise ValueError(f"{self.key} declares unknown data files: {sorted(unknown)}")
        if (self.is_plate is None) != (self.plate_title is None):
            raise ValueError(f"{self.key} names only one of is_plate / plate_title: "
                             "a plate the book can recognize but not name, or the "
                             "reverse, is half a hook")

    def has_data(self, name: str) -> bool:
        """Does this book have ``name``?  False means the feature is off."""
        _check_known(name)
        return name in self.data_files

    def data(self, name: str) -> Path:
        """The path of a data file this book DECLARES.  Asking for one it does
        not declare is a bug in the caller (check ``has_data`` first), and a
        declared file that is missing is a broken book — both raise."""
        path = self.data_path(name)             # the declaration check, once
        if not path.is_file():
            raise FileNotFoundError(f"{self.key} declares {name}, but {path} does not exist")
        return path

    # A FIELD ARRIVES WHEN ITS CONSUMER DOES.  Every field here is read by
    # working code; none is a placeholder for a later phase.  A declared-but-
    # unread setting invites the next reader to believe it does something, and
    # the phase that finally wires it inherits a decision nobody tested.
    #
    # Contributor binding shows why a hook is a whole PROCEDURE, not a pattern.
    # EB1911 puts the name INSIDE `{{EB1911 footer initials|Name|Initials}}`;
    # the DNB puts it in the template's NAME, `{{DNB AWW}}`, and looks it up in
    # a roster.  A regex swap would hand the DNB a reader hunting a field it
    # does not have, so `bind_contributors` hands the book the whole job.
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




def brand(name: str) -> str:
    """One branding value of the book being built — the ONE place the engine
    learns a name.  The site's URL was five separate module constants."""
    return current_corpus().need(name)


_BOOK: Corpus | None = None


def current_corpus() -> Corpus:
    """The book this process is building.

    Named by IMPORT PATH in the ``corpus`` setting — ``module:attribute``, e.g.
    ``britannica.books.eb1911:EB1911`` — set in the book's committed ``book.env``
    or as ``BRITANNICA_CORPUS``, beside the database URL that selects the same
    book's pages.  The engine imports the book; it never lists books it knows,
    and there is no default: a process that has not been told which book it is
    building must not quietly build the Britannica.
    """
    global _BOOK
    if _BOOK is None or _BOOK_PATH[0] != settings.corpus:
        import importlib
        module, _, attr = settings.corpus.partition(":")
        if not module or not attr:
            raise ValueError(f"the corpus setting must be 'module:attribute'; got {settings.corpus!r}")
        book = getattr(importlib.import_module(module), attr)
        if not isinstance(book, Corpus):
            raise TypeError(f"{settings.corpus} is a {type(book).__name__}, not a Corpus")
        _BOOK, _BOOK_PATH[0] = book, settings.corpus
    return _BOOK


_BOOK_PATH = [None]   # the setting _BOOK was resolved from: re-resolve if it changes


# ── for shell scripts, which cannot import this module ──────────────────────
#   uv run python -m britannica.corpora derived articles   ->  data/derived/articles
#   uv run python -m britannica.corpora images maps        ->  data/images/maps
# Prints the path for the book selected by settings (BRITANNICA_CORPUS), in
# POSIX form, so rebuild_all.sh and deploy.sh ask the book instead of spelling it.
if __name__ == "__main__":
    import sys
    # Through the REAL module, not this `__main__` copy: run with `-m`, this file
    # is loaded twice, and the book module's Corpus is the real module's — so
    # the isinstance check in current_corpus() would compare two classes.
    from britannica.corpora import brand, current_corpus  # noqa: F811
    kind, *parts = sys.argv[1:] or ["derived"]
    if kind == "brand" and len(parts) == 1:     # `brand slug` -> eb1911
        print(brand(parts[0]))
    elif kind == "data" and len(parts) == 1:    # `data maps.json` -> data/maps.json
        print(current_corpus().data(parts[0]).as_posix())
    elif kind in ("derived", "images"):
        print(getattr(current_corpus(), kind)(*parts).as_posix())
    else:
        raise SystemExit("usage: python -m britannica.corpora derived|images [part ...] | brand NAME | data FILE")
