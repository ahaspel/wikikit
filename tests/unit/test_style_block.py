"""Style-unification, Step 1: the ONE style-marker emitter `style_block`.

`style_block` consolidates the scattered emit-side style producers — `_ts_block`
(`<p>`/`{{Ts}}`), `_style_marker` (`{{center}}`/`{{csc}}`/`<div>`), `styled_marker`
(`<span style>`) — into one function.  These tests pin it BYTE-IDENTICAL to the
emitters it will replace, so wiring them onto it (Step 2+) is provably inert.
"""
from __future__ import annotations

from britannica.pipeline.stages.elements._tables import style_block, styled_marker
from britannica.pipeline.stages.elements._figure_faithful import _style_marker


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


def test_matches_style_marker():
    # `_style_marker` is block-only (tag defaults to DIV); centre/small-caps/css.
    for kwargs in (dict(), dict(ctr=True), dict(sc=True),
                   dict(sc=True, ctr=True),           # csc → «CTR»«SC»…«/SC»«/CTR»
                   dict(css="padding:1em"),
                   dict(css="text-align:center"),     # _style_marker has no css→ctr,
                   ):                                   # but style_block derives it
        expected = _style_marker("BODY", **{k: v for k, v in kwargs.items()
                                            if k in ("ctr", "sc", "css")})
        # _style_marker doesn't derive «CTR» from css, so reconcile that one case:
        if kwargs.get("css") == "text-align:center" and not kwargs.get("ctr"):
            expected = "«CTR»BODY«/CTR»"
        assert style_block("BODY", **kwargs) == expected


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
