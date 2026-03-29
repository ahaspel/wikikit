"""Fuzzy matching strategies for cross-reference resolution."""

import re


def find_fuzzy_match(target: str, title_map: dict[str, int]) -> int | None:
    """Try fuzzy matching strategies to resolve a target against known titles.

    Args:
        target: Normalized (uppercased) target string
        title_map: Dict mapping uppercased title -> article id

    Returns:
        article id if a match is found, None otherwise
    """
    # Strategy 1: Plural/singular normalization
    result = _try_plural_singular(target, title_map)
    if result is not None:
        return result

    # Strategy 2: Name inversion (FIRST LAST -> LAST, FIRST)
    result = _try_name_inversion(target, title_map)
    if result is not None:
        return result

    return None


def _try_plural_singular(target: str, title_map: dict[str, int]) -> int | None:
    """Try adding/removing plural suffixes."""
    # target is plural, article is singular
    if target.endswith("IES"):
        singular = target[:-3] + "Y"
        if singular in title_map:
            return title_map[singular]
    if target.endswith("ES"):
        singular = target[:-2]
        if singular in title_map:
            return title_map[singular]
    if target.endswith("S") and not target.endswith("SS"):
        singular = target[:-1]
        if singular in title_map:
            return title_map[singular]

    # target is singular, article is plural
    if target + "S" in title_map:
        return title_map[target + "S"]
    if target + "ES" in title_map:
        return title_map[target + "ES"]
    if target.endswith("Y"):
        plural = target[:-1] + "IES"
        if plural in title_map:
            return title_map[plural]

    return None


def _try_name_inversion(target: str, title_map: dict[str, int]) -> int | None:
    """Try inverting 'FIRST LAST' to 'LAST, FIRST' and vice versa."""
    words = target.split()
    if len(words) == 2:
        # FIRST LAST -> LAST, FIRST
        inverted = f"{words[1]}, {words[0]}"
        if inverted in title_map:
            return title_map[inverted]

    if ", " in target:
        # LAST, FIRST -> FIRST LAST
        parts = target.split(", ", 1)
        if len(parts) == 2:
            inverted = f"{parts[1]} {parts[0]}"
            if inverted in title_map:
                return title_map[inverted]

    return None
