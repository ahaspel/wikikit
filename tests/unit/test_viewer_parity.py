"""The slug rule has ONE owner, and the viewer is not allowed to grow a second.

Python's ``wikikit.util.strings.anchor_slug`` bakes ``id="section-<slug>"``
into ``rendered_html`` — TOC links and anchors alike — and the viewer only ever
JUMPS to ids that already exist (it reads ``window.location.hash``; it computes
nothing).  There used to be a ``sectionSlug`` copy in viewer.html from the
pre-baked-render era; this test EXECUTED it against the Python owner, because a
cross-runtime duplicate can only be bound, never imported.  The copy is deleted
(no callers — the anchors it once minted come baked), so the binding died with
it, and what remains to pin is its ABSENCE: a reintroduced JS slug computation
is a second owner of the rule, and the first anyone would hear of a divergence
is a dead in-page link.  If this test fails, either delete the new copy (the
viewer still doesn't need one) or bring back the execution binding that used to
live here (run the JS in node against ``section_slug`` on shared cases).
"""
import re
from wikikit.site.build import PAGES

VIEWER = PAGES / "viewer.html"


def test_the_viewer_does_not_own_a_slug_rule():
    src = VIEWER.read_text(encoding="utf-8")
    assert not re.search(r"function\s+sectionSlug\b", src), (
        "viewer.html defines sectionSlug again — a second owner of the slug "
        "rule across the runtime boundary.  Delete it (anchors come baked in "
        "rendered_html) or restore the node-execution parity binding.")
