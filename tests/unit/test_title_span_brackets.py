"""A title's span markers must strip even when their attribute contains `]`.

`decode_title` bounded its span patterns on `[^\\]]*` — "anything but a closing
bracket" — but a tooltip attribute may legitimately contain brackets, and the
renderer already knew this:

    # ANATOMY's «SPAN[title:farm [tribute] of the county]»
    # THEORY OF NUMBERS' «SPAN[title:2＝[2,1＋√m]²]»
    _SPAN_TITLE_RE = re.compile(r"«SPAN\\[title:([^«»]*)\\]»")

So the title decoder was wrong for any bracketed tooltip in a headword; nothing
reached it until `{{SIC}}` began emitting a hint containing `[sic]`, and two
titles came out carrying raw markup:

    LAISANT, CHARLES «SPAN[title:[sic] 'ANGE']»ANNE

Bound on the marker delimiters, which cannot occur inside an attribute.
"""
from britannica.pipeline.stages.elements._title import decode_title


def test_a_sic_hint_in_a_headword_leaves_no_markup():
    """The two articles the corpus rebuild caught."""
    assert decode_title(
        "LAISANT, CHARLES «SPAN[title:[sic] 'ANGE']»ANNE«/SPAN»"
    ) == "LAISANT, CHARLES ANNE"
    assert decode_title(
        "RAMSEY«SPAN[title:[sic]]»«/SPAN», SIR ANDREW CROMBIE"
    ) == "RAMSEY, SIR ANDREW CROMBIE"


def test_brackets_that_predate_sic_entirely():
    """These shapes exist in the corpus already and would have leaked too.

    The bug was latent, not introduced — `{{SIC}}` only made it reachable.
    """
    assert decode_title(
        "ANATOMY «SPAN[title:farm [tribute] of the county]»X«/SPAN»") == "ANATOMY X"
    assert decode_title(
        "NUMBERS «SPAN[title:2＝[2,1＋√m]²]»Y«/SPAN»") == "NUMBERS Y"


def test_no_title_may_carry_a_marker():
    """The property, not the instances.

    A title is plain text — it becomes an `<h1>`, a search-index entry, a TOC
    line. Whatever markers a headword recursed into, none may survive here.
    """
    for marker in (
        "A «SPAN[title:[sic] 'x']»B«/SPAN»",
        "«SC»small«/SC» CAPS",
        "«B»bold«/B» and «I»italic«/I»",
        "«SPAN[style:font-variant:small-caps]»styled«/SPAN»",
        "«SPAN[title:a ] b ] c]»nested«/SPAN»",
    ):
        out = decode_title(marker)
        assert "«" not in out and "»" not in out, f"marker leaked from {marker!r}: {out!r}"


def test_the_uppercase_span_still_capitalises():
    """`{{uc}}` rides as a text-transform span and must still fire.

    Its pattern was widened by the same fix, so this is the regression guard on
    the widening rather than on the leak.
    """
    assert decode_title(
        "«SPAN[style:text-transform:uppercase]»colenso«/SPAN»") == "COLENSO"
