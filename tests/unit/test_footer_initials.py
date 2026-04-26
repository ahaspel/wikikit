from britannica.pipeline.stages.extract_contributors import (
    _clean_footer_initials,
)


def test_simple_initials_pass_through() -> None:
    assert _clean_footer_initials("J. S. F.") == ["J. S. F."]


def test_compound_entry_splits_on_semicolon() -> None:
    assert _clean_footer_initials("E. H. P.; X.") == ["E. H. P."]


def test_trailing_period_added_when_missing() -> None:
    # AGRARIAN LAWS: source has `A. H. J. G}}` (trailing period typo)
    # but the DB stores initials as "A. H. J. G." — the normaliser
    # restores the period so the lookup finds the contributor.
    assert _clean_footer_initials("A. H. J. G") == ["A. H. J. G."]


def test_trailing_period_preserved_when_present() -> None:
    assert _clean_footer_initials("A. H. J. G.") == ["A. H. J. G."]


def test_trailing_asterisk_not_modified() -> None:
    # Some initials end with "*" (disambiguator) — don't append a period.
    assert _clean_footer_initials("J. W.*") == ["J. W.*"]


def test_anonymous_markers_dropped() -> None:
    assert _clean_footer_initials("X.") == []
    assert _clean_footer_initials("X") == []


def test_compound_with_period_typo() -> None:
    assert _clean_footer_initials("A. B.; C. D") == ["A. B.", "C. D."]
