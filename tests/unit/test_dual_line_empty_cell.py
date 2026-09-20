"""An EMPTY dual-line cell still stacks — the break is the structure.

`{{dual line||Na}}` is not a malformed two-cell row to be tidied into `Na`.  It
exists to put `Na` on its own line under whatever precedes it, which is how
EB1911 sets a structural formula: COUMARONES (vol 7 p.329) stacks

    C₆H₄ <  H
            O
            Na

as `H` + `{{dual line||Na}}`, and five of the corpus's eight empty-cell dual
lines are on that one page.  A `br_stack` that returned the other side when one
was empty collapsed those three lines to two, turning `O` and `Na` into `ONa`.

THE SNAPSHOTS COULD NOT CATCH IT.  All 21 transform seeds passed unchanged,
because none of them holds one of the 8 instances — the regression was found
only by fingerprinting the export before a rebuild and diffing against what
production still served.  A rule with 8 instances corpus-wide needs a test that
names them, not a sample that might contain one.
"""
from __future__ import annotations

import pytest

from britannica.pipeline.stages.elements import ElementContext, process_elements
from britannica.pipeline.stages.elements._tables import br_stack


def _render(text: str) -> str:
    return process_elements(text, ElementContext(volume=7))


def test_br_stack_is_unconditional():
    """Neither side being empty removes the break."""
    assert br_stack("A", "B") == "A«BR»B"
    assert br_stack("", "Na") == "«BR»Na"
    assert br_stack("O", "") == "O«BR»"
    assert br_stack("", "") == "«BR»"


@pytest.mark.parametrize("source,expected", [
    ("{{dual line||Na}}", "«BR»Na"),
    ("{{dual line|CHCl|}}", "CHCl«BR»"),
    ("{{dual line|A|B}}", "A«BR»B"),
])
def test_empty_cell_keeps_its_line(source, expected):
    assert _render(source) == expected


def test_indented_cell_still_keeps_its_line():
    """The corpus spells one of the 8 as `{{dual line|:CHCl|}}`, whose leading
    `:` is a wikitext INDENT and becomes its own div — the cell is not bare
    text.  What matters is unchanged: the empty half still contributes a break."""
    assert _render("{{dual line|:CHCl|}}").endswith("«BR»")


def test_coumarones_formula_stacks_three_lines():
    """The shape the regression broke, spelled as the source spells it."""
    out = _render("C{{sub|6}}H{{sub|4}} H{{dual line||O}}{{dual line||Na}}")
    assert "H«BR»O«BR»Na" in out
