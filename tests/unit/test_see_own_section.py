"""A see that names one of its own article's headings links to that heading
— strictly (the whole heading, it without its numbering, or its numbering
alone), and only where no article binds (measured 2026-10-10: section-first
re-pointed real article references, and the substring tier matched junk)."""
from wikikit.export.sections import match_section
from wikikit.link_resolver import LinkResolver

SHAKESPEARE = [{"title": "VII. Concordances and Indexes", "slug": "concordances-and-indexes"},
               {"title": "VIII. Probable Sources", "slug": "probable-sources"},
               {"title": "XIX. Payne Collier Controversy", "slug": "payne-collier-controversy"}]
GEOLOGY = [{"title": "Part VI.—Palaeontological Geology", "slug": "part-vi"},
           {"title": "Part VIII.—Physiographical Geology", "slug": "part-viii"}]
INFANTRY = [{"title": "The Roman Imperial Army.", "slug": "the-roman-imperial-army"}]


def slug(secs, name):
    s = match_section(secs, name, strict=True)
    return s and s["slug"]


def test_the_name_must_be_the_heading():
    assert slug(SHAKESPEARE, "Probable Sources") == "probable-sources"     # without its numbering
    assert slug(SHAKESPEARE, "VIII. Probable Sources") == "probable-sources"
    assert slug(SHAKESPEARE, "XIX") == "payne-collier-controversy"         # its numbering alone
    assert slug(GEOLOGY, "Part VIII") == "part-viii"
    assert slug(SHAKESPEARE, "Indexes") is None                            # part of a heading
    assert slug(INFANTRY, "Oman") is None                                  # the substring tier's junk


def test_numbering_alone_must_be_unique():
    assert slug([{"title": "III. Commercial", "slug": "a"}, {"title": "III", "slug": "b"}], "III") == "b"
    assert slug([{"title": "III. A", "slug": "a"}, {"title": "III. B", "slug": "b"}], "III") is None


def resolver(sections, see_binds=None):
    r = LinkResolver.__new__(LinkResolver)          # no corpus: only what these calls read
    r.resolve_see = lambda target, display, self_fn=None, window=False: (see_binds, target if see_binds else None)
    r._article_sections = lambda fn: sections
    return r


def test_a_see_naming_its_own_heading_links_to_it():
    got = resolver(SHAKESPEARE).resolve_xref("PROBABLE SOURCES", "Probable Sources",
                                             trusted=False, self_fn="24-0802-e2cb62.json")
    assert got[:2] == ("24-0802-e2cb62.json", "probable-sources")


def test_an_article_that_binds_wins_over_the_heading():
    got = resolver([{"title": "Oudenarde.", "slug": "oudenarde"}], see_binds="20-0411-cf0286.json") \
        .resolve_xref("OUDENARDE", None, trusted=False, self_fn="x.json")
    assert got == ("20-0411-cf0286.json", None, "OUDENARDE")


def test_no_heading_no_link():
    assert resolver(INFANTRY).resolve_xref("OMAN", None, trusted=False, self_fn="x.json") == (None, None, None)
