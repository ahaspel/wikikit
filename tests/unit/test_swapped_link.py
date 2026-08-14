"""The link swap recovers the source's argument ORDER — never its spelling.

`swapped_link` decides whether a two-positional source template was filed
backwards, and what the reader should then see.  The spelling half of that
decision is what STILT got wrong: `{{1911link|Oystercatcher|Oyster-catcher}}`
was swapped so the wiki page name appeared in print instead of the words
EB1911 set in type.
"""
import pytest

from britannica.export.article_json import swapped_link

# Filed titles come from EB1911 itself, so a title IS a printed spelling.
TITLES = {
    "OYSTER-CATCHER": "20-0462-oystercatcher.json",
    "BAG-PIPE": "04-0001-bagpipe.json",
    "MARK, GOSPEL OF ST": "17-0001-mark-gospel.json",
    "MARK": "17-0002-mark.json",
    "COPE, EDWARD DRINKER": "07-0109-cope.json",
    "SPAIN": "25-0001-spain.json",
    "EXODUS, THE": "10-0001-exodus.json",
}


def test_leaves_prose_alone():
    """A display TERSER than its target is prose doing its job."""
    assert swapped_link("Spain#History", "Spain", TITLES) is None
    assert swapped_link("Exodus, The", "Exodus", TITLES) is None


def test_recovers_a_filed_title_standing_in_prose():
    """`Cope, Edward Drinker` mid-sentence is the inverted-order tell."""
    target, shown, fn = swapped_link("Cope", "Cope, Edward Drinker", TITLES)
    assert target == "Cope, Edward Drinker"          # what we point at
    assert shown == "Cope"                           # what the reader sees
    assert fn == TITLES["COPE, EDWARD DRINKER"]


def test_extending_title_is_the_reference():
    """Both sides real titles: the EXTENDING one is the reference."""
    target, shown, fn = swapped_link("Mark", "Mark, Gospel of St", TITLES)
    assert target == "Mark, Gospel of St"
    assert shown == "Mark"
    assert fn == TITLES["MARK, GOSPEL OF ST"]


def test_abstains_when_neither_title_extends_the_other():
    titles = {"DRAGON": "a.json", "DRACO": "b.json"}
    assert swapped_link("Dragon", "Draco", titles) is None


@pytest.mark.parametrize("target,display,printed", [
    # STILT, and the compound class it stands for: arg1 is the modern wiki page
    # name, arg2 the EB1911 spelling that our corpus is filed under.
    ("Oystercatcher", "Oyster-catcher", "Oyster-catcher"),
    ("Bagpipe", "Bag-pipe", "Bag-pipe"),
])
def test_one_name_two_spellings_shows_the_printed_one(target, display, printed):
    swap = swapped_link(target, display, TITLES)
    assert swap is not None, "the swap must still fire — it is what resolves"
    got_target, shown, _ = swap
    assert got_target == display, "point at the filed title so the link lands"
    assert shown == printed, "but print the spelling EB1911 used"


@pytest.mark.parametrize("target,display", [
    ("Bag-pipe", "Bagpipe"),        # {{EB1911 article link|Bagpipe|Bag-pipe}}
    ("Oyster-catcher", "oystercatcher"),
])
def test_display_first_templates_reach_it_the_other_way_round(target, display):
    """Nothing needs SWAPPING here — the producer already put the filed title in
    the target — but the reader still gets the wiki page name.  Whichever slot
    the filed title landed in, it is EB1911's spelling."""
    got = swapped_link(target, display, TITLES)
    assert got is not None
    assert got[0] == target and got[1] == target


@pytest.mark.parametrize("target,display", [
    ("Monsoon", "“monsoon”"),       # the source's prose, quoted
    ("Helm Wind", "helm-wind"),
])
def test_a_longer_display_is_prose_and_is_left_alone(target, display):
    """The modern page name CLOSES UP what EB1911 hyphenates, so it is always
    shorter.  A display that is LONGER is the page's own words."""
    titles = dict(TITLES, **{target.upper(): "x.json"})
    assert swapped_link(target, display, titles) is None


def test_case_only_difference_is_not_a_reference_either():
    """`oystercatcher` vs `Oyster-catcher` folds alike — same name, one spelling."""
    swap = swapped_link("oystercatcher", "Oyster-catcher", TITLES)
    assert swap is not None
    assert swap[1] == "Oyster-catcher"
