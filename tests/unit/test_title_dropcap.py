"""The drop-cap wraps one rendered GLYPH, never a slice of an entity.

`_render_title_h1` escapes the title before wrapping its first character in
the enlarged span, so a title the edition prints in quotation marks —
`"SURVILLE, CLOTILDE DE,"`, the quotes are EB1911 flagging the persona as
apocryphal — starts with `&quot;`, and a single-CHARACTER drop-cap split the
entity: `<span>&</span>quot;…` rendered a big ampersand and the literal text
`quot;` on the live page.
"""
from britannica.render.article import RenderContext, _render_title_h1


def _h1(title):
    ctx = RenderContext(volume=26, scan_url=None, unproofed_pages=set())
    return _render_title_h1(f"«TITLE:{title}«/TITLE»", ctx)


def test_quoted_title_keeps_its_entity_whole():
    h = _h1('"SURVILLE, CLOTILDE DE,"')
    assert "&quot;</span>" in h          # the WHOLE entity is the drop-cap
    assert ">&</span>" not in h          # never a bare ampersand
    assert "quot;S" not in h.replace("&quot;", "")  # no literal `quot;` text


def test_plain_title_still_dropcaps_one_letter():
    h = _h1("DARWIN, CHARLES ROBERT")
    assert ">D</span>ARWIN" in h
