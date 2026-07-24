"""The «LN» kind parameter — `«LN[qv]:target|display«/LN»` (J7 slice 0).

A reference's KIND (link / qv / see …) governs its 6b5 resolution policy
(2026-07-20: collapsing kinds caused the JOHN VENN→McADAM false-positive
class).  Kind is an ATTRIBUTE of the one reference element, not a new tag —
the `«MATH[fs=N]` pattern — and it is consumed AT BAKE: 6b5 resolves by kind
and writes the plain 3-part form, so no `[kind]` survives into a post-bake
body.  Every PRE-bake «LN» consumer must therefore treat the parameterized
form exactly like the plain one.  This test drives both forms through each.
"""
from britannica.markers import markers_to_text


PLAIN = "see «LN:Geber|Geber«/LN» here"
KINDED = "see «LN[qv]:Geber|Geber«/LN» here"


def test_markers_to_text_strips_both_forms():
    assert markers_to_text(PLAIN) == "see Geber here"
    assert markers_to_text(KINDED) == "see Geber here"


def test_markdown_links_both_forms():
    from britannica.export.markdown import body_to_markdown
    assert body_to_markdown(PLAIN) == body_to_markdown(KINDED)


def test_render_decodes_kinded_open():
    from britannica.render.inline import _LN_OPEN_RE
    plain, kinded = _LN_OPEN_RE.match("«LN:T|D|"), _LN_OPEN_RE.match("«LN[qv]:T|D|")
    assert plain and kinded
    assert plain.groups() == kinded.groups()


def test_resolver_prose_strip_handles_kinded():
    from britannica.link_resolver import prose_window
    body = "before «LN[see]:Roman Art|Roman Art«/LN» after"
    w = prose_window(body, "«LN[see]:Roman Art|Roman Art«/LN»")
    assert "«LN" not in w


def test_bake_regex_matches_both_forms():
    """The 6b5 2-part bake pattern — where `[kind]` is consumed and dropped."""
    import re
    pat = re.compile(r"«LN(?:\[[a-z_]*\])?:([^|]*)\|([^«]*)«/LN»")
    for body in (PLAIN, KINDED):
        m = pat.search(body)
        assert m and m.group(1) == "Geber" and m.group(2) == "Geber"


def test_display_extraction_handles_kinded():
    from britannica.export.article_json import _LN_DISPLAY_RE
    for body in (PLAIN, KINDED):
        assert _LN_DISPLAY_RE.search(body).group(1) == "Geber"
