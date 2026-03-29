from britannica.xrefs.scoring import find_fuzzy_match


def _make_titles(*titles: str) -> dict[str, int]:
    return {t.upper(): i for i, t in enumerate(titles, 1)}


# --- Plural/singular ---


def test_plural_target_matches_singular_article() -> None:
    titles = _make_titles("ABBREVIATION", "AQUEDUCT")
    assert find_fuzzy_match("ABBREVIATIONS", titles) is not None
    assert find_fuzzy_match("AQUEDUCTS", titles) is not None


def test_singular_target_matches_plural_article() -> None:
    titles = _make_titles("ANTINOMIANS")
    assert find_fuzzy_match("ANTINOMIAN", titles) is not None


def test_ies_plural_matches_y_singular() -> None:
    titles = _make_titles("BATTERY")
    assert find_fuzzy_match("BATTERIES", titles) is not None


def test_y_singular_matches_ies_plural() -> None:
    titles = _make_titles("BATTERIES")
    assert find_fuzzy_match("BATTERY", titles) is not None


def test_es_plural_matches_singular() -> None:
    titles = _make_titles("AMPHITHEATRE")
    assert find_fuzzy_match("AMPHITHEATRES", titles) is not None


# --- Name inversion ---


def test_first_last_matches_last_comma_first() -> None:
    titles = _make_titles("AGRICOLA, JOHANNES")
    assert find_fuzzy_match("JOHANNES AGRICOLA", titles) is not None


def test_last_comma_first_matches_first_last() -> None:
    titles = _make_titles("JOHANNES AGRICOLA")
    assert find_fuzzy_match("AGRICOLA, JOHANNES", titles) is not None


# --- No false positives ---


def test_no_match_returns_none() -> None:
    titles = _make_titles("ABACUS", "ABANDON")
    assert find_fuzzy_match("ARISTOTLE", titles) is None


def test_exact_match_not_duplicated() -> None:
    """Fuzzy matching should not match exact titles (those are handled separately)."""
    titles = _make_titles("ABACUS")
    assert find_fuzzy_match("ABACUS", titles) is None
