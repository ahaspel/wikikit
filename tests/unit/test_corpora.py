"""The corpus seam.

The load-bearing test is `test_eb1911_titles_are_unchanged`: Phase 0's whole
claim is that introducing a second book moves nothing about the first, and the
page title is the one value the profile took over from a hardcoded format
string.  If that drifts, 37,000 live articles are being rebuilt from pages
fetched under different names.
"""
import pytest

from britannica import settings as settings_module
from britannica.corpora import DNB, EB1911, current_corpus


@pytest.fixture
def corpus(request):
    """Build as another book for one test, then put it back.

    The corpus is process-wide by design — one run reads one database and
    therefore one book — so a test that changes it has to restore it, or the
    next test inherits a different book.
    """
    before = settings_module.settings.corpus
    settings_module.settings.corpus = request.param
    try:
        yield request.param
    finally:
        settings_module.settings.corpus = before


def test_default_is_the_britannica():
    """Nothing that does not opt in can change behaviour."""
    assert current_corpus() is EB1911


def test_eb1911_titles_are_unchanged():
    """Byte-identical to the format string the fetcher used to hold.

    This is the gate in miniature.  The old line was:
        f"Page:EB1911 - Volume {volume:02d}.djvu/{page_number}"
    """
    for volume in range(1, 30):
        for page in (1, 7, 42, 1234):
            assert EB1911.page_title(volume, page) == (
                f"Page:EB1911 - Volume {volume:02d}.djvu/{page}")


@pytest.mark.parametrize("volume,expected", [
    # the original series — zero-padded
    (1, "Page:Dictionary of National Biography volume 01.djvu/5"),
    (63, "Page:Dictionary of National Biography volume 63.djvu/5"),
    # 1901 supplement — ROMAN numerals, and a different separator
    (64, "Page:Dictionary of National Biography. Sup. Vol I (1901).djvu/5"),
    (66, "Page:Dictionary of National Biography. Sup. Vol III (1901).djvu/5"),
    # 1912 — arabic, NOT zero-padded, and called the Second Supplement
    (67, "Page:Dictionary of National Biography, Second Supplement, volume 1.djvu/5"),
    (69, "Page:Dictionary of National Biography, Second Supplement, volume 3.djvu/5"),
    # 1927 — no volume number at all
    (70, "Page:Dictionary of National Biography, Third Supplement.djvu/5"),
    # the errata, a source in its own right
    (71, "Page:Dictionary of National Biography. Errata (1904).djvu/5"),
])
def test_dnb_names_its_volumes_five_different_ways(volume, expected):
    """A format string would have fitted the main series and silently
    mis-addressed all eight supplement volumes."""
    assert DNB.page_title(volume, 5) == expected


def test_dnb_refuses_a_volume_it_does_not_have():
    with pytest.raises(ValueError, match="volumes 1-71"):
        DNB.page_title(72, 1)


@pytest.mark.parametrize("corpus", ["dnb"], indirect=True)
def test_selecting_a_corpus_selects_its_profile(corpus):
    assert current_corpus() is DNB


@pytest.mark.parametrize("corpus", ["klingon"], indirect=True)
def test_an_unknown_corpus_is_refused_by_name(corpus):
    with pytest.raises(ValueError, match="unknown corpus"):
        current_corpus()


@pytest.mark.parametrize("corpus", ["dnb"], indirect=True)
def test_boundary_detection_refuses_a_book_it_cannot_read(corpus):
    """The DNB marks articles with `<section>` runs, not typography.

    Running the typographic reader over it would not crash — it would return a
    plausible set of wrong boundaries, which is worse.
    """
    from britannica.pipeline.stages.super_detect import detect_boundaries
    with pytest.raises(NotImplementedError, match="only 'typographic'"):
        detect_boundaries(1)


def test_every_field_is_read_by_something():
    """No placeholders.

    A declared-but-unread setting invites the next reader to believe it does
    something.  Contributor binding is deliberately NOT a field yet — it arrives
    in Phase 3 with the roster lookup that reads it.
    """
    import dataclasses
    fields = {f.name for f in dataclasses.fields(EB1911)}
    assert fields == {"key", "title", "scan_name", "boundary_style", "pages"}, (
        "a field was added or removed — is its consumer written?")


def test_the_page_manifest_covers_every_volume():
    """A missing volume means a silently short import, not an error."""
    assert EB1911.volumes == list(range(1, 30))
    assert DNB.volumes == list(range(1, 72))
    assert all(n > 0 for n in EB1911.pages.values())
    assert all(n > 0 for n in DNB.pages.values())


def test_eb1911_page_counts_match_the_array_they_replaced():
    """Verbatim, not re-derived.

    They lived in a bash array in fetch_all.sh.  ARTICLE_WS_RANGE looks like the
    same fact and is not — it is the span holding ARTICLES, and it disagrees for
    volumes 20 and 29, the index volume being absent from it entirely.
    """
    was = [0, 1029, 1027, 1015, 1031, 1002, 1017, 1008, 1027, 997, 967, 968,
           985, 985, 953, 994, 1016, 1039, 1000, 1034, 1054, 1019, 993, 1069,
           1100, 1090, 1104, 1092, 1091, 982]
    assert [EB1911.pages[v] for v in range(1, 30)] == was[1:]


def test_dnb_totals_match_what_was_measured():
    """33,824 pages, read from the DjVu files through the Wikisource API."""
    assert sum(DNB.pages.values()) == 33_824
    assert sum(DNB.pages[v] for v in range(1, 64)) == 29_232   # the original series
    assert sum(DNB.pages[v] for v in range(64, 67)) == 1_510   # 1901
    assert sum(DNB.pages[v] for v in range(67, 70)) == 2_118   # 1912
    assert DNB.pages[70] == 650                                # 1927
    assert DNB.pages[71] == 314                                # the Errata
