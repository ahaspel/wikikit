"""The desk's render table is measured from the engine, construct by construct;
each construct must survive the engine's chain with its content intact."""
from wikikit.scan.vocabulary import VOCABULARY, render_table


def test_every_construct_renders_through_the_engine_and_nothing_leaks():
    table = render_table()
    assert set(table) == {c.name for c in VOCABULARY} | {"paragraph"}
    for name, value in table.items():
        html = "".join(value) if isinstance(value, list) else value
        assert "{{" not in html and "'''" not in html and "«" not in html, (name, html)
    assert table["bold"] == ["<b>", "</b>"]
    assert table["paragraph"] == "<p>"
