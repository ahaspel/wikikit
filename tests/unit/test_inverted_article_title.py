"""A title that inverts its article — UNITED STATES, THE — is found by the
name without it.  Filed only under {UNITED, STATES, THE}, "United States"
missed the exact rungs and fell to subset, where an embedding chose UNITED
STATES NAVAL ACADEMY (26 links, 2026-11-06)."""
from wikikit.name_index import NameIndex

ARTS = [{"filename": "us.json", "title": "UNITED STATES, THE"},
        {"filename": "usna.json", "title": "UNITED STATES NAVAL ACADEMY"},
        {"filename": "bar.json", "title": "BAR"},
        {"filename": "bar-the.json", "title": "BAR, THE"},
        {"filename": "hague.json", "title": "HAGUE, THE"}]


def test_the_name_without_its_article_is_exact():
    ix = NameIndex(ARTS)
    assert ix.exact("United States") == [("us.json", "UNITED STATES, THE")]
    assert ix.fold_match("Hague") == [("hague.json", "HAGUE, THE")]


def test_a_real_title_keeps_its_name():
    assert NameIndex(ARTS).exact("Bar") == [("bar.json", "BAR")]
