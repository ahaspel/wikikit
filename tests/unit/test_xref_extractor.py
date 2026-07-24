from britannica.xrefs.extractor import extract_xrefs


# --- Existing See / See also patterns ---


def test_extract_xrefs_finds_explicit_see_reference() -> None:
    text = "For the related topic, See ABANDONMENT."

    results = extract_xrefs(text)

    assert len(results) == 1
    assert results[0]["xref_type"] == "see"
    assert results[0]["surface_text"] == "See ABANDONMENT"
    assert results[0]["normalized_target"] == "ABANDONMENT"


def test_extract_xrefs_finds_see_also_reference() -> None:
    text = "See also ABACUS."

    results = extract_xrefs(text)

    assert len(results) == 1
    assert results[0]["xref_type"] == "see_also"
    assert results[0]["surface_text"] == "See also ABACUS"
    assert results[0]["normalized_target"] == "ABACUS"


# --- q.v.: producer-stamped windows (J7 slice 1) ---
#
# Prose q.v. recognition moved OUT of this extractor into the body producer:
# `_stamp_qv_windows` stamps the TOTAL clause window as `«LN[w]:…»` (an
# UNASSERTED extent), the extractor reads the marker off the stream with a
# `window` flag, and the RESOLVER picks the extent by suffix cuts against the
# title index (GEBER out of "celebrated in Latin alchemy as Geber").  These
# tests pin the stamp+extract handoff; extent selection is resolver-tested.


def _stamped(text: str):
    from britannica.pipeline.stages.elements import _stamp_qv_windows
    return extract_xrefs(_stamp_qv_windows(text))


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
    from britannica.pipeline.stages.elements import _stamp_qv_windows
    text = "see «LN:Geber|Geber«/LN» (q.v.) for details"
    assert _stamp_qv_windows(text) == text
    results = extract_xrefs(_stamp_qv_windows(text))
    assert len(results) == 1
    assert results[0]["xref_type"] == "link"
    assert results[0].get("window") is None


# --- (See X) parenthesized pattern ---


def test_extract_xrefs_finds_paren_see() -> None:
    text = "(See Mechanics and Hodograph.)"

    results = extract_xrefs(text)

    assert len(results) == 2
    targets = {r["normalized_target"] for r in results}
    assert targets == {"MECHANICS", "HODOGRAPH"}


def test_extract_xrefs_finds_paren_see_also() -> None:
    text = "(See also Electricity.)"

    results = extract_xrefs(text)

    assert len(results) == 1
    assert results[0]["xref_type"] == "see_also"
    assert results[0]["normalized_target"] == "ELECTRICITY"


def test_extract_xrefs_finds_paren_see_single_target() -> None:
    text = "(See Arabian Philosophy.)"

    results = extract_xrefs(text)

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
