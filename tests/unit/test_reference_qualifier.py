"""The source's own disambiguator — `«LN:Down (hill)|Down«/LN»`.

Wikisource targets carry a TRAILING parenthetical naming which sense is meant
("Down (hill)", "Order (architecture)", "Alexandria (Egypt)").  The fill's
subset rung matches every same-titled article equally, so the qualifier was
used to PERMIT a match and never to CHOOSE one: `Down (hill)` bound county
Down, `Alexandria (Egypt)` bound Alexandria in Rumania, `Basilica (building)`
bound the legal code.  A stated source attribute, dropped at the one moment it
decided anything ([[feedback_forks_are_dropped_attributes]]).

The qualifier is CARRIED to the picker, never used to trim the name the fill
searches on — recall stays dumb, all disambiguation stays in the fisher
([[feedback_fill_dumb_fish_smart]]).  Inside the fisher it is weighed ahead of
kind and embedding, because the author declaring the sense outranks any proxy:
`Down (hill)` sits in a footnote about a battle at Dunkirk, so the prose cosine
points confidently at the Irish county.
"""
from britannica.link_resolver import _qualifier_of
from britannica.topic_fisher import Fisher
from britannica.xrefs.disambiguation import lead_kind


class _EmbStub:
    """The qualifier must decide BEFORE the embedding rung; a stub that scores
    every candidate identically proves the pick came from the qualifier."""
    def embed_text(self, text):
        return None

    def cosine(self, fn, q):
        return 0.0

    def vector_of(self, fn):
        return None


def _fisher(openings):
    return Fisher(_EmbStub(), lambda fn: openings[fn])


def _fish(openings, name, qualifier):
    cands = [(fn, name.split(" (")[0]) for fn in openings]
    return _fisher(openings).fish(name, cands, [], lead_kind(qualifier),
                                  prose="", qualifier=qualifier)


# ── recognition: TRAILING only ────────────────────────────────────────────
def test_trailing_parenthetical_is_the_qualifier():
    assert _qualifier_of("Down (hill)") == "hill"
    assert _qualifier_of("Alexandria (Egypt)") == "Egypt"
    assert _qualifier_of("Baldwin I. (emperor of Romania)") == "emperor of Romania"


def test_mid_string_parenthetical_is_not_a_qualifier():
    """`Bible (King James)/1 John#3:12` is a path form — its paren names no
    sense.  Read as a qualifier it matched "king of Poland" and rebound a
    scripture citation to John III Sobieski."""
    assert _qualifier_of("Bible (King James)/1 John#3:12") is None
    assert _qualifier_of("Down") is None
    assert _qualifier_of("Empty ()") is None


# ── picking ───────────────────────────────────────────────────────────────
def test_literal_qualifier_picks_the_lead_that_states_it():
    fn, _title, method = _fish({
        "county.json": "a maritime county of Ireland, in the province of Ulster",
        "hill.json": "a smooth rounded hill, or more particularly an expanse of "
                     "high rolling ground bare of trees",
    }, "Down (hill)", "hill")
    assert (fn, method) == ("hill.json", "qualifier")


def test_kind_qualifier_uses_the_first_is_a_not_a_whole_lead_grep():
    """`(canton)` names a KIND, so it is tested against each lead's FIRST is-a.
    Both leads contain the word "canton", so a whole-lead grep ties — but the
    city's opening is-a is "capital" and only the canton's is "canton"."""
    fn, _title, method = _fish({
        "city.json": "the capital of the canton of Zurich, on the Limmat",
        "canton.json": "one of the cantons of Switzerland, bounded on the north",
    }, "Zurich (canton)", "canton")
    assert (fn, method) == ("canton.json", "qualifier-kind")


def test_kind_abstention_falls_back_to_the_literal_test():
    """MAINE: both candidates are kind `division`, so the kind rung abstains —
    but only the US state's lead says "state"."""
    fn, _title, method = _fish({
        "province.json": "an old French province, bounded N. by Normandy, "
                         "E. by Orleanais, S. by Touraine and Anjou",
        "state.json": "a North Atlantic state of the United States of America, "
                      "the most north-easterly state in the Union",
    }, "Maine (state)", "state")
    assert (fn, method) == ("state.json", "qualifier")


# ── abstention: the rung may only ADD a decision ──────────────────────────
def test_abstains_when_no_lead_states_the_qualifier():
    fn, _title, method = _fish({
        "a.json": "a market-town and municipal borough in Suffolk",
        "b.json": "a township of Middlesex county, Massachusetts",
    }, "Somewhere (nonesuch)", "nonesuch")
    assert method != "qualifier"


def test_abstains_on_a_tie():
    """Both leads state it → the qualifier separates nothing, so it must hand
    off rather than pick arbitrarily."""
    fn, _title, method = _fish({
        "a.json": "a city and chief seaport of Egypt on the Mediterranean",
        "b.json": "a town of Egypt on the western bank of the Nile",
    }, "Somewhere (Egypt)", "Egypt")
    assert method != "qualifier"


def test_function_words_alone_never_decide():
    """A qualifier of only stopwords must not match every lead."""
    fn, _title, method = _fish({
        "a.json": "a city of the plain, of the older sort",
        "b.json": "a river of the north",
    }, "Somewhere (of the)", "of the")
    assert method != "qualifier"
