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


# ── figures read in place: a mark in the reading, the crop after its item ───

def test_a_figure_mark_becomes_its_crop_with_the_caption_the_model_read():
    items = [t("c1s1", 0, 0, 300, 900), f("c1f1", 0, 300, 300, 500)]
    texts = {"c1s1": f"{PARA}\nFirst paragraph.\n\n[[FIGURE]]\n{{{{c|{{{{sc|Fig.}}}} 14.}}}}\n\nSecond one."}
    out = body(assemble_page(items, texts, {"c1f1": "x.png"}, 300))
    assert out == ("First paragraph.\n\n[[File:x.png|center|360px]]\n{{c|{{sc|Fig.}} 14.}}\n\n"
                   "Second one.")


def test_a_figure_interrupting_a_sentence_goes_to_its_end():
    # the reading records the mark where it stands, mid-sentence; assembly
    # sets it after the sentence it interrupts, and the paragraph's text
    # after that begins a new one
    items = [t("c1s1", 0, 0, 300, 900), f("c1f1", 0, 300, 150, 500)]
    texts = {"c1s1": f"{PARA}\nornamented with\n[[FIGURE]]\nmoldings on the sides. Then more.\n\nNext."}
    out = body(assemble_page(items, texts, {"c1f1": "x.png"}, 300))
    assert out == ("ornamented with moldings on the sides.\n\n[[File:x.png|center|180px]]\n\n"
                   "Then more.\n\nNext.")


def test_a_table_where_the_sentence_has_ended_stays_there_and_is_set():
    # "printed with:—" then the table, then the text runs on: the table stays,
    # is centred, and a printed brace at a cell's start is the character
    items = [t("c1s1", 0, 0, 300, 900)]
    texts = {"c1s1": f"{PARA}\nThe cloth is printed with:—\n{{|\n|rowspan=2|{{||36||parts.\n|-\n|100||ice.\n|}}\n"
                     "Mix and add quickly."}
    out = body(assemble_page(items, texts, {}, 300))
    assert out == ('The cloth is printed with:—\n\n{| style="margin:auto"\n|rowspan=2|&#123;||36||parts.\n'
                   "|-\n|100||ice.\n|}\n\nMix and add quickly.")


def test_a_margin_heading_breaks_no_paragraph_and_no_word():
    # vol 26 p169: "deter-" | margin heading | "mining": one paragraph, the
    # word whole, the heading set before it
    items = [t("c1s1", 0, 0, 300, 900)]
    texts = {"c1s1": f"{PARA}\nthe route in advance and of deter-\n{{{{X heading|Triangulation.}}}}\n"
                     "mining the positions."}
    out = body(assemble_page(items, texts, {}, 300))
    assert out == "the route in advance and of\n{{X heading|Triangulation.}}\ndetermining the positions."


def test_a_sheet_signature_is_furniture_set_in_the_footer(monkeypatch):
    from wikikit.scan import transcribe as T
    from wikikit.scan.vocabulary import Construct
    sig = Construct("signature", "{{Sig|", "}}", "a sheet signature", furniture=True)
    monkeypatch.setattr(T, "constructs", lambda: T.VOCABULARY + (sig,))
    items = [t("c1s1", 0, 0, 300, 900)]
    page = assemble_page(items, {"c1s1": f"{PARA}\nThe last line of the column.\n{{{{Sig|XXIV. 31 a}}}}"}, {}, 300)
    assert body(page) == "The last line of the column."
    assert page.endswith("<noinclude>{{Sig|XXIV. 31 a}}<references/></noinclude>")


def test_a_figure_at_a_columns_head_lets_the_paragraph_run_on():
    # column 1 ends mid-sentence; column 2 opens with a figure, then the rest
    items = [t("c1s1", 0, 0, 300, 900), t("c2s1", 300, 0, 600, 900), f("c2f1", 300, 0, 600, 200, col=2)]
    texts = {"c1s1": f"{PARA}\nThe sentence runs",
             "c2s1": "[[FIGURE]]\n{{c|Fig. 2.}}\n\non into column two.\n\nAnother."}
    out = body(assemble_page(items, texts, {"c2f1": "y.png"}, 300))
    assert out == ("The sentence runs on into column two.\n\n"
                   "[[File:y.png|center|360px]]\n{{c|Fig. 2.}}\n\nAnother.")


def test_two_figures_in_one_column_take_their_crops_in_order():
    items = [t("c1s1", 0, 0, 300, 900), f("c1f1", 0, 100, 300, 200), f("c1f2", 0, 500, 300, 600)]
    texts = {"c1s1": f"{PARA}\nA.\n\n[[FIGURE]]\n\nB.\n\n[[FIGURE]]\n{{{{c|Fig. 2.}}}}\n\nC."}
    out = body(assemble_page(items, texts, {"c1f1": "1.png", "c1f2": "2.png"}, 300))
    assert out == ("A.\n\n[[File:1.png|center|360px]]\n\nB.\n\n"
                   "[[File:2.png|center|360px]]\n{{c|Fig. 2.}}\n\nC.")


def test_measured_type_replaces_the_readings_own_marks():
    from wikikit.scan.transcribe import apply_type
    reading = ("⟦200⟧ {{smaller block|A body paragraph the reader marked small.}}\n\n"
               "⟦700⟧ A small paragraph the reader left plain.\n\n"
               "{| \n|cell\n|}\n\n"
               "⟦900⟧ {{smaller block|<sup>1</sup> A footnote.}}")
    out = apply_type(reading, ["body", "small", "small"])
    assert "⟦200⟧ A body paragraph the reader marked small." in out
    assert "⟦700⟧ {{smaller block|A small paragraph the reader left plain.}}" in out
    assert "{| \n|cell\n|}" in out and "{{smaller block|{|" not in out      # a table is never wrapped
    assert "{{smaller block|<sup>1</sup> A footnote.}}" in out               # a footnote as it was


def test_no_sizes_leave_the_reading_alone():
    from wikikit.scan.transcribe import apply_type
    assert apply_type("⟦200⟧ {{smaller block|x}}", None) == "⟦200⟧ {{smaller block|x}}"
