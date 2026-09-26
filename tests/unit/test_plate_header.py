"""A plate's header carries no word count; an article's still does."""
from britannica.render.article import render_article


def _record(article_type, word_count):
    return {"title": "AEGEAN CIVILIZATION", "stable_id": "01-0278-bc0a88",
            "article_type": article_type, "volume": 1, "page_start": 248, "page_end": 248,
            "word_count": word_count, "body": "«TITLE:AEGEAN CIVILIZATION«/TITLE»Fig. 1.—A vase.",
            "contributors": [], "plates": [], "xrefs": []}


def test_a_plate_shows_no_word_count_even_when_its_legend_has_words():
    for target in ("site", "epub"):
        html = render_article(_record("plate", 30), target=target)
        assert "30 words" not in html and " words" not in html.split("</h1>", 1)[1][:400]


def test_an_article_keeps_its_word_count():
    for target in ("site", "epub"):
        assert "1,460 words" in render_article(_record("article", 1460), target=target)
