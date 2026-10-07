"""A page's layout is the book's to declare; where it declares nothing, the
cutter measures the ink."""
import dataclasses

from wikikit import corpora
from wikikit.scan import layout
from wikikit.scan.layout import PageLayout, layout_for


def test_a_book_that_declares_nothing_leaves_the_ink_to_decide(monkeypatch):
    book = corpora.current_corpus()
    monkeypatch.setattr(layout, "current_corpus", lambda: dataclasses.replace(book, page_layout=None))
    assert layout_for(1, 5) == PageLayout(columns=None)


def test_a_book_declares_its_pages(monkeypatch):
    book = corpora.current_corpus()
    two_after_the_preface = lambda volume, page: PageLayout(columns=1 if page < 30 else 2)
    monkeypatch.setattr(layout, "current_corpus",
                        lambda: dataclasses.replace(book, page_layout=two_after_the_preface))
    assert layout_for(1, 12).columns == 1 and layout_for(1, 401).columns == 2
