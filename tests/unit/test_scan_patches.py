"""Patches lay words and formulas on a fetched page, and nothing else."""
import pytest

from wikikit.scan import patches


def test_a_patch_replaces_its_span():
    raw = "the iliiculty of the case"
    out = patches.apply_patches(raw, [{"start": 4, "end": 13, "old": "iliiculty", "new": "difficulty"}])
    assert out == "the difficulty of the case"


def test_a_patch_not_at_its_place_fails_never_patches_elsewhere():
    with pytest.raises(patches.PatchError):
        patches.apply_patches("the page changed", [{"start": 4, "end": 13, "old": "iliiculty", "new": "x"}])


def test_overlapping_patches_fail():
    raw = "abcdef"
    with pytest.raises(patches.PatchError):
        patches.apply_patches(raw, [{"start": 0, "end": 3, "old": "abc", "new": "x"},
                            {"start": 2, "end": 4, "old": "cd", "new": "y"}])


def test_structure_ignores_formula_templates_and_wrapper_contents():
    raw = "{{center|x {{Greek|φ}} = 1}} see [[Waves]]<ref>a note</ref>"
    s = patches.structure(raw)
    assert s == ["{{center|", "}}", "[[Waves]]", "<ref>a note</ref>"]


def test_a_formula_patch_inside_a_wrapper_keeps_the_structure():
    raw = "{{center|x {{Greek|φ}} = I}}"
    p = [{"start": 9, "end": 26, "old": "x {{Greek|φ}} = I", "new": r"<math>x\phi=1</math>"}]
    assert raw[9:26] == "x {{Greek|φ}} = I"
    assert patches.check(raw, p) == []


def test_tex_braces_close_no_template():
    raw = r"{{block center|<math>\frac{a}{b}}</math>}} see [[Waves]]"
    assert patches.structure(raw) == ["{{block center|", "}}", "[[Waves]]"]


def test_a_brace_of_the_text_before_a_template_is_text():
    # "√{{{Greek|γ}}…": OCR's brace, then the template (vol 25 p. 443)
    assert patches.structure("√{{{Greek|γ}}<sub>1</sub>) see [[Waves]]") == ["[[Waves]]"]


def test_a_formula_patch_sets_its_own_display():
    # Wikisource's centred salad, and its formula laid out in an unruled
    # table, give way to our displayed formula
    raw = "thus:\n{{center|x = l/2}}\nand<table align=center><tr><td>y</td></tr></table> so"
    old1, old2 = "{{center|x = l/2}}", "<table align=center><tr><td>y</td></tr></table>"
    a, b = raw.index(old1), raw.index(old2)
    p = [{"start": a, "end": a + len(old1), "old": old1, "new": r"{{block center|<math>x=\tfrac{1}{2}</math>}}"},
         {"start": b, "end": b + len(old2), "old": old2, "new": "<math>y</math>"}]
    assert patches.check(raw, p) == []


def test_a_formula_patch_never_takes_a_ruled_table_or_a_note():
    raw = '<table border="1"><tr><td>1</td></tr></table> and x<ref>note</ref> end'
    for old in ('<table border="1"><tr><td>1</td></tr></table>', "x<ref>note</ref>"):
        a = raw.index(old)
        assert patches.check(raw, [{"start": a, "end": a + len(old), "old": old, "new": "<math>x</math>"}])


def test_a_patch_that_touches_a_link_is_refused():
    raw = "see [[Waves]] here"
    p = [{"start": 4, "end": 13, "old": "[[Waves]]", "new": "Waves"}]
    assert patches.check(raw, p)
