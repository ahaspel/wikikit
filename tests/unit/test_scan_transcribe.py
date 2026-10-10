"""wikikit.scan.transcribe: the prompt's owners, and assembling a page from its
readings — strips joined across cuts, figures floated to the next paragraph
boundary with their captions.  No model is called."""
import pytest

from wikikit.scan.cut import Item
from wikikit.scan.transcribe import PARA, assemble_page, captions, instructions, join_across_cut, prompt_for
from wikikit.scan.vocabulary import VOCABULARY

H = Item("head", (0, 0, 900, 40), 0, "head")


def t(name, x0=0, y0=0, x1=300, y1=100, col=1):
    return Item("text", (x0, y0, x1, y1), col, name)


def f(name, x0=0, y0=0, x1=300, y1=100, col=1):
    return Item("figure", (x0, y0, x1, y1), col, name)


def body(page):
    return page.split("</noinclude>", 1)[1].rsplit("<noinclude>", 1)[0]


def test_the_strip_prompt_states_the_engine_vocabulary_and_the_edge_signals():
    p = instructions()
    for c in VOCABULARY:
        assert f"{c.open}text{c.close}" in p
    assert PARA in p and '"diffi-"' in p


def test_strips_join_across_cuts_and_break_where_the_model_says():
    items = [H, t("a"), t("b"), t("c")]
    texts = {"head": "{{RunningHeader|x|1|y}}",
             "a": f"{PARA}\n'''one''' runs on and is diffi-",
             "b": "cult to end here.",
             "c": f"{PARA}\n'''two''' begins anew."}
    page = assemble_page(items, texts, {}, 300)
    assert page.startswith('<noinclude><pagequality level="1" user="transcriber" />{{RunningHeader|x|1|y}}')
    assert body(page) == "'''one''' runs on and is difficult to end here.\n\n'''two''' begins anew."


def test_a_table_is_a_block_of_its_own_between_strips():
    # Anthon leaf 1459: the table, read in its own strip, ran on from the
    # sentence before it; and the text after it is a paragraph of its own
    items = [Item("text", (0, 0, 300, 50), 1, "s1"), Item("text", (0, 50, 300, 100), 1, "s2"),
             Item("text", (0, 100, 300, 150), 1, "s3")]
    table = '{| style="margin:auto"\n|As||12\n|}'
    texts = {"s1": f"{PARA}\nThe following table exhibits the parts.", "s2": table,
             "s3": "The Romans made their weights of marble."}
    out = body(assemble_page(items, texts, {}, 300))
    assert out == "\n\n".join(["The following table exhibits the parts.", table,
                               "The Romans made their weights of marble."])


def test_a_figure_waits_for_the_paragraph_to_end_and_takes_its_caption():
    # an inset interrupts a hyphenated word: "mold-" [figure + caption] "ings"
    items = [t("s1"), f("f1", 0, 100, 150, 300), Item("caption", (0, 300, 150, 330), 1, "cap"),
             t("side", 160, 100, 300, 400),
             t("s2", 0, 400, 300, 500)]
    texts = {"s1": f"{PARA}\nornamented with mold-",
             "cap": "{{c|Capital of the Parthenon.}}",
             "side": "ings on the sides.\n\nA new paragraph",
             "s2": "continues here."}
    assert captions(items) == {"cap"}
    out = body(assemble_page(items, texts, {"f1": "Century v01 n0029 c1f1.png"}, 300))
    assert out == ("ornamented with moldings on the sides.\n\n"
                   "[[File:Century v01 n0029 c1f1.png|center|180px]]\n{{c|Capital of the Parthenon.}}\n\n"
                   "A new paragraph continues here.")


def test_the_strip_after_an_uncaptioned_figure_is_text_not_its_caption():
    # Anthon leaf 1450: the entry below the family tree, taken for its caption,
    # let the column's next strip run on from the text above the tree
    items = [t("s1"), f("f1", 0, 100, 300, 300), t("s2", 0, 300, 300, 400), t("s3", 0, 400, 300, 500)]
    texts = {"s1": f"{PARA}\nthe following genealogical table.",
             "s2": f"{PARA}\n{{{{sc|Alexămenus}}}}, of Teos, natural ad-",
             "s3": "vantages of manner."}
    assert captions(items) == set()
    out = body(assemble_page(items, texts, {"f1": "x.png"}, 300))
    assert out == ("the following genealogical table.\n\n[[File:x.png|center|360px]]\n\n"
                   "{{sc|Alexămenus}}, of Teos, natural advantages of manner.")
    # a page cut before captions were marked keeps its old assembly
    assert captions(items, legacy=True) == {"s2"}


def test_a_figure_at_the_end_of_the_page_is_not_lost():
    items = [t("s1"), f("f1", 0, 100, 300, 300)]
    out = body(assemble_page(items, {"s1": f"{PARA}\nlast words"}, {"f1": "x.png"}, 300))
    assert out == "last words\n\n[[File:x.png|center|360px]]"


@pytest.mark.parametrize("before, after, joined", [
    # a construct running across the cut is one span
    ("in {{smaller|small type that}}", "{{smaller|goes on}} here",
     "in {{smaller|small type that goes on}} here"),
    # a broken word joins, and so does the span it is in
    ("ornamented with {{smaller|mold-}}", "{{smaller|ings on the sides}}",
     "ornamented with {{smaller|moldings on the sides}}"),
    # the next strip did not reopen the span: the word stays whole
    ("ornamented with {{smaller|mold-}}", "ings on the sides",
     "ornamented with {{smaller|moldings}} on the sides"),
    # nested: italic inside small type
    ("{{smaller|''Shak.'', As you ''Li-''}}", "{{smaller|''ke it''}}",
     "{{smaller|''Shak.'', As you ''Like it''}}"),
    # a hyphen that is not at a word's end is left alone
    ("a compound —", "next", "a compound — next"),
    # the hyphen set after the construct's close (SILVER: "{{sc|Bi}}-")
    ("the articles {{sc|Money}} and {{sc|Bi}}-", "{{sc|metallism}}; the reader",
     "the articles {{sc|Money}} and {{sc|Bimetallism}}; the reader"),
])
def test_readings_join_across_a_cut(before, after, joined):
    assert join_across_cut(before, after) == joined


def test_the_book_declares_its_notation_or_the_prompt_omits_it():
    p = instructions()
    assert "own notation" not in p          # the fixture book declares none


def test_a_title_in_the_head_position_is_page_text_not_a_header():
    """A page that opens a section ("LIST OF WRITERS") has no running head:
    its first line is printed text, and stays in the page."""
    items = [H, t("a")]
    page = assemble_page(items, {"head": "LIST OF WRITERS", "a": f"{PARA}\nG. A. A. . . G. A. Aitken."}, {}, 300)
    assert page.startswith('<noinclude><pagequality level="1" user="transcriber" /></noinclude>')
    assert body(page) == "LIST OF WRITERS\n\nG. A. A. . . G. A. Aitken."


def test_type_across_the_columns_never_joins_a_column():
    """A library's number below the columns is not the end of the last row,
    and a title above them does not run on into the first."""
    items = [Item("text", (0, 0, 100, 10), 0, "c0s1"), Item("text", (0, 10, 50, 90), 1, "c1s1"),
             Item("text", (0, 90, 100, 100), 0, "c0s2")]
    texts = {"c0s1": "LIST OF WRITERS", "c1s1": "E. H. M. . . . E. H. Marshall.", "c0s2": "79293"}
    page = assemble_page(items, texts, {}, 50)
    assert "LIST OF WRITERS\n\nE. H. M." in page and "Marshall.\n\n79293" in page
