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

from wikikit.settings import settings



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
    "patches.json",              # words and formulas laid on fetched pages (wikikit.scan.patches)
    "hyphen_map.json",           # corpus-frequency dehyphenation
    "contributor_aliases.json",  # contributor name adjudications
    "signatures.json",           # signature templates -> the initials the page prints
    "xref_adjudications.json",   # cross-reference picks made by hand
    "maps.json",                 # the map-plate bundle's manifest
    "link_exceptions.json",      # audited source links with no destination
    "genealogy_images.json",     # family-tree templates -> their scan crops
    "mdx_sample.json",           # the dictionary's compatibility sample
    "tei_odd.xml",               # the TEI customisation shipped in the TEI bundle
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
    "reference_link_overrides.json",   # the viewer's Reader's Guide link overrides
    # A book transcribed from its scans (wikikit.scan):
    "scan_notation.json",        # its own printed notation (a pronunciation key)
    "templates/scan_page.md",    # its pages and its transcription rules, in its words: (none)
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
    #: WHERE AN ARTICLE'S HEADING ENDS.  ``heading_end(opening, bold_end)`` —
    #: given the opening and the end of its bold heading run, the book says where
    #: the heading really ends.  None: the heading IS the bold run.  Books differ
    #: in what their bold marks: EB1911 bolds the whole name, so what follows the
    #: bold (``BARON``) is body; the DNB bolds only the surname it alphabetises
    #: by, and a biographical dictionary's title is its subject's full name, so
    #: its forenames run on in plain capitals (``ROTHSCHILD, LIONEL NATHAN de``).
    heading_end: Callable[[str, int], int] | None = None
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
    #: SCANS — for the pages the book transcribes itself (``wikikit.scan``,
    #: run as ``python -m wikikit.scan``).  ``page_scan(volume, page)`` is the
    #: page's image; ``page_scan_url(volume, page)`` is the scan a transcription
    #: names as its source.  Each book takes the best scan it can find, whoever
    #: holds it (``wikikit.scan.wikisource`` renders the volume's DjVu).  Both
    #: or neither: None = the book transcribes nothing.
    page_scan: Callable[[int, int], object] | None = None
    page_scan_url: Callable[[int, int], str] | None = None
    #: THE DOOR — the proofreading level a supplied page must reach before it
    #: replaces a fetched page that has text (`source_pages.prefer`).  1: as
    #: soon as it is complete, our first reading over Wikisource's raw OCR.
    #: EB1911 says 3 (user, 2026-10-15): a whole page enters only once a person
    #: has checked it at the desk — whole pages shipped unchecked brought their
    #: own errors in with them (TIDE's footnotes mid-article, its schedules
    #: ruled between every row).  A page with nothing fetched enters at once.
    supplied_replaces_text_at: int = 1
    #: QUOTATION MARKS — True: a straight pair ("…") in the text, or a pair
    #: half straight and half curly, is set curly (“…”), as the book prints
    #: them all (`preprocess.curly_quotes`).  EB1911 (user, 2026-10-16): the
    #: print makes no difference, Wikisource's transcribers and our readings
    #: mixed the two (vol 28 p. 427: “long” beside "overtides").
    curly_quotes: bool = False
    #: OUR READINGS' MARKS — True: each page we transcribe has its quotation
    #: marks and its apostrophes set curly as it is read (`supplied.curly_marks`):
    #: the book prints them so, and a straight ' beside '' or ''' is the
    #: markup's own character (the Century, user 2026-10-17: "curly is much
    #: better for us because it doesn't interfere with markup").  Off where our
    #: readings are matched against another transcription's text (EB1911's
    #: patches, against Wikisource's).
    curly_readings: bool = False
    #: OUR READINGS' CAPTIONS — True: the book prints its figures' captions and
    #: legends in smaller type, and each page we transcribe sets them so as it is
    #: read (`transcribe.small_captions`); the model reads them at the text's
    #: size (the Century, user 2026-10-18: "consistently read as normal-sized",
    #: small in every one seen).  An exception is the proofreader's to undo.
    captions_small: bool = False
    #: THE BOOK'S OWN RULES for the pages we transcribe: each ``rule(text) ->
    #: text`` sets something the book prints that a reading misses (the
    #: Century, user 2026-10-25: its etymologies set off by two spaces).  Run
    #: on a page's body wherever its text is written — assembly, a person's
    #: save at the desk (`supplied.book_rules`) — so each must change nothing
    #: it has already set.  () = none.
    text_rules: tuple = ()
    #: LAYOUT — what the book knows of a page's layout, for the cutter:
    #: ``page_layout(volume, page)`` -> ``wikikit.scan.layout.PageLayout``
    #: (its column count, …).  The cutter measures the ink only where the book
    #: declares nothing.  None = nothing declared on any page.
    page_layout: Callable[[int, int], object] | None = None
    #: SECTIONS of a transcribed page: ``page_sections(volume, page, raw_text)``
    #: -> ``([{"name", "before"}], None)`` — the `<section begin>` addresses its
    #: articles' ids hang on, and the headword each opens at (None: the page's
    #: top) — or ``(None, why)``.  ``wikikit.scan.supplied.name_sections``.
    #: None = the book's ids hang on no section names a page must keep.
    page_sections: Callable[[int, int, str], tuple] | None = None
    #: MARKUP the book prints that the engine reads but the shared transcription
    #: vocabulary does not name — its own templates, e.g. EB1911's margin
    #: heading {{EB1911 Shoulder Heading|…}}: ``wikikit.scan.vocabulary.Construct``s
    #: added to the vocabulary for this book's pages only (prompt, desk, parity
    #: gate alike).  Empty = the shared vocabulary is all the book needs.
    scan_constructs: tuple = ()
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
    #: The site (`wikikit.site`): the names its pages wear, and the folder of
    #: the book's OWN pages, served beside the engine's.
    work_name: str | None = None        # "Encyclopædia Britannica" — page titles
    edition_label: str | None = None    # "11th Edition" — titles, page headers
    monogram: str | None = None         # "EB" — the header's boxed mark
    search_key: str | None = None       # the PUBLIC search-only key the pages send
    search_index: str | None = None     # "articles" — the book's search index name
    analytics_code: str | None = None   # "britannica11" — the GoatCounter site
    volume_scans_url: str | None = None  # one volume's scans; `{vol}` = 2 digits
    contributor_example: str | None = None  # "Chisholm" — the search help's `by:`
    site_dir: str | None = None         # "tools/viewer" — the book's own pages

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

    @property
    def root(self) -> str:
        """The site's path under its host, without a trailing slash: "" for
        https://britannica11.org, "/dnb" for https://reference.aaronhaspel.com/dnb.
        Every root-relative address the site and the export write starts with it."""
        from urllib.parse import urlsplit
        return urlsplit(self.need("site")).path.rstrip("/")

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
        if (self.page_scan is None) != (self.page_scan_url is None):
            raise ValueError(f"{self.key} names only one of page_scan / page_scan_url: "
                             "a transcription must name the scan it was read from")

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


def book_root() -> Path:
    """The book's repository: the directory every command runs in.

    The book's files — its data, its derived outputs, its site pages — are found
    from HERE, never from the engine's own location: once the engine is an
    installed package, `Path(__file__)` points into the engine's checkout, not
    the book's.  It is already where the settings read `book.env` and `.env`
    from; this names it, and refuses a directory that holds no book.  The
    engine's OWN files (stylesheets, templates it ships, installer sources) are
    found from the package, beside the code that uses them.
    """
    root = Path.cwd().resolve()
    if not (root / "book.env").is_file():
        raise RuntimeError(f"{root} is not a book's repository (no book.env); "
                           "run wikikit from the book's root")
    return root


_BOOK: Corpus | None = None


def current_corpus() -> Corpus:
    """The book this process is building.

    Named by IMPORT PATH in the ``corpus`` setting — ``module:attribute``, e.g.
    ``eb1911:EB1911`` — set in the book's committed ``book.env``
    or as ``WIKIKIT_CORPUS``, beside the database URL that selects the same
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
#   uv run python -m wikikit.corpora derived articles   ->  data/derived/articles
#   uv run python -m wikikit.corpora images maps        ->  data/images/maps
# Prints the path for the book selected by settings (WIKIKIT_CORPUS), in
# POSIX form, so rebuild_all.sh and deploy.sh ask the book instead of spelling it.
if __name__ == "__main__":
    import sys
    # Through the REAL module, not this `__main__` copy: run with `-m`, this file
    # is loaded twice, and the book module's Corpus is the real module's — so
    # the isinstance check in current_corpus() would compare two classes.
    from wikikit.corpora import brand, current_corpus  # noqa: F811
    kind, *parts = sys.argv[1:] or ["derived"]
    if kind == "brand" and len(parts) == 1:     # `brand slug` -> eb1911
        print(brand(parts[0]))
    elif kind == "data" and len(parts) == 1:    # `data maps.json` -> data/maps.json
        print(current_corpus().data(parts[0]).as_posix())
    elif kind in ("derived", "images"):
        print(getattr(current_corpus(), kind)(*parts).as_posix())
    else:
        raise SystemExit("usage: python -m wikikit.corpora derived|images [part ...] | brand NAME | data FILE")
