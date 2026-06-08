"""Style-unification, Step 1: the ONE style-marker emitter `style_block`.

`style_block` consolidated the scattered emit-side style producers — `_ts_block`
(`<p>`/`{{Ts}}`), `_style_marker` (`{{center}}`/`{{csc}}`/`<div>`), `styled_marker`
(`<span style>`) — into one function.  These tests pin its behaviour against the
local `_ts_block_ref` and the live `styled_marker`.  (The `_style_marker`
byte-identity cross-check retired with that producer in the figure collapse.)
"""
from __future__ import annotations

from britannica.pipeline.stages.elements._tables import style_block, styled_marker


def _ts_block_ref(css_list, content):
    """Verbatim logic of body_text `_ts_block` (the `<p {{Ts}}>` / `{{Ts}}` path)."""
    if not css_list:
        return content
    if css_list == ["text-align:center"]:
        return f"«CTR»{content}«/CTR»"
    return f"«DIV[style:{';'.join(css_list)}]»{content}«/DIV»"


def test_matches_ts_block():
    for css_list in ([], ["text-align:center"],
                     ["text-align:center", "font-size:92%"],
                     ["padding-left:0.5em", "padding-right:0.5em"],
                     ["text-indent:-2em", "padding-left:4em"]):
        assert style_block("BODY", css=";".join(css_list)) == \
            _ts_block_ref(css_list, "BODY")


def test_matches_span_carry():
    # `<span style>` carry → `styled_marker("SPAN", …)`.  A centred SPAN stays an
    # inline «SPAN[style:text-align:center]» — the «CTR» shortcut is block-only.
    for css in ("border-bottom:1px dashed red", "position:relative;top:.8em",
                "text-align:center"):
        assert style_block("X", css=css, tag="SPAN") == \
            styled_marker("SPAN", css, "X")


def test_edges():
    assert style_block("", css="text-align:center") == ""   # empty content → ""
    assert style_block("X", css="") == "X"                   # no style → unwrap
    assert style_block("X") == "X"
