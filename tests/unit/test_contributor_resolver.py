from britannica.contributors.resolver import ContributorResolver


NAMES = [
    "Donald Francis Tovey",
    "Rev. Duncan Crookes Tovey",
    "Miss Kathleen Schlesinger",
    "John Gray McKendrick",
    "William Henry Hadow",
    "John Alexander Fuller Maitland",
    "Frederick William Maitland",
    "Cleveland Abbe",
    "Sir Archibald Geikie",
    "George E. Woodberry",
]


def test_exact_match_ignoring_honorific() -> None:
    r = ContributorResolver(NAMES)
    # "Kathleen Schlesinger" matches "Miss Kathleen Schlesinger"
    assert r.resolve("Kathleen Schlesinger") == "Miss Kathleen Schlesinger"
    # With Miss included: still matches
    assert r.resolve("Miss Kathleen Schlesinger") == "Miss Kathleen Schlesinger"
    # Title-prefixed input matches title-prefixed canonical
    assert r.resolve("Sir Archibald Geikie") == "Sir Archibald Geikie"
    assert r.resolve("Archibald Geikie") == "Sir Archibald Geikie"


def test_first_initial_last_disambiguates_shared_surname() -> None:
    r = ContributorResolver(NAMES)
    # Two Toveys; input's first name pins the composer
    assert r.resolve("Donald F. Tovey") == "Donald Francis Tovey"
    assert r.resolve("Donald Tovey") == "Donald Francis Tovey"
    assert r.resolve("Duncan Tovey") == "Rev. Duncan Crookes Tovey"


def test_two_maitlands_disambiguated_by_first_initial() -> None:
    r = ContributorResolver(NAMES)
    assert r.resolve("J. A. Fuller Maitland") == "John Alexander Fuller Maitland"
    assert r.resolve("John Maitland") == "John Alexander Fuller Maitland"
    assert r.resolve("Frederick Maitland") == "Frederick William Maitland"
    assert r.resolve("F. W. Maitland") == "Frederick William Maitland"


def test_ambiguous_lastname_with_no_first_signal_returns_none() -> None:
    r = ContributorResolver(NAMES)
    # Surname alone, two candidates -> no match
    assert r.resolve("Tovey") is None
    assert r.resolve("Maitland") is None


def test_unique_lastname_resolves_from_bare_surname() -> None:
    r = ContributorResolver(NAMES)
    assert r.resolve("McKendrick") == "John Gray McKendrick"
    assert r.resolve("Hadow") == "William Henry Hadow"


def test_honorific_stripped_from_input() -> None:
    r = ContributorResolver(NAMES)
    assert r.resolve("Prof. McKendrick") == "John Gray McKendrick"
    assert r.resolve("Dr. John Gray McKendrick") == "John Gray McKendrick"


def test_initials_are_interchangeable_with_spelled_out() -> None:
    r = ContributorResolver(NAMES)
    # "D. F. Tovey" — first-initial "D" matches both Toveys, but
    # middle initial "F" matches Francis uniquely.
    assert r.resolve("D. F. Tovey") == "Donald Francis Tovey"
    # Bare "D. Tovey" is genuinely ambiguous (both Toveys start with D)
    # — the resolver returns None rather than guess.
    assert r.resolve("D. Tovey") is None


def test_empty_or_unknown_returns_none() -> None:
    r = ContributorResolver(NAMES)
    assert r.resolve("") is None
    assert r.resolve("   ") is None
    assert r.resolve("Totally Unknown Person") is None


def test_honorific_only_returns_none() -> None:
    r = ContributorResolver(NAMES)
    assert r.resolve("Prof.") is None
    assert r.resolve("Miss") is None
