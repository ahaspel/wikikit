"""The drop-cap wraps one rendered GLYPH, and only when that glyph is a LETTER.

Two rules learned in the same place, one after the other.

FIRST, the unit is a glyph, not a character.  `_render_title_h1` escapes the
title before wrapping its opening in the enlarged span, so a title the edition
prints in quotation marks — `"SURVILLE, CLOTILDE DE,"`, the quotes are EB1911
flagging the persona as apocryphal — starts with `&quot;`, and a
single-CHARACTER drop-cap split the entity: `<span>&</span>quot;…` rendered a
big ampersand and the literal text `quot;` on the live page.

SECOND, and only a letter takes it.  Keeping the entity whole still set the
quotation mark itself at 1.6em, which is what the rule above was really
avoiding.  Sixteen titles open on a quotation or transliteration mark —
`“CHALLENGER” EXPEDITION`, `’AḤAI`, `‘ALQAMA IBN ‘ABADA` — and each was
enlarging punctuation.  Such a title now gets NO drop-cap; the second character
is not promoted in its place, because the mark is part of the word.

The entity rule still matters even though no title starts with `&quot;` today:
the letter test reads the UNESCAPED glyph, so it must see `&quot;` whole to know
it is punctuation at all.
"""
from britannica.render.article import RenderContext, _render_title_h1

DROPCAP = "font-size:1.6em"


def _h1(title):
    ctx = RenderContext(volume=26, scan_url=None, unproofed_pages=set())
    return _render_title_h1(f"«TITLE:{title}«/TITLE»", ctx)


def test_plain_title_dropcaps_one_letter():
    assert ">D</span>ARWIN" in _h1("DARWIN, CHARLES ROBERT")


def test_accented_letter_still_dropcaps():
    """A letter is a letter — `É` is not punctuation."""
    assert ">É</span>" in _h1("ÉCOLE NORMALE")


def test_quotation_mark_takes_no_dropcap():
    for title in ('“CHALLENGER” EXPEDITION', '“ADDRESS, THE,”',
                  '"SURVILLE, CLOTILDE DE,"'):
        h = _h1(title)
        assert DROPCAP not in h, f"{title!r} enlarged its quotation mark"


def test_transliteration_mark_takes_no_dropcap():
    """`’AḤAI`, `‘ALQAMA` — the mark is part of the word, so no cap and no
    promotion of the letter behind it."""
    for title in ("’AḤAI", "‘ALQAMA IBN ‘ABADA", "’S HERTOGENBOSCH"):
        h = _h1(title)
        assert DROPCAP not in h, f"{title!r} enlarged its transliteration mark"
        assert ">A</span>" not in h and ">S</span>" not in h


def test_entity_is_never_split():
    """The glyph stays atomic — the test that made the letter rule possible."""
    h = _h1('"SURVILLE, CLOTILDE DE,"')
    assert ">&</span>" not in h
    assert "quot;S" not in h.replace("&quot;", "")
