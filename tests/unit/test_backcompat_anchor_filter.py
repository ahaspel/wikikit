"""Every back-compat «ANCHOR» must be filtered back out of the section list.

The accent fix ([[project_bogo_url_reslug]]'s sibling) changed `#section-…`
fragments for 102 articles, so the producers emit an extra «ANCHOR» carrying the
OLD slug to keep existing links landing.  That anchor is invisible markup, and
`export.sections.detect_sections` drops it by RECOMPUTING `section_slug` from the
anchor's own label.  The recomputation only matches if the producer labelled the
anchor with the very text it minted the slug from — a roman-numeral prefix or a
stray marker on one side and not the other breaks the match silently, and the
anchor surfaces as a duplicate section in the download bundle (which drops
`kind`).  This binds the two ends together.
"""
from britannica.export.sections import detect_sections
from britannica.pipeline.stages.elements._anchor import anchor_marker
from britannica.util.strings import anchor_slug, section_slug

# Names whose two slugs differ — i.e. every name that triggers a back-compat anchor.
DIVERGENT = [
    "Kościuszko", "Sömmerring", "Stanislaus Leszczyński", "Zürich",
    "IV.—Kościuszko", "B.—Sömmerring", "2.—Zürich",
]


def test_divergent_names_really_do_diverge():
    """Guard the fixture itself: if these stopped diverging the test would pass
    vacuously, proving nothing."""
    for name in DIVERGENT:
        bare = name.split("—")[-1]
        assert section_slug(bare) != anchor_slug(bare), name


def test_backcompat_anchor_never_reaches_the_section_list():
    for name in DIVERGENT:
        bare = name.split("—")[-1]          # what the producer mints from
        legacy = section_slug(bare)
        body = (anchor_marker(legacy, bare)
                + f"«SEC:{anchor_slug(bare)}|{name}»body text")
        secs = detect_sections(body)
        kinds = [s["kind"] for s in secs]
        assert kinds == ["sec"], f"{name!r} leaked a back-compat anchor: {secs}"


def test_a_genuine_anchor_is_still_kept():
    """The filter must not eat real anchors — `_anchor` mints with `anchor_slug`,
    so a genuine one never looks like a legacy slug."""
    body = anchor_marker(anchor_slug("Kościuszko"), "Kościuszko") + "text"
    assert [s["kind"] for s in detect_sections(body)] == ["anchor"]
