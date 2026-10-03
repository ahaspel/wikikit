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


def test_a_figure_waits_for_the_paragraph_to_end_and_takes_its_caption():
    # an inset interrupts a hyphenated word: "mold-" [figure + caption] "ings"
    items = [t("s1"), f("f1", 0, 100, 150, 300), t("cap", 0, 300, 150, 330), t("side", 160, 100, 300, 400),
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
])
def test_readings_join_across_a_cut(before, after, joined):
    assert join_across_cut(before, after) == joined


def test_the_book_declares_its_notation_or_the_prompt_omits_it():
    p = instructions()
    assert "own notation" not in p          # the fixture book declares none
