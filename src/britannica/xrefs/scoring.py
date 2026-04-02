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

    # Strategy 3: Trailing "THE" (UNITED STATES -> UNITED STATES, THE)
    result = _try_trailing_article(target, title_map)
    if result is not None:
        return result

    # Strategy 4: Trailing period (EDWARD VII. -> EDWARD VII)
    result = _try_trailing_period(target, title_map)
    if result is not None:
        return result

    # Strategy 5: Qualified title (CLIMATE -> CLIMATE AND CLIMATOLOGY)
    result = _try_prefix_match(target, title_map)
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


def _try_trailing_article(target: str, title_map: dict[str, int]) -> int | None:
    """Try adding/removing trailing ', THE'."""
    if target.endswith(", THE"):
        without = target[:-5]
        if without in title_map:
            return title_map[without]
    else:
        with_the = f"{target}, THE"
        if with_the in title_map:
            return title_map[with_the]
    return None


def _try_trailing_period(target: str, title_map: dict[str, int]) -> int | None:
    """Handle trailing periods in regnal numbers (EDWARD VII. -> EDWARD VII)."""
    if target.endswith("."):
        without = target.rstrip(".")
        if without in title_map:
            return title_map[without]
    else:
        with_period = target + "."
        if with_period in title_map:
            return title_map[with_period]
    return None


def _try_prefix_match(target: str, title_map: dict[str, int]) -> int | None:
    """Match target as a prefix of an article title.

    E.g. CLIMATE -> CLIMATE AND CLIMATOLOGY, GROUPS -> GROUPS, THEORY OF.
    Only matches if the target is followed by a comma, space+AND, space+SYSTEM,
    or similar qualifier — not arbitrary substrings.
    """
    if len(target) < 4:
        return None
    candidates = []
    for title, aid in title_map.items():
        if not title.startswith(target) or title == target:
            continue
        rest = title[len(target):]
        # Must be followed by a word boundary: comma, space, or hyphen
        if rest[0] in (",", " ", "-"):
            candidates.append((title, aid))
    if len(candidates) == 1:
        # Unambiguous prefix match
        return candidates[0][1]
    if candidates:
        # Multiple matches — pick shortest (most likely the right one)
        candidates.sort(key=lambda x: len(x[0]))
        return candidates[0][1]
    return None
