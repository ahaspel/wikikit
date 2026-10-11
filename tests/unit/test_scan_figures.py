"""The first look's answer: figures and the type set across the columns, in
page pixels — and an answer without `across` (as older prompts gave) reads as
none."""
import pytest

from wikikit.scan.figures import PAGE_PROMPT, locate_answer


def test_the_answer_scales_figures_and_type_across_to_the_page():
    figs, across = locate_answer(
        '{"figures": [{"box": [10, 20, 30, 40], "caption": null, "column": 1, "inset": false}],'
        ' "across": [{"box": [5, 6, 7, 8]}]}', 2.0)
    assert figs == [{"box": [20, 40, 60, 80], "caption": None, "column": 1, "inset": False}]
    assert across == [[10, 12, 14, 16]]


def test_an_answer_without_type_across_has_none():
    assert locate_answer('{"figures": []}', 2.0) == ([], [])


def test_the_prompt_asks_for_both_and_keeps_its_braces():
    text = PAGE_PROMPT.format(w=1000, h=1500, g=100)
    assert '"across"' in text and '"figures"' in text and "{w}" not in text


def test_a_close_look_that_settles_on_another_drawing_keeps_the_first_box():
    # the window held two drawings and the close look boxed the other one
    # (EB1911 vol 22 p706): the first look's box stands
    from wikikit.scan.figures import refine_answer
    fig = {"box": [100, 1281, 600, 1352], "caption": None, "column": 2, "inset": False}
    window = {"x0": 0, "y0": 1000, "shrink": 1.0}
    out = refine_answer('{"box": [100, 617, 600, 702], "caption": null}', fig, window)
    assert out["box"] == [100, 1281, 600, 1352]
    near = refine_answer('{"box": [96, 278, 604, 356], "caption": null}', fig, window)
    assert near["box"] == [96, 1278, 604, 1356]


def test_an_answer_corrected_in_words_is_read_as_its_correction():
    # EB1911 vol 26 ws p863, verbatim: an answer, a second thought, an answer
    text = ('{"figures":[{"box":[36,565,182,912],"caption":[50,915,170,948],"column":1,"inset":true}],'
            '"across":[{"box":[36,1210,1000,1300],"caption":null}]}\n\nWait, the "across" entries should '
            'only have "box". Corrected:\n\n{"figures":[{"box":[36,565,182,912],"caption":[50,915,170,948],'
            '"column":1,"inset":true}],"across":[{"box":[36,1210,1000,1300]}]}')
    figs, across = locate_answer(text, 1.0)
    assert len(figs) == 1 and len(across) == 1


def test_an_answer_with_no_object_is_refused():
    with pytest.raises(ValueError):
        locate_answer("I cannot see any figures.", 1.0)


def test_the_gutter_look_names_what_crosses_or_nothing():
    from wikikit.scan.figures import gutter_answer
    assert gutter_answer('{"crosses": false, "what": []}') == []
    what = [{"kind": "table", "where": "bottom", "detail": "E.H.P. table over the rule"}]
    assert gutter_answer('{"crosses": true, "what": [{"kind": "table", "where": "bottom", '
                         '"detail": "E.H.P. table over the rule"}]}') == what
    # crossing said, nothing named: still a crossing, never taken for plain columns
    assert gutter_answer('{"crosses": true, "what": []}')[0]["kind"] == "other"
