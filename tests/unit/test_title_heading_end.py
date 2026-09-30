"""Where a heading ends is the BOOK's to say (`Corpus.heading_end`).

With no hook, the heading is the bold run and nothing after it — EB1911's
MOUNTSTEPHEN, GEORGE STEPHEN is the heading, and `BARON` begins the body, as
printed.  A book whose bold marks less than its title (the DNB bolds only the
surname) names a hook, and the engine cuts where the hook says.
"""
import dataclasses

import pytest

import wikikit.pipeline.stages.elements._title as T
from fixture_book import BARE

OPENING = "«B»MOUNTSTEPHEN, GEORGE STEPHEN,«/B» BARON (1829–), Canadian financier"


@pytest.fixture
def book(monkeypatch):
    def use(corpus):
        monkeypatch.setattr(T, "current_corpus", lambda: corpus)
    return use


def test_with_no_hook_the_heading_is_the_bold_run(book):
    book(BARE)
    body, span = T.produce_title(OPENING)
    assert span == "«B»MOUNTSTEPHEN, GEORGE STEPHEN,«/B»"
    assert body.startswith("BARON (1829")


def test_the_book_says_where_its_heading_ends(book):
    seen = []

    def heading_end(opening, bold_end):
        seen.append(opening[:bold_end])
        return opening.index(" (")                 # this book's heading runs to its date

    book(dataclasses.replace(BARE, heading_end=heading_end))
    body, span = T.produce_title(OPENING)
    assert seen == ["«B»MOUNTSTEPHEN, GEORGE STEPHEN,«/B»"]   # handed the bold run's end
    assert span == "«B»MOUNTSTEPHEN, GEORGE STEPHEN,«/B» BARON"
    assert body.startswith("(1829")
