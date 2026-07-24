"""Unterminated attribute quotes are tolerated by the attr READERS (J2).

363 corpus tags have an odd number of `"` — the transcriber forgot the closing
quote (`<td style="padding-left:2em;>`, `<span title="They cut off his head>`).
The global repair pass (`close_unclosed_attr_quotes`) is deleted; the rule now
lives where the attrs are read: an attribute value can never cross its own
tag's `>`, so an unterminated quote ends at the tag close.  Two readers own it:
`_table_fold._KV_RE` (every cell/row/table/styled-wrapper attr slot) and
`_walker._SPAN_TITLE_OPEN_RE` (title-span recognition + peel).
"""
from britannica.pipeline.stages.elements import process_elements
from britannica.pipeline.stages.elements._context import ElementContext
from britannica.pipeline.stages.elements._table_fold import fold_cell_attrs


def ctx():
    return ElementContext(volume=1)


def test_unterminated_style_value_runs_to_slot_end():
    """The ABBEY Fig. 10 shape: `<td style="…;>` — the value is everything the
    tag carried, exactly what the deleted repair's inserted quote gave."""
    css, _ = fold_cell_attrs('style="padding-left:1.0em; padding-right:1.0em;')
    assert "padding-left:1.0em" in css and "padding-right:1.0em" in css
    # multi-declaration value stays WHOLE (the pre-tolerance fallback tore it:
    # `"position:` matched as a bare token and ` relative` was dropped)
    css, _ = fold_cell_attrs('style="position: relative; top: .8em;')
    assert "position: relative" in css and "top: .8em" in css


def test_wellformed_attrs_unchanged():
    css, attrs = fold_cell_attrs('style="width:50%" class="figcenter" colspan=2')
    assert "width:50%" in css
    assert attrs.get("class") == "figcenter" and attrs.get("colspan") == "2"


def test_unterminated_title_span_carries_and_never_crashes():
    """CARMAGNOLE (vol 5 p368): the title gloss carries; before the tolerance
    the quoted value ran past `>` into the NEXT span's quote, and the
    walker/classifier disagreement crashed the walk."""
    out = process_elements(
        '<span title="They cut off his head>On lui coupa la tête</span>', ctx())
    assert "On lui coupa la tête" in out
    assert "They cut off his head" in out          # tooltip carried, not dropped
    two = process_elements(
        '<span title="Long live the sound,>Vive le son,</span>\n'
        '<span title="Let\'s dance the Carmagnole">Dansons la Carmagnole,</span>',
        ctx())
    assert "Vive le son" in two and "Dansons la Carmagnole" in two
    assert "Let's dance the Carmagnole" in two     # second span intact
