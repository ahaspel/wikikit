"""Templates a second book brought (the DNB, 2026-10-02), each rendered as
Wikisource renders it (checked with its expandtemplates).  EB1911 uses none of
them except the one-argument `{{DNB lkpl}}`, whose name used to vanish."""
import pytest

from wikikit.pipeline.stages.elements import ElementContext, process_elements


def walk(text):
    return process_elements(text, ElementContext(volume=1))


@pytest.mark.parametrize("source, rendered", [
    ("into action{{--}}probably", "into action—probably"),
    ("a small turret{{peh}} ship", "a small turret- ship"),
    # a superscript, as {{sup}} (letters outside the Unicode table stay as they are)
    ("Right Hon{{sups|ble}} the Barons", "Right Honble the Barons"),
    # citations of other works print their display, unlinked
    ("Mennell's {{DAB lkpl|M'Culloch, Hon. Sir James|Dict. of Austr. Biog.}}; Victoria",
     "Mennell's Dict. of Austr. Biog.; Victoria"),
    ("{{DNB_lkpl|Hamilton, Richard (fl.1688)|Richard}}, and his", "Richard, and his"),
    ("{{The Times lkpl|1893/Obituary/Arthur Locker|Times}}, 26 June", "Times, 26 June"),
    # a Wikipedia link: its display, or with none its target
    ("After the {{w|battle of Bannockburn}} the king", "After the battle of Bannockburn the king"),
    ("After the {{w|Battle of Bannockburn|the battle}} the king", "After the the battle the king"),
    # the namespace switch: our entries are the article view, not the Page: view
    ("{{#ifeq:{{NAMESPACE}}|Page|[[Garrett, Jeremiah (DNB00)|Jeremiah]]|}} Jeremiah",
     " Jeremiah"),
    # a transcriber's note on the scanned copy is not the book (user, 2026-10-02)
    ("ty in 1628.{{user annotation|Adhered onto the page is a [[Notes and Queries|note]].}} He",
     "ty in 1628. He"),
])
def test_renders_as_wikisource_does(source, rendered):
    assert walk(source) == rendered


def test_a_cross_work_citation_with_only_a_name_shows_the_name():
    """EB1911 vol 6 p. 438 read "See the article “,” in the Dict. of Nat.
    Biography": the lone argument is the cited article's name."""
    assert walk("See the article “{{DNB lkpl|Burgh, Ulick de}},” in the") == (
        "See the article “Burgh, Ulick de,” in the")


def test_the_dnb_margin_heading_is_a_shoulder_heading():
    out = walk("{{PT Shoulder Heading|The succession to the crown in 1817.}} Of the")
    assert out.startswith("«SH:the-succession-to-the-crown-in-1817»"
                          "The succession to the crown in 1817.«/SH»")


def test_a_malformed_styler_names_itself():
    """`{{sc}Will. Malm.}}` — a `}` typed for `|` — used to die as an
    AttributeError on None.group; the message must point at the source."""
    with pytest.raises(ValueError, match="malformed styler template"):
        walk("ii. 205; {{sc}Will. Malm.}} ii. 180")
