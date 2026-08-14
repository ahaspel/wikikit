"""The `«EQN:LABEL»` slot is `»`-delimited plain text, and the parens are the
renderer's.

Both halves failed in the corpus: HYDRAULICS' number arrived as `<br>(15)` and
shipped as the label `«BR` — the number lost, a mangled marker in its place —
and MECHANICS' `(8)` / `(9)` rendered as `((8))` / `((9))`.
"""
import pytest

from britannica.pipeline.stages.elements._math import (
    _eqn_label_text, _eqn_strip_paren_label,
)


@pytest.mark.parametrize("label,want", [
    ("«BR»(15)", "15"),          # HYDRAULICS — marker AND its own parens
    ("(9)", "9"),                # MECHANICS
    ("(8)", "8"),
    ("«I»7«/I»", "7"),           # markup the margin cannot show anyway
    ("1", "1"),                  # the 973 that were always right
    ("2a", "2a"),
    ("", ""),
    ("  (15)  ", "15"),
])
def test_label_reduces_to_its_number(label, want):
    assert _eqn_label_text(label) == want


def test_no_marker_survives_into_a_delimited_slot():
    """Whatever else it does, it may not leave a `«` for the `»`-delimited
    slot to truncate at."""
    assert "«" not in _eqn_label_text("«BR»(15)«I»x«/I»")


@pytest.mark.parametrize("label,want", [
    ("(6).", "6"),      # INTERPOLATION
    ("(9),", "9"),      # ORDNANCE
    ("(10a).", "10a"),
])
def test_sentence_punctuation_after_the_number_is_not_part_of_it(label, want):
    """The source files its numbers inside the running sentence, so the slot
    arrives with the comma or full stop that followed the `(n)` on the page."""
    assert _eqn_label_text(label) == want
    assert _eqn_strip_paren_label(label) == want
