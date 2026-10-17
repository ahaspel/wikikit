"""The desk's render table is measured from the engine, construct by construct;
each construct must survive the engine's chain with its content intact — or,
for one the reader typesets, be lifted out whole to the engine's stand-in."""
from wikikit.scan.vocabulary import TYPESET, VOCABULARY, prompt_text, render_table


def test_every_construct_renders_through_the_engine_and_nothing_leaks():
    table = render_table()
    page_forms = {"resumes", "figure", "inset", "nop", "br", "gap", "quotes", "around a block"}
    assert set(table) == {c.name for c in VOCABULARY} | {"paragraph", "table"} | page_forms
    for name, value in table.items():
        if name in TYPESET or name in page_forms:
            continue
        html = "".join(value[:2]) if isinstance(value, list) else value
        assert "{{" not in html and "'''" not in html and "«" not in html, (name, html)
    assert table["bold"] == ["<b>", "</b>", " "]
    assert table["smaller block"][2] == "<p>"          # a block ends its paragraph
    assert table["paragraph"] == "<p>"


def test_what_assembly_writes_into_a_page_is_measured_too():
    table = render_table()
    assert table["nop"] == "" and table["br"] == "<br>"
    for html in (table["figure"], table["inset"]["left"], table["inset"]["right"]):
        assert html.startswith(("<img", "<span")) and "\u0010src\u0010" in html and "\u0010width\u0010" in html
    assert "\u0010cap\u0010" in table["inset"]["left"]
    assert table["gap"] == {"unit": " ", "gap": 2, "em": 1}
    assert "hence" in table["resumes"]["joiner"] and table["resumes"]["open"] == ",;:"


def test_a_poem_is_measured_as_lines_the_engine_keeps():
    assert render_table()["poem"] == {"open": '<blockquote class="verse">', "line": "<br>",
                                      "stanza": "<p>", "close": "</blockquote>"}
    assert "<poem>" in prompt_text()


def test_math_is_lifted_out_for_the_reader_to_typeset():
    assert render_table()["math"] == ["«MATHPH»"]
    assert "no HTML but <sup>, <sub>, <math>," in prompt_text()


def test_a_span_round_a_block_is_measured_as_the_block_it_becomes():
    # {{smaller|{{c|…}}}}: a browser lets no block stand in a span, so the
    # engine sets it as a div (`_tables.styled_marker`) and the desk does too
    assert render_table()["around a block"]["smaller"] == ['<div style="font-size:83%">', "</div>"]
