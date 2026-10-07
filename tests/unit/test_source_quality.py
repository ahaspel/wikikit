"""What a reader is told of a page's source (user, 2026-10-07): Wikisource's
unproofread OCR is warned of; the book's own transcription from the scan is
not OCR — complete, it gets a quiet note and no margin marker; incomplete (a
strip refused or unread), it is level 2, "problematic", and keeps its marker."""
from types import SimpleNamespace

from wikikit.export import article_json
from wikikit.render.article import render_article
from wikikit.scan import supplied


def payload(refused=(), unread=None, checked=None):
    items = [{"name": "head", "kind": "head", "column": 0, "box": [0, 0, 10, 10]},
             {"name": "c1s1", "kind": "text", "column": 1, "box": [0, 10, 10, 20]},
             {"name": "c1s2", "kind": "text", "column": 1, "box": [0, 20, 10, 30]}]
    return {"provenance": {"items": items, "refused": list(refused), "unread": unread or {},
                           "checked": checked or {}}}


def test_a_page_with_text_missing_is_problematic_until_checked():
    assert supplied.level(payload()) == 1
    assert supplied.level(payload(refused=["c1s2"])) == 2
    assert supplied.level(payload(unread={"c1s1": "empty"})) == 2
    assert supplied.level(payload(unread={"head": "a malformed running head"})) == 1  # no text lost
    assert supplied.level(payload(refused=["c1s2"], checked={"c1s2": "x"})) == 1         # a person read it
    assert supplied.level(payload(checked={"head": 1, "c1s1": 1, "c1s2": 1})) == 3


class FakeQuery:
    def __init__(self, pages):
        self.pages = pages

    def filter(self, *a):
        return self

    def all(self):
        return self.pages


def quality(pages):
    session = SimpleNamespace(query=lambda model: FakeQuery(pages))
    article = SimpleNamespace(volume=27, page_start=323, page_end=325)
    return article_json._source_quality(session, article)


def page(n, level, source):
    return SimpleNamespace(page_number=n, source_name=source, wikitext=None,
                           raw_text=f'<pagequality level="{level}" user="x" />text')


def test_the_books_own_transcription_is_no_ocr():
    q = quality([page(323, 3, "wikisource"), page(324, 1, "scan"), page(325, 2, "scan")])
    assert q["transcribed_pages"] == [324]
    assert q["unproofed_pages"] == {"325": 2}          # incomplete: marked
    assert q["lowest_level"] == 2                      # no OCR warning
    q = quality([page(323, 1, "wikisource"), page(324, 1, "scan")])
    assert q["unproofed_pages"] == {"323": 1} and q["lowest_level"] == 1


def article(sq):
    return {"title": "TROPINE", "volume": 27, "page_start": 323, "page_end": 325, "body": "Tropine.",
            "filename": "x.json", "source_quality": sq, "contributors": []}


def test_the_reader_sees_a_quiet_note_not_a_warning():
    html = render_article(article({"lowest_level": 3, "unproofed_pages": {}, "transcribed_pages": [324]}))
    assert "transcribed from the scan for this edition" in html and "Source quality notice" not in html
    html = render_article(article({"lowest_level": 1, "unproofed_pages": {"323": 1}, "transcribed_pages": [324]}))
    assert "Source quality notice" in html
