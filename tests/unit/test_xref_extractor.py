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


# --- q.v. pattern ---


def test_extract_xrefs_finds_qv_single_word() -> None:
    text = "a finely granular variety of gypsum (q.v.)."

    results = extract_xrefs(text)

    assert len(results) == 1
    assert results[0]["xref_type"] == "qv"
    assert results[0]["normalized_target"] == "GYPSUM"


def test_extract_xrefs_finds_qv_capitalized_word() -> None:
    text = "in 343 Aristotle (q.v.) came to Macedonia."

    results = extract_xrefs(text)

    assert len(results) == 1
    assert results[0]["xref_type"] == "qv"
    assert results[0]["normalized_target"] == "ARISTOTLE"


def test_extract_xrefs_finds_qv_multi_word_proper_noun() -> None:
    text = "the Aleutian Islands (q.v.) lie to the west."

    results = extract_xrefs(text)

    assert len(results) == 1
    assert results[0]["normalized_target"] == "ALEUTIAN ISLANDS"


def test_extract_xrefs_finds_qv_does_not_overcapture() -> None:
    text = "celebrated in Latin alchemy as Geber (q.v.)."

    results = extract_xrefs(text)

    assert len(results) == 1
    assert results[0]["normalized_target"] == "GEBER"


def test_extract_xrefs_finds_multiple_qv() -> None:
    text = "Aristotle (q.v.) and Plato (q.v.) both wrote on this."

    results = extract_xrefs(text)

    assert len(results) == 2
    targets = {r["normalized_target"] for r in results}
    assert targets == {"ARISTOTLE", "PLATO"}


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
    text = "Aristotle (q.v.) wrote this. Later Aristotle (q.v.) expanded it."

    results = extract_xrefs(text)

    assert len(results) == 1
    assert results[0]["normalized_target"] == "ARISTOTLE"
