"""Style-unification, Step 2: context-independence of the STYLED wrapper.

The acceptance criterion for routing styled `<div>`/`<p>`/`<span>` wrappers to the
ONE `_process_styled` producer (which recurses its content through the main
dispatch) is that **style is orthogonal to structure**: the SAME content must
produce the SAME markers whether it sits at top level or inside a styled wrapper.

A `<math>` / `{|`-table / styled-`<div>` / image inside a styled wrapper must be
handled by its OWN producer (`«MATH»` / `«HTMLTABLE»` / `«DIV[style]»` /
`{{IMG:}}`), not leaked raw and not re-classified by a second partial classifier.
This pins exactly that — the depth-0 markers appear byte-identically nested.

This is the regression guard for the motivating bug:
`<p {{Ts|ac}}><math>x^2+1</math></p>` used to leak the `<math>` raw because the
faithful figure decomposer was a SECOND, partial classifier; now the inner
recurses through the main dispatch and the `<math>` is produced.
"""
from __future__ import annotations

import pytest

from britannica.pipeline.stages.elements import process_elements, ElementContext


def _render(text: str) -> str:
    return process_elements(text, ElementContext(volume=1, page_number=1))


# Each fixture is a self-contained construct whose producer is NOT body-text:
# MATH, TABLE, a nested STYLED `<div>`, and an IMAGE (the `_p_ts` image-drop case).
_FIXTURES = {
    "math": "<math>x^2</math>",
    "wiki_table": "{|\n|-\n| a || b\n|}",
    "styled_div": "<div {{Ts|sm92}}>small</div>",
    "image": "[[File:Foo.png|120px]]",
}

# Three wrapper spellings — block `<div>`, paragraph `<p>` (carrying `{{Ts}}`),
# inline `<span>` — each carrying a DISTINCT, non-centre style so the wrapper's
# own marker is unambiguous and we can assert the inner is verbatim-nested.
_WRAPPERS = {
    "div_style": '<div style="padding:1em">{X}</div>',
    "p_ts": '<p {{Ts|sm92}}>{X}</p>',
    "span_style": '<span style="color:red">{X}</span>',
}


@pytest.mark.parametrize("fixture_name", list(_FIXTURES))
@pytest.mark.parametrize("wrapper_name", list(_WRAPPERS))
def test_styled_wrapper_is_context_independent(fixture_name, wrapper_name):
    """The fixture's depth-0 markers appear byte-identically inside the wrapper —
    style is orthogonal to structure, the inner is produced by its own producer."""
    fixture = _FIXTURES[fixture_name]
    depth0 = _render(fixture).strip()
    assert depth0, "fixture produced empty output at depth 0"

    wrapped = _render(_WRAPPERS[wrapper_name].replace("{X}", fixture))
    assert depth0 in wrapped, (
        f"{fixture_name} markers not nested verbatim inside {wrapper_name}: "
        f"depth0={depth0!r} wrapped={wrapped!r}")


def test_math_in_styled_wrapper_does_not_leak_raw():
    """The motivating bug, pinned: `<math>` inside a centred `<p {{Ts|ac}}>` is
    PRODUCED (`«MATH»`), never leaked raw (`<math>…`)."""
    out = _render("<p {{Ts|ac}}><math>x^2+1</math></p>")
    assert out == "«CTR»«MATH:x^2+1«/MATH»«/CTR»"
    assert "<math" not in out


def test_image_in_styled_p_is_not_dropped():
    """The `_p_ts` image-drop defect, pinned: an image inside a styled `<p>` is
    produced (`{{IMG:}}`), not silently dropped."""
    out = _render("<p {{Ts|ac}}>[[File:Foo.png|200px]]</p>")
    assert out == "«CTR»{{IMG:Foo.png|width=200}}«/CTR»"


def test_pure_centre_collapses_to_ctr_block_only():
    """A pure-centre block (`align=center` / `{{Ts|ac}}` / `style=text-align:
    center`) collapses to the canonical `«CTR»`; a centred SPAN stays an inline
    `«SPAN[style:text-align:center]»` (centring is a block concept)."""
    assert _render("<div align=center>x</div>") == "«CTR»x«/CTR»"
    assert _render("<p {{Ts|ac}}>x</p>") == "«CTR»x«/CTR»"
    assert _render('<div style="text-align:center">x</div>') == "«CTR»x«/CTR»"
    assert _render('<span style="text-align:center">x</span>') == \
        "«SPAN[style:text-align:center]»x«/SPAN»"


def test_top_level_br_in_styled_block_is_line_break():
    """A styled block is a display block; its own (top-level) `<br>` is a line
    break carried as `«BR»` (not collapsed to a space by the body producer)."""
    assert _render("<div align=center>a<br />b</div>") == "«CTR»a«BR»b«/CTR»"


def test_editorial_title_span_is_unwrapped_not_carried():
    """A `<span style="border-bottom:1px dashed red" title="amended from …">`
    is a Wikisource OCR-correction highlight (editorial provenance) — its text is
    kept, the decoration dropped (owned by `_handle_title_spans`), NOT carried as
    `«SPAN[style]»`."""
    out = _render(
        'word <span style="border-bottom:1px dashed red" '
        'title="amended from wrod">word</span> end')
    assert "border-bottom" not in out
    assert "«SPAN" not in out
    assert "word word end" in out
