"""The FILL substrate's rungs — the shared recall engine under every resolver.

These lock the BEHAVIOUR each rung exists for, in the terms the design states it
(docs/xref_resolution_strategy.md, [[feedback_fill_dumb_fish_smart]]): recall is
dumb and broad, each rung is strictly looser than the last, and NOTHING here
picks — a rung returns a bag.
"""
from britannica.name_index import NameIndex, content, fold, wordset, wordset_f


def _idx(*titles):
    return NameIndex([{"filename": f"{i:02d}.json", "title": t}
                      for i, t in enumerate(titles)])


def _titles(bag):
    return sorted(t for _fn, t in bag)


# ── tokenizers ───────────────────────────────────────────────────────────
def test_wordset_is_order_independent():
    assert wordset("Descartes, René") == wordset("René Descartes")


def test_fold_strips_diacritics():
    assert fold("ZÜRICH") == "ZURICH"
    assert wordset_f("Poincaré") == frozenset({"POINCARE"})


def test_content_drops_furniture_and_initials():
    # honorifics / structural words / single-letter initials are never identity
    assert content(wordset("Fletcher, Alice C.")) == frozenset({"FLETCHER", "ALICE"})
    assert content(wordset("Smith, Sir John")) == frozenset({"SMITH", "JOHN"})


# ── exact: the tight rung ────────────────────────────────────────────────
def test_exact_matches_regardless_of_name_order():
    idx = _idx("DESCARTES, RENÉ")
    assert _titles(idx.exact("René Descartes")) == ["DESCARTES, RENÉ"]


def test_exact_is_diacritic_SENSITIVE_fold_is_not():
    idx = _idx("ZÜRICH")
    assert idx.exact("Zurich") == []          # tight rung keeps the accent
    assert _titles(idx.fold_match("Zurich")) == ["ZÜRICH"]


def test_exact_returns_every_colliding_title():
    idx = _idx("ROME", "ROME", "ROME")
    assert len(idx.exact("Rome")) == 3        # a bag, never a pick


# ── subset: name ⊂ title ─────────────────────────────────────────────────
def test_subset_recovers_the_forward_personal_name():
    # the big declared-miss pool: 'Richard Francis Burton' filed as an inversion
    idx = _idx("BURTON, SIR RICHARD FRANCIS")
    assert _titles(idx.subset("Richard Francis Burton")) == ["BURTON, SIR RICHARD FRANCIS"]


def test_subset_ignores_initials_when_containing():
    idx = _idx("FLETCHER, ALICE CUNNINGHAM")
    assert _titles(idx.subset("Fletcher, Alice C.")) == ["FLETCHER, ALICE CUNNINGHAM"]


def test_subset_requires_every_content_word():
    idx = _idx("BURTON, SIR RICHARD FRANCIS")
    assert idx.subset("Richard Francis Burton Smith") == []


# ── superset: title ⊂ name (reverse), guarded ────────────────────────────
def test_superset_picks_the_longest_contained_title():
    idx = _idx("INDEPENDENCE, DECLARATION OF", "INDEPENDENCE")
    bag = idx.superset("United States Declaration of Independence")
    assert _titles(bag) == ["INDEPENDENCE, DECLARATION OF"]     # most specific wins


def test_superset_refuses_a_single_content_word():
    """The component-noise guard: BATTLE ⊂ 'Saratoga, Battles of' is a part of
    the reference, not its subject — those recover via firstword instead."""
    idx = _idx("BATTLE")
    assert idx.superset("Saratoga, Battles of") == []


def test_superset_binds_on_two_content_words():
    idx = _idx("COURT MANUSCRIPT")
    assert _titles(idx.superset("Queen's Court Manuscript")) == ["COURT MANUSCRIPT"]


# ── firstword: the loose recall rung ─────────────────────────────────────
def test_firstword_returns_every_title_carrying_the_first_word():
    idx = _idx("ZÜRICH", "ZÜRICH, LAKE OF", "BASEL")
    assert _titles(idx.firstword("Zürich")) == ["ZÜRICH", "ZÜRICH, LAKE OF"]


def test_firstword_folds_diacritics():
    idx = _idx("ZÜRICH, LAKE OF")
    assert len(idx.firstword("Zurich")) == 1


# ── aliases: recall only, never shadowing a real title ───────────────────
def test_alias_adds_recall():
    idx = _idx("MAHOMMEDAN RELIGION")
    idx.add_alias("Moslem Religion", "00.json")
    assert _titles(idx.exact("Moslem Religion")) == ["MAHOMMEDAN RELIGION"]


def test_alias_never_shadows_an_existing_title():
    idx = _idx("BATH", "BATHS")
    before = idx.exact("Bath")
    idx.add_alias("Bath", "01.json")          # collides with a real title
    assert idx.exact("Bath") == before        # the title keeps the key
