"""The shipped `xrefs` field lists OTHER articles — the panel, the dataset's
xref_edges and the indexer all read it so.  NUMBER listed NUMBER in its own
Cross-references panel (111 such entries in 50 articles, 2026-11-07), and every
"fig. 3" link would add one more."""
from wikikit.export.article_json import panel_xrefs

SELF = "19-0870-number.json"


def test_a_jump_within_the_page_is_not_a_cross_reference():
    xs = [
        {"status": "resolved", "target_filename": SELF, "target_section": "fig-3"},
        {"status": "resolved", "target_filename": "11-0695-geometry.json"},
        {"status": "unresolved", "normalized_target": "Nowhere"},
    ]
    assert panel_xrefs(xs, SELF) == [xs[1]]
