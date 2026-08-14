"""A guillemet outside a marker token, and the signature that identifies it.

This is the lexicon half of the mangled-marker gate.  It cannot say whether a
guillemet is ours or the source's — only `tools/diagnostics/mangled_markers.py`
can, by asking the source — so what it owes is a signature STABLE across the
pipeline, or the comparison drowns in false positives.
"""
import pytest

from britannica.markers import unaccounted_guillemets


def sigs(text):
    return [s for s, _ctx in unaccounted_guillemets(text)]


@pytest.mark.parametrize("text", [
    "«I»cocaine«/I»",
    "«LN:08-0435.json|Dongola|Dongola (province)«/LN»",
    "plain prose with no markers at all",
    "«SC»Dongola«/SC»: «I»Mudiria«/I»",
    "",
])
def test_well_formed_markers_are_accounted_for(text):
    assert sigs(text) == []


def test_finds_the_mangled_close_marker():
    """`subpage_target` split `«/I»` on its slash and produced this."""
    assert sigs("«I»acid«#I»") == ["#I»"]


def test_finds_a_marker_whose_close_was_eaten():
    """`_render_eqn` read its label to the first `»` and emitted `(«BR)`."""
    assert sigs('<div class="math-system-label">(«BR)</div>') == ["BR)"]


@pytest.mark.parametrize("variant", [
    "the xvp«w\nand are also",          # source: a newline
    "the xvp«w and are also",           # marker stream: collapsed to a space
    "the xvp«w\n\n\nand are also",      # paragraph break
    "the xvp«w   and   are also",
])
def test_signature_ignores_whitespace(variant):
    """Whitespace is the one thing the pipeline legitimately rewrites, so the
    same guillemet must sign identically however its neighbours are spaced."""
    assert sigs(variant) == ["wan"]


def test_signature_distinguishes_different_neighbours():
    assert sigs("a«#I»") != sigs("a«BR)")


def test_context_is_returned_for_reporting():
    (sig, ctx), = unaccounted_guillemets("some prose here «#I» and more prose")
    assert sig == "#I»"
    assert "some prose here" in ctx


def test_source_mojibake_is_reported_too():
    """`Â«` is Wikisource's, but this function does not adjudicate — it reports
    every unaccounted guillemet and lets the source comparison decide."""
    assert sigs("absorption through thÂ« lymph") == ["lym"]
