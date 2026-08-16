"""THE «AL» reader — one grammar, three consumers, zero forks.

The «AL» grammar was spelled as a private regex in the xref extractor, the
5.4 contributor pass, and the article-JSON bake — the exact three-spellings
shape that silently unlinked «LN» references whose display carried markers.
`markers.iter_al_markers` / `sub_al_markers` are now the only spelling.
"""
from britannica.markers import iter_al_markers, sub_al_markers


PLAIN = "by «AL:John Smith|J. Smith«/AL» here"
MARKED = "signed «AL:Hugh Chisholm|«SC»h. ch.«/SC»«/AL»."


def test_reads_plain_and_marked_displays():
    (m,) = iter_al_markers(PLAIN)
    assert (m.target, m.display) == ("John Smith", "J. Smith")
    (m,) = iter_al_markers(MARKED)
    assert (m.target, m.display) == ("Hugh Chisholm", "«SC»h. ch.«/SC»")
    assert MARKED[m.start:m.end].endswith("«/AL»")


def test_pipeless_marker_is_left_visible():
    """A pipe-less «AL» is malformed; every reader before refused it and the
    marker leaked visibly — same posture, one owner."""
    bad = "x «AL:No Pipe«/AL» y"
    assert list(iter_al_markers(bad)) == []
    assert sub_al_markers(bad, lambda m: "GONE") == bad


def test_sub_rewrites_through_the_reader():
    out = sub_al_markers(PLAIN + " " + MARKED,
                         lambda m: f"[{m.target}]")
    assert out == "by [John Smith] here signed [Hugh Chisholm]."


def test_extractor_reads_a_marked_up_author_display():
    from britannica.xrefs.extractor import extract_xrefs
    (rec,) = extract_xrefs("by «AL:Hugh Chisholm|«SC»Hugh Chisholm«/SC»«/AL»")
    assert rec["xref_type"] == "author"
    assert rec["display"] == "«SC»Hugh Chisholm«/SC»"
