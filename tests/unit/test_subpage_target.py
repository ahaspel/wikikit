"""`subpage_target` strips Wikisource pagination — without touching markers.

A link target can arrive with EB1911's own markup already marked: SUDAN's
`{{11link|{{sc|Dongola}}: «I»Mudiria«/I»|Dongola (province)}}`.  A close
marker's slash is not a path separator, and treating it as one produced `«#I»`,
which no longer matches the «LN» opener grammar — the marker collapsed to its
2-part reading and the link rendered with the filename as its href.
"""
import pytest

from britannica.markers import MARKER_TOKEN_RE
from britannica.pipeline.stages.elements._link import subpage_target


@pytest.mark.parametrize("path,want", [
    ("Egypt/2 Ancient Egypt", "Egypt#Ancient Egypt"),
    ("Rome/History", "Rome#History"),
    ("Japan/04 Art", "Japan#Art"),
    ("/Egypt/3_History#Mahommedan", "Egypt#Mahommedan"),
    ("Egypt", "Egypt"),
    ("", ""),
])
def test_pagination_is_stripped(path, want):
    assert subpage_target(path) == want


@pytest.mark.parametrize("target", [
    "Africa: Ethnology«/I»",
    "«SC»Dongola«/SC»: «I»Mudiria«/I»",
    "§ «I»Archaeology«/I»",
])
def test_a_marker_is_not_a_path(target):
    """No path here at all — every slash belongs to a close marker."""
    assert subpage_target(target) == target


def test_markers_survive_a_real_path():
    """Both at once: the path is stripped, the markup is carried."""
    got = subpage_target("«SC»Egypt«/SC»/2 Ancient Egypt")
    assert got == "«SC»Egypt«/SC»#Ancient Egypt"


@pytest.mark.parametrize("target", [
    "Africa: Ethnology«/I»",
    "«SC»Dongola«/SC»: «I»Mudiria«/I»",
    "«SC»Egypt«/SC»/2 Ancient Egypt",
])
def test_every_marker_out_is_a_marker_in(target):
    """The output's marker tokens are exactly the input's — none invented,
    none mangled.  `«#I»` would fail this even if the text looked right."""
    assert (MARKER_TOKEN_RE.findall(subpage_target(target))
            == MARKER_TOKEN_RE.findall(target))
