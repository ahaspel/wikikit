"""The corpus seam — the ENGINE's side: what a book profile is and how the
engine selects, checks and refuses one.

Every test here runs against `fixture_book`, which holds nothing of any real
book, so none of it can pass because EB1911 happens to be configured.  The
books' own facts — EB1911's page titles and counts, the DNB's five volume
namings — are in test_corpora_books.py.
"""
import dataclasses

import pytest

import fixture_book
from fixture_book import BARE
from wikikit.corpora import KNOWN_DATA, Corpus, current_corpus


@pytest.mark.parametrize("corpus", ["fixture_book:BARE"], indirect=True)
def test_selecting_a_corpus_selects_its_profile(corpus):
    assert current_corpus() is fixture_book.BARE


@pytest.mark.parametrize("corpus,error", [
    ("klingon", ValueError),                       # not module:attribute
    ("klingon:K", ModuleNotFoundError),
    ("fixture_book:NOT_A_BOOK", TypeError),        # not a Corpus
], indirect=["corpus"])
def test_a_book_that_cannot_be_used_is_refused(corpus, error):
    with pytest.raises(error):
        current_corpus()


@pytest.mark.parametrize("corpus", ["fixture_book:BARE"], indirect=True)
def test_boundary_detection_refuses_a_book_it_cannot_read(corpus):
    """A book that names no article detector is refused.

    Running some other book's reader over it would not crash — it would return
    a plausible set of wrong boundaries, which is worse.
    """
    from wikikit.pipeline.stages.super_detect import detect_boundaries
    with pytest.raises(NotImplementedError, match="names no article detector"):
        detect_boundaries(1)


def test_a_plate_hook_comes_whole():
    """A book that can recognize a plate but not name it — or the reverse — is
    refused when the profile is made, not when the first plate turns up."""
    whole = dataclasses.replace(BARE, is_plate=lambda raw: False,
                                plate_title=lambda raw, v, p: "")
    with pytest.raises(ValueError, match="only one of is_plate / plate_title"):
        dataclasses.replace(whole, plate_title=None)
    with pytest.raises(ValueError, match="only one of is_plate / plate_title"):
        dataclasses.replace(BARE, is_plate=lambda raw: False)


def test_every_field_is_read_by_something():
    """No placeholders.

    A declared-but-unread setting invites the next reader to believe it does
    something.
    """
    fields = {f.name for f in dataclasses.fields(Corpus)}
    # data_files / data_dir: read by `data()` and `has_data()`, which the eight
    # book-data readers call (corrections, hyphen map, contributor aliases,
    # xref adjudications, maps, link exceptions, genealogy crops, MDX sample).
    # Branding, via `brand()` / `need()`:
    #   site        mdx.build, epub.front_matter, epub.pack, export.tei,
    #               export.download, mdx.navigation (host check)
    #   short_name  the dictionary's title, sample note, help word
    #   file_stem   dictionary_basename (the .mdx/.mdd/.png names)
    #   slug        the dictionary's CSS scope and wrap(), EPUB file name,
    #               corpus / maps / TEI archive names
    #   key_prefix  the dictionary's internal keys (mdx.build)
    #   urn         EPUB identifiers
    #   source_url  EPUB dc:source
    #   search_name the installers' program label (via the manifest `book` block)
    #   concept_doi TEI headers and catalogue, the TEI readme, EPUB dc:relation
    #   cover_volumes  epub.build.install_cover — which builds the cover fits
    #   subtitle, years, article_volumes  the EPUB title page and closing
    #   derived_dir, images_dir  `derived()` / `images()` — every output and
    #               image path in the engine
    #   article_starts  super_detect.detect_boundaries — the boundaries hook
    #   is_plate, plate_title  detect_boundaries._split_out_plates — the inserts hook
    #   alias_sources  link_resolver._overlay_aliases — the aliases hook
    #   bind_contributors  wikikit.pipeline.post_export — the contributors hook
    #   ancillary   epub.front_matter.book_pages (EPUB + dictionary) — front matter
    #   article_pages  volumes.article_ws_range / in_article_range — the walk's span
    #   empty_records  mdx.build (the gate + exclusions), mdx.release (the count)
    #   sampler_volume  the book's deploy — the sampler EPUB it builds and uploads
    assert fields == {"key", "title", "scan_name", "article_starts",
                      "is_plate", "plate_title", "alias_sources",
                      "bind_contributors", "ancillary",
                      "article_pages", "empty_records", "sampler_volume",
                      "pages", "raw_dir", "data_files", "data_dir",
                      "site", "short_name", "file_stem", "slug", "key_prefix",
                      "urn", "source_url", "search_name", "concept_doi",
                      "cover_volumes", "subtitle", "years", "article_volumes",
                      "derived_dir", "images_dir"}, (
        "a field was added or removed — is its consumer written?")


def test_a_book_without_a_name_cannot_print_none():
    """A book with no domain yet: asking for one must fail, not print 'None'
    into a published file."""
    with pytest.raises(LookupError):
        BARE.need("site")
    assert dataclasses.replace(BARE, site="https://fixture.example").need("site") \
        == "https://fixture.example"


def test_book_data_is_declared_at_both_ends(tmp_path):
    """The user's rule: declared, never discovered.  A missing file used to
    read as "no data" — corrections silently unapplied looked exactly like a
    book that needed none."""
    # A book that declares nothing is handed nothing.
    assert not BARE.data_files
    assert not BARE.has_data("corrections.json")
    with pytest.raises(LookupError):
        BARE.data("corrections.json")
    # An engine-side typo cannot quietly turn a feature off.
    with pytest.raises(KeyError):
        BARE.has_data("correction.json")
    # A book-side typo cannot be declared at all.
    with pytest.raises(ValueError):
        dataclasses.replace(BARE, data_files=frozenset({"corections.json"}))
    # Declared but absent is a broken book, not an empty feature.
    ghost = dataclasses.replace(BARE, data_files=frozenset({"maps.json"}), data_dir="no/such/dir")
    with pytest.raises(FileNotFoundError):
        ghost.data("maps.json")
    # Declared and present is found.
    (tmp_path / "maps.json").write_text("{}", encoding="utf-8")
    real = dataclasses.replace(BARE, data_files=frozenset({"maps.json"}), data_dir=str(tmp_path))
    assert real.data("maps.json").is_file()
    assert "maps.json" in KNOWN_DATA


@pytest.mark.parametrize("corpus", ["fixture_book:BARE"], indirect=True)
def test_a_book_reads_from_its_own_directory(corpus):
    """Two books, two trees — the same separation the databases have."""
    from wikikit.source_pages import raw_dir, volume_dir, page_filename
    assert raw_dir().as_posix() == "data/raw/fixture"
    assert volume_dir(2).as_posix() == "data/raw/fixture/vol_02"
    assert page_filename(3, 42) == "vol03-page0042.json"
