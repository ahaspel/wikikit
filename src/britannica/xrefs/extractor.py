import re

from britannica.xrefs.normalizer import normalize_xref_target


# Target (q.v.) — the dominant cross-reference pattern.
# Captures up to 6 words before (q.v.), but not across sentence/clause boundaries.
_QV_PATTERN = re.compile(r"([\w][\w\-]*(?:\s+[\w][\w\-]*){0,5})\s*\(q\.v\.\)")

# Link-marker variant: «LN:target|display«/LN» (q.v.)
_QV_LINK_PATTERN = re.compile(r"\u00abLN:([^|]*)\|[^«]*\u00ab/LN\u00bb\s*\(q\.v\.\)")


_SENTENCE_STARTERS = frozenset({
    "A", "An", "And", "As", "At", "But", "By", "For", "From", "He", "Her",
    "Here", "His", "How", "If", "In", "Into", "Is", "It", "Its", "Later",
    "Many", "Most", "No", "Not", "Of", "On", "One", "Or", "She", "So",
    "Some", "Such", "That", "The", "Their", "Then", "There", "These",
    "They", "This", "Those", "Through", "To", "Under", "Was", "Were",
    "What", "When", "Where", "Which", "While", "Who", "With",
})


def _extract_qv_target(raw_match: str) -> str:
    """Extract the actual reference target from text preceding (q.v.).

    Works backwards from the end to find the proper-noun or term:
    - "Aleutian Islands" -> "Aleutian Islands"
    - "celebrated in Latin alchemy as Geber" -> "Geber"
    - "first oxidized to aldehydes" -> "aldehydes"
    - "Later Aristotle" -> "Aristotle"
    """
    words = raw_match.split()
    if not words:
        return raw_match

    # Start from the last word and extend backwards through capitalized words
    result = [words[-1]]
    for word in reversed(words[:-1]):
        if not word[0].isupper():
            break
        if word in _SENTENCE_STARTERS:
            break
        if result[0][0].islower():
            break
        result.insert(0, word)

    return " ".join(result)

# (See Target) and (See also Target) — parenthesized editorial references
_PAREN_SEE_ALSO_PATTERN = re.compile(
    r"\(See also\s+([^)]+)\)"
)
_PAREN_SEE_PATTERN = re.compile(
    r"\(See\s+(?!also\s)([^)]+)\)"
)

# See TARGET / See also TARGET — sentence-level references (all-caps)
_SEE_ALSO_PATTERN = re.compile(r"\bSee also ([A-Z][A-Z\s\-]+)\b")
_SEE_PATTERN = re.compile(r"\bSee ([A-Z][A-Z\s\-]+)\b")


def _clean_paren_see_target(raw: str) -> list[str]:
    """Split parenthesized See targets on 'and', strip trailing punctuation."""
    raw = raw.strip().rstrip(".")
    parts = re.split(r"\s+and\s+", raw)
    results = []
    for p in parts:
        cleaned = p.strip().rstrip(";,.:!?").strip()
        if cleaned and _is_plausible_target(cleaned):
            results.append(cleaned)
    return results


def _is_plausible_target(target: str) -> bool:
    """Reject targets that are clearly not article references."""
    if not target:
        return False
    # Reject single common words that result from broken markup or overcapture
    if target.lower() in (
        "a", "above", "also", "although", "an", "and", "at", "bel", "below",
        "but", "by", "dr", "emperor", "for", "founded", "further",
        "he", "her", "his", "in", "is", "it", "its",
        "not", "of", "on", "or", "s", "son", "spread",
        "the", "their", "these", "they", "this", "those", "to",
        "under", "was", "were", "which", "with",
    ):
        return False
    # Reject very short targets (1-2 chars) — almost always noise
    if len(target) <= 2:
        return False
    # Reject absurdly long targets (table content parsed as xrefs)
    if len(target) > 200:
        return False
    # Reject targets that start with common words (sentence fragments, not titles)
    if re.match(r"(?i)^(?:also|although|and|especially|for|further|particularly|separate|the)\b", target):
        return False
    # Reject bibliographic citations (contain numbers, volume refs, page refs)
    if re.search(r"\b(?:p\.|pp\.|vol\.|Ber\.|Journ\.|Proc\.|Hist\.|Dict\.|Biog\.|Gesch\.|Zeits\.|\d{4})", target):
        return False
    # Reject bibliographic-style references (author name + title)
    if re.search(r"'s\s+(Dict|Hist|Bibl|Life|Lives|Memoir)", target):
        return False
    # Reject targets with stray semicolons (broken markup)
    if ";" in target:
        return False
    return True


def extract_xrefs(text: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(surface: str, target: str, xref_type: str) -> None:
        normalized = normalize_xref_target(target)
        if not normalized:
            return
        key = (normalized, xref_type)
        if key in seen:
            return
        seen.add(key)
        results.append(
            {
                "surface_text": surface.strip(),
                "normalized_target": normalized,
                "xref_type": xref_type,
            }
        )

    # Link markers are implicit cross-references
    for m in re.finditer(r"\u00abLN:([^|]*)\|([^«]*)\u00ab/LN\u00bb", text):
        target = m.group(1).strip()
        if _is_plausible_target(target):
            _add(m.group(0), target, "link")

    # q.v. references — link-marker variant first (more precise)
    for m in _QV_LINK_PATTERN.finditer(text):
        target = m.group(1).strip()
        if _is_plausible_target(target):
            _add(m.group(0), target, "qv")

    # q.v. references — plain text variant
    for m in _QV_PATTERN.finditer(text):
        target = _extract_qv_target(m.group(1))
        if _is_plausible_target(target):
            _add(m.group(0), target, "qv")

    # (See also X) before (See X) to avoid double-matching
    for m in _PAREN_SEE_ALSO_PATTERN.finditer(text):
        for target in _clean_paren_see_target(m.group(1)):
            _add(m.group(0), target, "see_also")

    for m in _PAREN_SEE_PATTERN.finditer(text):
        for target in _clean_paren_see_target(m.group(1)):
            _add(m.group(0), target, "see")

    # Sentence-level See also / See (all-caps targets)
    for m in _SEE_ALSO_PATTERN.finditer(text):
        _add(m.group(0), m.group(1), "see_also")

    for m in _SEE_PATTERN.finditer(text):
        _add(m.group(0), m.group(1), "see")

    return results
