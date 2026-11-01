from wikikit.xrefs.extractor import extract_xrefs


# --- See / See also: producer-stamped windows (J7 slice 2) ---
#
# See recognition moved OUT of this extractor into the body producer: the cue
# token stamps a forward, clause-bounded window as `«LN[see]:…»` /
# `«LN[see_also]:…»` (capital-led, segment-split on `,`/` and `), and the
# resolver's abstain-default tier (`resolve_see`, span cuts + topic gate)
# decides what binds.  The extractor just reads the markers.


def test_see_window_is_stamped_and_extracted() -> None:
    results = _stamped("For the related topic, See ABANDONMENT.")

    assert len(results) == 1
    assert results[0]["xref_type"] == "see"
    assert results[0].get("window") is True
    assert results[0]["normalized_target"] == "ABANDONMENT"


def test_see_also_window() -> None:
    results = _stamped("See also ABACUS.")

    assert len(results) == 1
    assert results[0]["xref_type"] == "see_also"
    assert results[0]["normalized_target"] == "ABACUS"


def test_lowercase_window_not_stamped() -> None:
    """The verb `see` in running prose points at lowercase clauses — the one
    lexical gate the old _TARGET_TAIL armor is reduced to."""
    assert _stamped("we see that the reader may confirm this") == []


def test_see_article_lead_is_skipped() -> None:
    results = _stamped("see the article on Ships.")

    assert len(results) == 1
    assert results[0]["normalized_target"] == "SHIPS"


def test_cited_work_in_italics_gets_no_stamp() -> None:
    """«I» is a window boundary, so a cited (italicized) work yields an empty
    window — the old _is_bibliographic gate for free."""
    assert _stamped("See «I»Geschichte der Mathematik«/I», 2nd Bd.") == []


def test_a_cited_book_or_author_gets_no_stamp() -> None:
    """A name a chapter or section number follows is a citation: Herodotus,
    not HEROD; the Gospel, not Pope JOHN VII."""
    assert _stamped("in 480 (cf. Herod. vi. 49 with 94).") == []
    assert _stamped("a quotation (cf. John vii. 20, viii. 48).") == []
    assert _stamped("see Fig. 3, t, on the plate") == []


def test_a_name_ending_its_sentence_is_still_stamped() -> None:
    results = _stamped("For this kingdom see Ethiopia. J. T. Bent conjectured")

    assert [r["normalized_target"] for r in results] == ["ETHIOPIA"]


def test_only_the_cited_segment_of_an_enumeration_goes() -> None:
    results = _stamped("see Mechanics and Herod. vii. 61")

    assert [r["normalized_target"] for r in results] == ["MECHANICS"]


# --- q.v.: producer-stamped windows (J7 slice 1) ---
#
# Prose q.v. recognition moved OUT of this extractor into the body producer:
# `_stamp_qv_windows` stamps the TOTAL clause window as `«LN[w]:…»` (an
# UNASSERTED extent), the extractor reads the marker off the stream with a
# `window` flag, and the RESOLVER picks the extent by suffix cuts against the
# title index (GEBER out of "celebrated in Latin alchemy as Geber").  These
# tests pin the stamp+extract handoff; extent selection is resolver-tested.


def _stamped(text: str):
    """Producer stamps (q.v. then see, the _produce_body order) → extract."""
    from wikikit.pipeline.stages.elements import (
        _stamp_qv_windows, _stamp_see_windows)
    return extract_xrefs(_stamp_see_windows(_stamp_qv_windows(text)))


def test_qv_window_is_stamped_and_extracted() -> None:
    results = _stamped("a finely granular variety of gypsum (q.v.).")

    assert len(results) == 1
    assert results[0]["xref_type"] == "link"
    assert results[0].get("window") is True
    assert results[0]["normalized_target"] == \
        "A FINELY GRANULAR VARIETY OF GYPSUM"


def test_qv_window_stops_at_clause_boundary() -> None:
    results = _stamped("came to Macedonia; in 343 Aristotle (q.v.) taught.")

    assert len(results) == 1
    assert results[0]["normalized_target"] == "IN 343 ARISTOTLE"


def test_qv_window_total_not_trimmed() -> None:
    """The stamp is DUMB: the whole clause rides; the resolver cuts."""
    results = _stamped("celebrated in Latin alchemy as Geber (q.v.).")

    assert len(results) == 1
    assert results[0].get("window") is True
    assert results[0]["normalized_target"] == \
        "CELEBRATED IN LATIN ALCHEMY AS GEBER"


def test_multiple_qv_windows() -> None:
    results = _stamped("Aristotle (q.v.) and Plato (q.v.) both wrote on this.")

    assert len(results) == 2
    targets = {r["normalized_target"] for r in results}
    assert targets == {"ARISTOTLE", "AND PLATO"}


def test_qv_after_link_gets_no_stamp() -> None:
    """A linked reference is already asserted — the cue is just prose."""
    from wikikit.pipeline.stages.elements import _stamp_qv_windows
    text = "see «LN:Geber|Geber«/LN» (q.v.) for details"
    assert _stamp_qv_windows(text) == text
    results = extract_xrefs(_stamp_qv_windows(text))
    assert len(results) == 1
    assert results[0]["xref_type"] == "link"
    assert results[0].get("window") is None


# --- (See X) parenthesized pattern ---


def test_extract_xrefs_finds_paren_see() -> None:
    """An enumerated window splits into segments — each its own stamp."""
    results = _stamped("(See Mechanics and Hodograph.)")

    assert len(results) == 2
    targets = {r["normalized_target"] for r in results}
    assert targets == {"MECHANICS", "HODOGRAPH"}


def test_extract_xrefs_finds_paren_see_also() -> None:
    results = _stamped("(See also Electricity.)")

    assert len(results) == 1
    assert results[0]["xref_type"] == "see_also"
    assert results[0]["normalized_target"] == "ELECTRICITY"


def test_extract_xrefs_finds_paren_see_single_target() -> None:
    results = _stamped("(See Arabian Philosophy.)")

    assert len(results) == 1
    assert results[0]["normalized_target"] == "ARABIAN PHILOSOPHY"


# --- Deduplication ---


def test_extract_xrefs_deduplicates_same_target() -> None:
    """Identical windows dedup at extraction; DIFFERENT windows that would cut
    to the same title ("Aristotle" / "Later Aristotle") stay separate records —
    same-target collapse happens at resolution (the panel dedups by target)."""
    results = _stamped(
        "Aristotle (q.v.) wrote this. Later Aristotle (q.v.) expanded it.")
    assert {r["normalized_target"] for r in results} == \
        {"ARISTOTLE", "LATER ARISTOTLE"}

    results = _stamped("Aristotle (q.v.) wrote. Aristotle (q.v.) expanded.")
    assert len(results) == 1
    assert results[0]["normalized_target"] == "ARISTOTLE"


# --- See + a name in small capitals: the small-caps producer stamps it ---
#
# The book prints a cross-reference's name in small capitals ("see
# {{sc|Hydromechanics}}"); the walker produces it as its own element, so the
# body producer's window never meets it.  The small-caps producer reads the cue
# off its left neighbour (and, for an enumeration's later names, the sibling
# before) and stamps the name inside the small capitals.


def _chain(wikitext: str) -> str:
    from wikikit.db.models import SourcePage
    from wikikit.pipeline.stages.elements import ElementContext, process_elements
    from wikikit.pipeline.stages.preprocess import stream_with_keys
    stream, _, _ = stream_with_keys([SourcePage(volume=1, page_number=1, wikitext=wikitext)], 1)
    return process_elements(stream, ElementContext(volume=1))


def test_see_with_a_small_caps_name_is_stamped_inside_it() -> None:
    out = _chain("the equation of continuity (see {{sc|Hydromechanics}}) is")
    assert "(see «SC»«LN[see]:Hydromechanics|Hydromechanics«/LN»«/SC»)" in out
    assert [(x["xref_type"], x["normalized_target"]) for x in extract_xrefs(out)] == \
        [("see", "HYDROMECHANICS")]


def test_see_the_article_and_an_enumeration_of_small_caps_names() -> None:
    out = _chain("See the article {{sc|Harmonic Analysis}}; see {{sc|Gyroscope}} and {{sc|Gyrostat}}.")
    assert {x["normalized_target"] for x in extract_xrefs(out)} == \
        {"HARMONIC ANALYSIS", "GYROSCOPE", "GYROSTAT"}


def test_a_small_caps_name_the_source_linked_is_left_as_it_made_it() -> None:
    out = _chain("see {{sc|Light}}, and {{sc|{{1911link|Electric Waves}}}}.")
    assert "«SC»«LN:Electric Waves|Electric Waves«/LN»«/SC»" in out
    assert {(x["xref_type"], x["normalized_target"]) for x in extract_xrefs(out)} == \
        {("see", "LIGHT"), ("link", "ELECTRIC WAVES")}


def test_small_caps_without_a_cue_is_not_a_reference() -> None:
    assert extract_xrefs(_chain("in {{sc|Rome}} and {{sc|Paris}} alike")) == []


# The book points at an article with more words than "see" — and the small
# capitals are still its mark (the links Wikisource's editors put on the
# supplied pages: 42 behind such cues, 2026-10-11).

def test_the_article_and_treated_under_cue_a_small_caps_name() -> None:
    out = _chain("is given in the article {{sc|Projection}}, and the subject is treated "
                 "under {{sc|Element}}; compare (q.v. under {{sc|Hippopotamus}}).")
    assert {x["normalized_target"] for x in extract_xrefs(out)} == \
        {"PROJECTION", "ELEMENT", "HIPPOPOTAMUS"}


def test_a_semicolon_continues_an_enumeration() -> None:
    out = _chain("(see {{sc|Artiodactyla}}; {{sc|Hippopotamus}}; {{sc|Peccary}}).")
    assert {x["normalized_target"] for x in extract_xrefs(out)} == \
        {"ARTIODACTYLA", "HIPPOPOTAMUS", "PECCARY"}


# The heading the book prints after the name names a SECTION of that article:
# "see MEXICO: History".  It is stamped as a section of the name before it.


def test_a_printed_heading_after_a_name_is_a_section_of_it() -> None:
    out = _chain("the Liberals (see {{sc|Mexico}}: ''History''). General")
    assert "«I»«LN[sec]:Mexico#History|History«/LN»«/I»" in out
    assert [(x["xref_type"], x["normalized_target"]) for x in extract_xrefs(out)] == \
        [("see", "MEXICO"), ("section", "MEXICO: HISTORY")]


def test_a_heading_after_a_plain_name_or_a_source_link() -> None:
    assert "«LN[sec]:Mexico#History|History«/LN»" in _chain("(see Mexico: ''History'')")
    assert "«LN[sec]:Ethiopia#The Axumite Kingdom|The Axumite Kingdom«/LN»" in \
        _chain("(see {{1911link|Ethiopia}}, ''The Axumite Kingdom''.)")


def test_a_name_linked_to_its_section_already_is_passed() -> None:
    out = _chain("(see {{1911link|Spain#History|Spain}}, ''History'')")
    assert "LN[sec]" not in out


def test_the_new_cues_are_small_caps_only() -> None:
    # in plain text "the article" is prose, not a pointer
    assert extract_xrefs(_chain("The article Projection was printed in 1910.")) == []
