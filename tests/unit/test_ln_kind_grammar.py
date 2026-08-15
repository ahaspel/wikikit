"""The «LN» kind parameter — `«LN[qv]:target|display«/LN»` (J7 slice 0).

A reference's KIND (link / qv / see …) governs its 5.4 resolution policy
(2026-07-20: collapsing kinds caused the JOHN VENN→McADAM false-positive
class).  Kind is an ATTRIBUTE of the one reference element, not a new tag —
the `«MATH[fs=N]` pattern — and it is consumed AT BAKE: 5.4 resolves by kind
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


def test_the_one_reader_handles_both_forms():
    """`markers.iter_ln_markers` — THE «LN» reader every pre-bake consumer
    (extractor, bake, bio scan) iterates — reads the kinded form like the
    plain one."""
    from britannica.markers import iter_ln_markers
    for body, want_kind in ((PLAIN, None), (KINDED, "qv")):
        (m,) = iter_ln_markers(body)
        assert (m.kind, m.target, m.display) == (want_kind, "Geber", "Geber")
        assert body[m.start:m.end].endswith("«/LN»")


def test_extractor_reads_a_marked_up_display():
    """A display carrying markers (`«SC»Parasitic Diseases«/SC»`, a printed
    cross-reference in small caps) MUST still extract — the old `([^«]*)`
    display group matched none of these, so no xref was filed, nothing bound
    a target, and the bake silently stripped the link to plain text."""
    from britannica.xrefs.extractor import extract_xrefs
    body = ("see «LN:Parasitic Diseases|«SC»Parasitic "
            "Diseases«/SC»«/LN» for details")
    (rec,) = extract_xrefs(body)
    assert rec["xref_type"] == "link"
    assert rec["normalized_target"] == "PARASITIC DISEASES"
    assert rec["display"] == "«SC»Parasitic Diseases«/SC»"
