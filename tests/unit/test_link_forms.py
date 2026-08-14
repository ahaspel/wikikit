"""The seven link forms read their slots two ways, and only two.

`_link_display` (the PEEL) and the wrap (the PRODUCE) now read the SAME slots,
so the classified display cannot drift from what the wrap emits — they used to
be separate parses kept in step by hand.
"""
import pytest

from britannica.export.article_json import _resolve_ln_markers
from britannica.pipeline.stages.elements._link import (
    _LINK_FORMS, _link_display, _slots_bracket, _slots_display_first,
    _slots_target_first,
)


def test_every_form_is_routed():
    """The classifier's label set IS the form table — one list, not two."""
    from britannica.pipeline.stages.elements._link import _LINK_LABELS
    assert _LINK_LABELS == frozenset(_LINK_FORMS)


@pytest.mark.parametrize("raw,want", [
    ("[[Target|Display]]", ("Target", "Display")),
    ("[[Target]]", ("Target", None)),
    ("[[Target|]]", ("Target", None)),
])
def test_bracket_slots(raw, want):
    assert _slots_bracket(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("{{lkpl|Target|Display}}", ("Target", "Display")),
    ("{{lkpl|Target}}", ("Target", None)),
    ("{{1911link|no c=1|Target|Display}}", ("Target", "Display")),  # named arg dropped
    ("{{lkpl}}", ("", None)),
])
def test_target_first_slots(raw, want):
    assert _slots_target_first(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("{{EB1911 article link|Display|Target}}", ("Target", "Display")),
    # One positional is BOTH — shown and followed.
    ("{{EB1911 article link|Both}}", ("Both", "Both")),
])
def test_display_first_slots(raw, want):
    assert _slots_display_first(raw) == want


def test_markup_marks_the_printed_slot():
    """A Wikisource page name is plain text, so a marked-up slot is what the
    PAGE PRINTS — `{{1911link|«I»organistrum«/I»|Organistrum}}` was showing the
    reader the page name and flattening the italics into the target."""
    assert _slots_target_first("{{1911link|«I»organistrum«/I»|Organistrum}}") == (
        "Organistrum", "«I»organistrum«/I»")


def test_plain_slots_keep_the_template_convention():
    assert _slots_target_first("{{1911link|Oystercatcher|Oyster-catcher}}") == (
        "Oystercatcher", "Oyster-catcher")


@pytest.mark.parametrize("label,raw,want", [
    # A template's lone positional is its own display; a bracket's absent one is nothing.
    ("TARGET_FIRST_LINK", "{{lkpl|Egypt/3 History}}", "Egypt/3 History"),
    ("WIKILINK", "[[Target]]", ""),
    ("EB1911_ARTICLE_LINK", "{{EB1911 article link|Shown|Filed}}", "Shown"),
])
def test_peel_reads_the_same_slots(label, raw, want):
    assert _link_display(raw, label) == want


def test_resolution_scans_so_a_display_may_carry_markers():
    """The display is the RECURSED slot: `«SC»Africa«/SC»: «I»Ethnology«/I»` is a
    printed cross-reference.  The old `([^«]*)` display group matched none of
    these, so they never resolved and rendered as plain text."""
    marker = "«LN:Africa#Ethnology|«SC»Africa«/SC»: «I»Ethnology«/I»«/LN»"
    seen = []

    def resolve(m):
        seen.append((m.group(1), m.group(2), m.group(3)))
        return "OK"

    assert _resolve_ln_markers(marker, resolve) == "OK"
    assert seen == [(None, "Africa#Ethnology",
                     "«SC»Africa«/SC»: «I»Ethnology«/I»")]


def test_resolution_reads_the_kind_slot():
    seen = []
    _resolve_ln_markers("«LN[see]:Rome|Rome«/LN»",
                        lambda m: seen.append(m.group(1)) or "")
    assert seen == ["see"]


def test_an_unclosed_marker_is_left_alone():
    """A resolver that invented a close would hide the producer bug that failed
    to write one."""
    text = "before «LN:Rome|Rome after"
    assert _resolve_ln_markers(text, lambda m: "X") == text
