"""The xref key folds typography, because a reference and a filed title spell
one name two ways.

335 article links resolved to nothing on this alone — they rendered as flat text
where a link belongs.  The fold applies to the FINISHED key, so it can only
merge: whatever matched before still matches.
"""
import pytest

from britannica.xrefs.normalizer import normalize_xref_target as N


@pytest.mark.parametrize("reference,filed", [
    ("O'Neill", "O’NEILL"),                                  # straight vs curly
    ("Queen Anne's Bounty", "QUEEN ANNE’S BOUNTY"),
    ("St Paul's Cathedral", "ST PAUL’S CATHEDRAL"),
    ("Seven Weeks’ War", "SEVEN WEEKS' WAR"),                # and the reverse
    ("Napoleon I.", "NAPOLEON I"),                           # sentence stop
    ("George I.", "GEORGE I"),
    ("Constitution of Athens", "“CONSTITUTION OF ATHENS”"),  # filed in quotes
    ("Sea Power", "SEA-POWER"),                              # hyphen
    ("Aemilia, Via", "AEMILIA VIA"),                         # comma
])
def test_a_reference_finds_the_title_it_spells_differently(reference, filed):
    assert N(reference) == N(filed)


@pytest.mark.parametrize("a,b", [
    ("Europe#History", "Europe: History"),   # the two section forms
    ("Ægean", "Aegean"),
    ("w:Napoleon", "Napoleon"),              # interwiki prefix
])
def test_the_older_canonicalizations_still_hold(a, b):
    assert N(a) == N(b)


@pytest.mark.parametrize("a,b", [
    ("Bath", "Bathe"),
    ("Mark", "Mark, Gospel of St"),
    ("Dragon", "Draco"),
    ("Cope", "Cope, Edward Drinker"),
])
def test_it_does_not_merge_distinct_names(a, b):
    assert N(a) != N(b)


def test_the_key_is_idempotent():
    """Applying it twice changes nothing — the fold is a canonical form, not a
    pass that keeps eating."""
    for s in ["O’Neill", "Napoleon I.", "“Constitution of Athens”",
              "Europe#History", "Sea-Power"]:
        assert N(N(s)) == N(s)


def test_a_section_key_keeps_its_colon():
    """`ARTICLE: SECTION` is the shape `extract_xrefs` files; folding commas and
    hyphens must not disturb the separator that carries the section."""
    assert N("Egypt#Ancient Egypt") == "EGYPT: ANCIENT EGYPT"
