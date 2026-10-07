"""The desk's render table is measured from the engine, construct by construct;
each construct must survive the engine's chain with its content intact — or,
for one the reader typesets, be lifted out whole to the engine's stand-in."""
from wikikit.scan.vocabulary import TYPESET, VOCABULARY, prompt_text, render_table


def test_every_construct_renders_through_the_engine_and_nothing_leaks():
    table = render_table()
    assert set(table) == {c.name for c in VOCABULARY} | {"paragraph", "table"}
    for name, value in table.items():
        if name in TYPESET:
            continue
        html = "".join(value[:2]) if isinstance(value, list) else value
        assert "{{" not in html and "'''" not in html and "«" not in html, (name, html)
    assert table["bold"] == ["<b>", "</b>", " "]
    assert table["smaller block"][2] == "<p>"          # a block ends its paragraph
    assert table["paragraph"] == "<p>"


def test_math_is_lifted_out_for_the_reader_to_typeset():
    assert render_table()["math"] == ["«MATHPH»"]
    assert "no HTML but <sup>, <sub>, <math>," in prompt_text()
