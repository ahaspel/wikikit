"""wikikit.scan.transcribe: the prompt's owners, and assembling a page from its
readings — strips joined across cuts, figures floated to the next paragraph
boundary with their captions.  No model is called."""
import pytest

from wikikit.pipeline.stages.preprocess import PARAGRAPH_ENDS_WITH_PAGE
from wikikit.scan.cut import Item
from wikikit.scan.transcribe import PARA, assemble_page, captions, instructions, join_across_cut, prompt_for
from wikikit.scan.vocabulary import VOCABULARY

H = Item("head", (0, 0, 900, 40), 0, "head")


def t(name, x0=0, y0=0, x1=300, y1=100, col=1):
    return Item("text", (x0, y0, x1, y1), col, name)


def f(name, x0=0, y0=0, x1=300, y1=100, col=1):
    return Item("figure", (x0, y0, x1, y1), col, name)


def body(page):
    """The page's text, without the mark that it opens a paragraph (its own
    test: `test_a_page_says_whether_it_opens_a_paragraph`)."""
    text = page.split("</noinclude>", 1)[1].rsplit("<noinclude>", 1)[0]
    return text.removeprefix(PARAGRAPH_ENDS_WITH_PAGE)


def test_a_page_says_whether_it_opens_a_paragraph():
    # the build's page join reads it: a paragraph break, or the paragraph
    # running on from the page before (SOLUTION p. 374/375, 2026-10-15)
    items = [Item("text", (0, 0, 300, 100), 1, "c1s1")]
    opens = assemble_page(items, {"c1s1": f"{PARA}\nA new paragraph."}, {}, 300)
    runs_on = assemble_page(items, {"c1s1": "carried on from the page before."}, {}, 300)
    assert opens.split("</noinclude>", 1)[1].startswith(PARAGRAPH_ENDS_WITH_PAGE + "A new paragraph.")
    assert runs_on.split("</noinclude>", 1)[1].startswith("carried on")


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
    # sentence before it ("{|" mid-line): it opens a line of its own, inside
    # the paragraph it stands in, and the strip after it, carrying on that
    # paragraph (no ¶), runs on after it on a line of its own
    items = [Item("text", (0, 0, 300, 50), 1, "s1"), Item("text", (0, 50, 300, 100), 1, "s2"),
             Item("text", (0, 100, 300, 150), 1, "s3")]
    table = '{| style="margin:auto"\n|As||12\n|}'
    texts = {"s1": f"{PARA}\nThe following table exhibits the parts.", "s2": table,
             "s3": "The Romans made their weights of marble."}
    out = body(assemble_page(items, texts, {}, 300))
    assert out == ("The following table exhibits the parts.\n" + table
                   + "\nThe Romans made their weights of marble.")


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


def test_a_figure_interrupting_a_paragraph_is_set_after_it_and_the_text_runs_on():
    # the reading records the mark where it stands, mid-sentence: the text it
    # interrupted runs on as one, every line full, and the figure follows the
    # paragraph — set inside it, it would break the line before it short and
    # start the text after it on a new line (SIGNAL p. 72; user, 2026-10-15)
    items = [t("c1s1", 0, 0, 300, 900), f("c1f1", 0, 300, 150, 500)]
    texts = {"c1s1": f"{PARA}\nornamented with\n[[FIGURE]]\nmoldings on the sides. Then more.\n\nNext."}
    out = body(assemble_page(items, texts, {"c1f1": "x.png"}, 300))
    assert out == ("ornamented with\nmoldings on the sides. Then more.\n\n"
                   "[[File:x.png|center|180px]]\n\nNext.")


def test_a_figure_the_text_introduces_stays_where_it_is_pointed_to():
    # "shown below:—" (SIGNAL p. 72's semaphore): the figure stands there,
    # and the paragraph runs on after it
    items = [t("c1s1", 0, 0, 300, 900), f("c1f1", 0, 300, 150, 500)]
    texts = {"c1s1": f"{PARA}\nThe code is shown below:—\n[[FIGURE]]\nIn using it the signaller faces him.\n\nNext."}
    out = body(assemble_page(items, texts, {"c1f1": "x.png"}, 300))
    assert out == ("The code is shown below:—\n[[File:x.png|center|180px]]\n"
                   "In using it the signaller faces him.\n\nNext.")


def test_a_table_where_the_sentence_has_ended_stays_there_and_is_set():
    # "printed with:—" then the table, then the text runs on: the table stays,
    # is centred, and a printed brace at a cell's start is the character
    items = [t("c1s1", 0, 0, 300, 900)]
    texts = {"c1s1": f"{PARA}\nThe cloth is printed with:—\n{{|\n|rowspan=2|{{||36||parts.\n|-\n|100||ice.\n|}}\n"
                     "Mix and add quickly."}
    out = body(assemble_page(items, texts, {}, 300))
    assert out == ('The cloth is printed with:—\n{| style="margin:auto"\n|rowspan=2|&#123;||36||parts.\n'
                   "|-\n|100||ice.\n|}\nMix and add quickly.")


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


def test_a_reading_wrapper_spanning_paragraphs_is_taken_off_once():
    # vol 24 p601: the reader wrapped two paragraphs in one small-type block;
    # taken off paragraph by paragraph it was missed and wrapped again
    from wikikit.scan.transcribe import apply_type
    reading = "⟦600⟧ {{smaller block|See Roscher.\n\n⟦800⟧ Selenga is a river.}}"
    out = apply_type(reading, ["small", "body"])
    assert out == "⟦600⟧ {{smaller block|See Roscher.}}\n\n⟦800⟧ Selenga is a river."
    assert "{{smaller block|{{smaller block|" not in out


def test_an_unmeasured_paragraph_keeps_the_readings_size():
    from wikikit.scan.transcribe import apply_type
    reading = ("⟦200⟧ {{smaller block|Short note, read small.\n\n"
               "⟦300⟧ Measured body.}}\n\n⟦500⟧ Short, read body.")
    out = apply_type(reading, [None, "body", None])
    assert out == ("⟦200⟧ {{smaller block|Short note, read small.}}\n\n"
                   "⟦300⟧ Measured body.\n\n⟦500⟧ Short, read body.")


def test_a_paragraph_signal_mid_column_never_reaches_the_page():
    # vol 24 p236: the reader wrote ¶ before a new article in mid-column
    from wikikit.scan.cut import Item
    from wikikit.scan.transcribe import assemble_page
    items = [Item("text", (0, 0, 100, 100), 1, "c1s1")]
    reading = "the end of one article.\n\n¶\n'''SAUMAREZ''', a man."
    out = assemble_page(items, {"c1s1": reading}, {}, 100)
    assert "¶" not in out
    assert "the end of one article.\n\n'''SAUMAREZ''', a man." in out


def test_a_formulas_braces_never_close_a_template():
    # vol 1 p671: "\frac{d}{d\text{X}}" ends in "}}"; the small-type wrapper
    # "closed" inside it and its real close leaked onto the page
    from wikikit.scan.transcribe import apply_type
    reading = ("⟦200⟧ {{smaller block|we find <math>\\frac{d}{d\\text{X}}</math> here.\n\n"
               "⟦500⟧ Next.}}")
    out = apply_type(reading, ["small", "body"])
    assert out == ("⟦200⟧ {{smaller block|we find <math>\\frac{d}{d\\text{X}}</math> here.}}\n\n"
                   "⟦500⟧ Next.")


def test_a_block_that_ends_its_sentence_stays():
    # vol 28 p508: "…the tables of" + verse, then a new sentence
    from wikikit.scan.cut import Item
    from wikikit.scan.transcribe import assemble_page
    items = [Item("text", (0, 0, 100, 100), 1, "c1s1")]
    reading = "learn the tables of\n<poem>ton, cwt.</poem>\nIn Code standards above the fifth, more."
    out = assemble_page(items, {"c1s1": reading}, {}, 100)
    assert out.index("the tables of") < out.index("<poem>ton") < out.index("In Code standards")


def test_a_small_paragraph_holding_a_block_wraps_its_text_not_the_block():
    from wikikit.scan.cut import Item
    from wikikit.scan.transcribe import apply_type, assemble_page
    items = [Item("text", (0, 0, 100, 100), 1, "c1s1")]
    reading = "⟦200⟧ learn the tables of\n<poem>ton, cwt.</poem>\nIn Code standards, more."
    out = assemble_page(items, {"c1s1": apply_type(reading, ["small"])}, {}, 100)
    assert "{{smaller block|learn the tables of}}" in out
    assert "{{smaller block|In Code standards, more.}}" in out
    assert "{{smaller block|<poem>" not in out and "{{smaller block|{{smaller block|" not in out


def test_small_type_continued_from_the_column_before_is_opened():
    # vol 25 p130: the column begins inside small type, closed with no opening
    from wikikit.scan.transcribe import apply_type
    reading = "a row of settlers.}}\n\n⟦500⟧ {{smaller block|A 20-stamp mill.}}"
    out = apply_type(reading, ["small"])
    assert out == "{{smaller block|a row of settlers.}}\n\n⟦500⟧ {{smaller block|A 20-stamp mill.}}"


def test_a_margin_heading_inside_an_italic_waits_for_it_to_close():
    # vol 25 p463: "(''Phil." | heading | "Trans.'', 1880"
    from wikikit.scan.transcribe import _join_text
    out = _join_text(["memoir (''Phil.", "{{EB1911 Shoulder Heading|Stroboscopic Method.}}",
                      "Trans.'', 1880), a cylinder."])
    assert out == ("memoir (''Phil. Trans.'', 1880), a cylinder.\n"
                   "{{EB1911 Shoulder Heading|Stroboscopic Method.}}")


def test_a_lead_in_body_type_is_left_exactly_as_read():
    # vol 26 p995: a margin heading, then text carrying on the column before
    from wikikit.scan.transcribe import apply_type
    reading = "{{EB1911 Shoulder Heading|Over-Tides.}}⟦200⟧arise from the potential."
    assert apply_type(reading, ["body"]) == reading


def test_a_start_recorded_inside_a_table_leaves_the_table_whole():
    # vol 28 p511: a tick in a table's first cell
    from wikikit.scan.transcribe import apply_type, POSITION
    reading = "{{smaller block|\n{|\n|⟦300⟧Pouce||France\n|-\n|||Russia\n|}}}"
    out = POSITION.sub("", apply_type(reading, ["small"]))
    assert "{|\n|Pouce||France\n|-\n|||Russia\n|}" in out


def test_a_small_paragraph_ending_in_a_figure_keeps_the_figure():
    # vol 20 p453: "obtained:—" then the figure mark, the paragraph small
    from wikikit.scan.cut import Item
    from wikikit.scan.transcribe import apply_type, assemble_page
    items = [Item("text", (0, 0, 100, 100), 1, "c1s1"), Item("figure", (0, 40, 100, 60), 1, "c1f1")]
    reading = "⟦10⟧ may be obtained:—\n[[FIGURE]]\n\n⟦70⟧ An ingenious application."
    out = assemble_page(items, {"c1s1": apply_type(reading, ["small", "small"])}, {"c1f1": "f.png"}, 100)
    assert "[[File:f.png" in out and "[[FIGURE]]" not in out


def test_the_measured_size_is_set_into_the_reading_and_a_correction_there_stands():
    # vol 1 p634: the reading gave "Taking the first polar" small, the print and
    # the measurement body — the reading now carries the measured size, and a
    # proofreader's correction to it stands (no measurement laid over it)
    from wikikit.scan.cut import Item
    from wikikit.scan.transcribe import apply_type, assemble_page
    items = [Item("text", (0, 0, 100, 100), 1, "c1s1")]
    measured = apply_type("⟦100⟧ {{smaller block|Taking the first polar.}}\n\n⟦300⟧ Moreover.", ["body", "small"])
    assert "{{smaller block|Taking" not in measured and "{{smaller block|Moreover.}}" in measured
    corrected = measured.replace("Taking the first polar.", "{{smaller block|Taking the first polar.}}")
    out = assemble_page(items, {"c1s1": corrected}, {}, 100)
    assert "{{smaller block|Taking the first polar.}}" in out and "{{smaller block|Moreover.}}" in out


def test_short_notes_run_on_in_one_line_are_each_their_own():
    from wikikit.scan.transcribe import place_footnotes, take_footnotes
    text = ("being given.<sup>2</sup> More text, and V.<sup>1</sup>\n\n"
            "{{smaller block|<sup>1</sup> See papers by Mr A. W. Johns. <sup>2</sup> See ''Trans.'', 1875.}}")
    body, notes = take_footnotes(text)
    assert notes == [("1", "See papers by Mr A. W. Johns."), ("2", "See ''Trans.'', 1875.")]
    body, left = place_footnotes(body, notes)
    assert left == [] and "given.<ref>See ''Trans.'', 1875.</ref>" in body


def test_a_power_in_a_note_does_not_split_it():
    from wikikit.scan.transcribe import take_footnotes
    _, notes = take_footnotes("{{smaller block|<sup>1</sup> As 10<sup>2</sup> shows. The rest.}}")
    assert notes == [("1", "As 10<sup>2</sup> shows. The rest.")]


def test_a_legend_under_a_figures_caption_goes_with_the_figure():
    from wikikit.scan.transcribe import _segments
    piece = ("and it appears\n[[FIGURE]]\n{{c|{{sc|Fig.}} 2.—Imperial Standard Pound, 1844.}}\n"
             "{{smaller block|Platinum pound avoirdupois, of cylindrical form.}}\n"
             "probable that further experiments will succeed. Then more.")
    segs = _segments(piece)
    assert segs[0][0] == "figure float" and "Platinum pound" in segs[0][1]
    assert segs[1] == ("text", "and it appears\nprobable that further experiments will succeed. Then more.")


def test_a_table_title_of_several_lines_goes_with_its_table_and_the_table_stays():
    # vol 26 p582 (TEMPERANCE): "…quoted above:—" introduces the table, whose
    # title is printed in two centred lines; the sentence after the table stays
    # after it (only the second title line went with the table, and the
    # sentence was moved up before it)
    from wikikit.scan.transcribe import _segments
    piece = ("gives the following table:—\n{{c|''Consumption of Alcohol.''}}\n"
             "{{c|''Annual Average per Head'', 1901–5.}}\n{|\n|France||9·69\n|}\n"
             "Apart from the gaps, allowance must be made. In no case is it so.")
    segs = _segments(piece)
    assert segs[0] == ("text", "gives the following table:—")
    assert segs[1][0] == "block" and segs[1][1].startswith("{{c|''Consumption of Alcohol.''}}\n{{c|")
    assert segs[2] == ("text", "Apart from the gaps, allowance must be made. In no case is it so.")


def test_small_type_carrying_on_the_sentence_is_not_a_legend():
    from wikikit.scan.transcribe import _units
    piece = "the flow\n[[FIGURE]]\n{{c|{{sc|Fig.}} 3.}}\n{{smaller block|also in air for low velocities.}}"
    assert [k for k, _ in _units(piece)] == ["text", "figure", "text"]


# ── a figure with type beside it: floated in its place ──────────────────────

def test_a_figure_with_type_beside_it_floats_in_its_place_and_the_text_runs_on():
    # SOUND p. 453's Fig. 30: type runs beside it down two paragraphs; a
    # float ends no line, so the paragraph it stands in runs on whole
    items = [t("c1s1", 0, 0, 300, 900), f("c1f1", 0, 300, 60, 500)]
    texts = {"c1s1": f"{PARA}\nonly slightly.\n[[FIGURE]]\n{{{{c|{{{{sc|Fig.}}}} 30.}}}}\n"
                     "If, however, the pipe.\n\nNext."}
    out = body(assemble_page(items, texts, {"c1f1": "x.png"}, 300, insets={"c1f1": "left"}))
    assert out == ("only slightly. {{img float|file=x.png|width=72px|align=left|cap={{sc|Fig.}} 30.}} "
                   "If, however, the pipe.\n\nNext.")


def test_an_inset_takes_the_replaced_pages_crop_told_its_side():
    items = [t("c1s1", 0, 0, 300, 900), f("c1f1", 0, 300, 60, 500)]
    crop = "{{Css image crop\n|Image = V.djvu\n|Page = 9\n|cWidth = 73\n|Location = center}}"
    texts = {"c1s1": f"{PARA}\nmold-\n[[FIGURE]]\n{{{{c|{{{{sc|Fig.}}}} 3.}}}}\nings here."}
    out = body(assemble_page(items, texts, {"c1f1": "x.png"}, 300,
                             source_images={"c1f1": [crop]}, insets={"c1f1": "right"}))
    # before the word the line broke, which joins whole
    assert out == ("{{Css image crop|Image = V.djvu|Page = 9|cWidth = 73"
                   "|Location = right|Description = {{sc|Fig.}} 3.}} moldings here.")


def test_an_inset_on_a_page_without_marks_stands_at_the_end_of_the_text_before_it():
    # vol 18 p272: figure and caption items, the type beside it the next reading
    items = [t("c2s1", 0, 0, 300, 100), f("c2f1", 150, 100, 300, 300),
             Item("caption", (150, 300, 300, 330), 1, "c2c1"), t("c2s2", 0, 100, 150, 330)]
    texts = {"c2s1": f"{PARA}\ninto numerous minute par-", "c2c1": "{{c|{{sc|Fig.}} 1.}}",
             "c2s2": "ticles of water."}
    out = body(assemble_page(items, texts, {"c2f1": "x.png"}, 300, insets={"c2f1": "right"}))
    assert out == ("into numerous minute {{img float|file=x.png|width=180px|align=right|cap={{sc|Fig.}} 1.}} "
                   "particles of water.")


def test_a_figure_not_inset_is_set_after_its_paragraph_as_before():
    items = [t("c1s1", 0, 0, 300, 900), f("c1f1", 0, 300, 150, 500)]
    texts = {"c1s1": f"{PARA}\nornamented with\n[[FIGURE]]\nmoldings.\n\nNext."}
    plain = assemble_page(items, texts, {"c1f1": "x.png"}, 300)
    assert assemble_page(items, texts, {"c1f1": "x.png"}, 300, insets={}) == plain
