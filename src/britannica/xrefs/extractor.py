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

# (See Target) and (See also Target) — parenthesized editorial references.
# Inside the parentheses the content may itself contain parens because
# link markers like «LN:Leopold I. (emperor)|…«/LN» embed them. So the
# content matches either a complete link marker or any non-`)` char.
_PAREN_CONTENT = r"(?:\u00abLN:[^\u00ab]*\u00ab/LN\u00bb|[^)])+"
_PAREN_SEE_ALSO_PATTERN = re.compile(
    r"\(See also\s+(" + _PAREN_CONTENT + r")\)"
)
_PAREN_SEE_PATTERN = re.compile(
    r"\(See\s+(?!also\s)(" + _PAREN_CONTENT + r")\)"
)

# See TARGET / See also TARGET — sentence-level references (all-caps)
_SEE_ALSO_PATTERN = re.compile(r"\bSee also ([A-Z][A-Z\s\-]+)\b")
_SEE_PATTERN = re.compile(r"\bSee ([A-Z][A-Z\s\-]+)\b")

# Mixed-case sentence-level references.  The all-caps patterns above
# only catch ``See QUEBEC`` style; many EB1911 articles have
# mixed-case titles (``Babylonia and Assyria``, ``Roman Art``) and
# the references to them are ``See Roman Art`` or lowercase
# ``see Roman Art`` mid-sentence.  Coverage audit (2026-05-07,
# tools/diagnostics/xref_coverage_audit.py) found 1,300+ resolvable
# candidates across these patterns.
#
# Target shape: starts with ``[A-Z]``, then any letter/digit/comma/
# space/apostrophe/period/hyphen, bounded by punctuation or one of a
# small list of stop-conjunctions ("and"/"or"/"in"/"of"/...).  Length
# 4-80 to avoid grabbing single capitalized words ("see God") or
# whole paragraphs.  ``_is_plausible_target`` filters the residual
# junk; unresolvable targets just don't link.
_TARGET_TAIL = (
    r"([A-Z][A-Za-z][A-Za-z0-9 ,'.\-]{2,80}?)"
    r"(?=[,;.)\"“”‘’]|"
    r"\s+(?:and|or|in|of|on|by|at|to|from|with|for|under|"
    r"who|which|where|when|while|the)\b|$|\n)"
)

# ``see article X`` / ``see the article X`` / ``see the article on X``
# — the most distinctive form (68% precision in the audit).  Allow
# the leading ``See``/``see`` to be either case.
_SEE_ARTICLE_PATTERN = re.compile(
    r"\b[Ss]ee\s+(?:the\s+)?article\s+(?:on\s+)?" + _TARGET_TAIL
)

# Mixed-case ``See X`` — sentence-leading ``See`` followed by mixed-
# case target.  Runs after the all-caps version so dedup-by-target
# avoids double-extraction when both match the same target.
_SEE_MIXED_PATTERN = re.compile(r"\bSee\s+" + _TARGET_TAIL)

# Lowercase ``see X`` mid-sentence.  Biggest single source of
# unrealized links per the audit (781 resolvable).  More noise-prone
# than the capitalised form (78% of candidates don't resolve), but
# cheap because unresolvable ones are filtered at extraction by
# ``_is_plausible_target`` and at resolution by absence-of-title.
_SEE_LOWER_PATTERN = re.compile(r"(?<![\w.])see\s+" + _TARGET_TAIL)

# ``See also X`` mixed-case variant.
_SEE_ALSO_MIXED_PATTERN = re.compile(r"\bSee\s+also\s+" + _TARGET_TAIL)

# ``Cf. X`` / ``cf. X`` — Latin abbreviation for ``compare``.
_CF_PATTERN = re.compile(r"\b[Cc]f\.\s+" + _TARGET_TAIL)

# ``compare X`` — English equivalent of ``cf.``.  Low volume but high
# enough precision to be worth catching.
_COMPARE_PATTERN = re.compile(r"\b[Cc]ompare\s+" + _TARGET_TAIL)


def _strip_markers(text: str) -> str:
    """Remove internal markers (links, formatting) to get plain text."""
    # Replace «LN:...|...|display«/LN» or «LN:target|display«/LN» with display text
    text = re.sub(r"\u00abLN:(?:[^|]*\|)*([^«]*)\u00ab/LN\u00bb", r"\1", text)
    # Strip formatting markers: «B», «I», «SC», etc.
    text = re.sub(r"\u00ab/?[A-Z]+\u00bb", "", text)
    return text.strip()


_MARKER_PLACEHOLDER = "\x05MARKER\x05"


def _is_bibliographic(raw: str) -> bool:
    """True if the (See …) content is a bibliographic citation rather
    than a cross-reference to another Britannica article.

    Bibliographic citations contain italic book/journal titles, quoted
    paper titles, page/volume references, or "by Author" attributions.
    Cross-references are plain article names, possibly with § sections.
    """
    # Italic markers indicate a book or journal title.
    if "\u00abI\u00bb" in raw:
        return True
    # Quoted paper/chapter titles.
    if re.search(r'"[^"]{4,}"', raw):
        return True
    # Page, volume, or roman-numeral volume refs.
    if re.search(r"\b(?:p\.|pp\.|vol\.|chap\.)\s", raw):
        return True
    # Roman-numeral volume + page pattern: "xxii. p. 305"
    if re.search(r"\b[ivxlc]{2,}\.\s*p\.", raw, re.IGNORECASE):
        return True
    # "by Author" attribution.
    if re.search(r"\bby\s+[A-Z]", raw):
        return True
    return False


def _clean_paren_see_target(raw: str) -> list[str]:
    """Split parenthesized See content into individual xref targets.

    Link markers `«LN:target|display«/LN»` contribute their TARGET
    (preserving internal commas and periods, e.g. `Metternich-Winneburg,
    Clemens Wenzel Lothar` or `Napoleon I.`). Plain text between/around
    markers is split on commas, semicolons, and 'and'.

    Using the link target (not display) lets see-entries deduplicate
    against the same article referenced as a link elsewhere.
    """
    # Reject bibliographic citations before splitting — once split,
    # fragments like "American Timber Bridges" lose their citation
    # context and look like plausible article names.
    if _is_bibliographic(raw):
        return []

    results: list[str] = []

    # Pull link markers out intact — their target is the xref target.
    def _capture_marker(m: re.Match) -> str:
        results.append(m.group(1).strip())
        return _MARKER_PLACEHOLDER

    text = re.sub(
        r"\u00abLN:([^|]*)\|[^\u00ab]*\u00ab/LN\u00bb",
        _capture_marker,
        raw,
    )
    text = _strip_markers(text)
    text = text.strip().rstrip(".")

    # Remaining plain-text targets: split on commas, semicolons, 'and'.
    # Skip any piece that was adjacent to a link marker (fragments like
    # ": History" or "of Russia" attached to a link aren't xref targets
    # of their own — the link already captured the real target).
    for piece in re.split(r"[;,]|\s+and\s+", text):
        if _MARKER_PLACEHOLDER in piece:
            continue
        cleaned = piece.strip().strip(";,:!?").strip()
        if cleaned and _is_plausible_target(cleaned):
            results.append(cleaned.rstrip("."))

    # Filter results through plausibility check too (link targets).
    return [t for t in results if _is_plausible_target(t)]


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
    # Reject Wikisource cross-project / language-prefix targets:
    # ``:sv:Antiqvarisk Tidskrift för Sverige`` (Swedish Wikisource),
    # ``:de:...``, ``:fr:...``, etc.  These are inter-project links,
    # not EB1911 article references.
    if re.match(r"^\s*:[a-z]{2,3}:", target, re.IGNORECASE):
        return False
    # Reject targets ending in a single uppercase letter (with or
    # without trailing period): ``See Rev. E``, ``See Miss A``,
    # ``See Sir J`` — the trailing letter is a person's first
    # initial, the surface text was truncated mid-name by the
    # extractor's pattern.  Single uppercase letter is never a real
    # article title.
    if re.search(r"\b[A-Z]\.?\s*$", target) and len(target) <= 12:
        return False
    # Reject legal-citation residue: ``R.S.C., O. xliii.`` shape —
    # comma-separated all-caps initialism followed by a period+lower-
    # roman fragment.  These get extracted from cell-table tabular
    # citations that the loose ``See X`` pattern grabs.
    if re.search(r"^[A-Z]\.[A-Z]\.[A-Z]\.,?\s*[A-Z]?\.\s*[ivxlcdm]+\b",
                 target, re.IGNORECASE):
        return False
    return True


def extract_xrefs(text: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen_by_type: set[tuple[str, str]] = set()   # (normalized, type)
    seen_targets: set[str] = set()               # normalized only — dedupe
                                                 # "see" or "see_also" lists
                                                 # whose target already
                                                 # appeared as a link xref.

    def _add(surface: str, target: str, xref_type: str) -> None:
        normalized = normalize_xref_target(target)
        if not normalized:
            return
        # A see/see_also entry duplicating an existing link is redundant.
        if xref_type in ("see", "see_also") and normalized in seen_targets:
            return
        key = (normalized, xref_type)
        if key in seen_by_type:
            return
        seen_by_type.add(key)
        seen_targets.add(normalized)
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

    # «AL» is the surviving [[Author:…]] marker — 6b4 resolves the contributor
    # SIGNOFFS and leaves the rest for us.  Its target names a PERSON, not an
    # article title, so it is its own kind: the resolver matches a surname
    # against EB's surname-first titles instead of running the article ladder.
    for m in re.finditer("«AL:([^|]*)\\|([^«]*)«/AL»", text):
        target = m.group(1).strip()
        if _is_plausible_target(target):
            _add(m.group(0), target, "author")

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

    # Mixed-case / lowercase variants — added 2026-05-07 after coverage
    # audit found 1,710 resolvable candidates the strict patterns missed.
    # Dedup ensures these don't double-emit when the all-caps versions
    # also match.
    for m in _SEE_ARTICLE_PATTERN.finditer(text):
        _add(m.group(0), m.group(1), "see")

    for m in _SEE_ALSO_MIXED_PATTERN.finditer(text):
        _add(m.group(0), m.group(1), "see_also")

    for m in _SEE_MIXED_PATTERN.finditer(text):
        _add(m.group(0), m.group(1), "see")

    for m in _SEE_LOWER_PATTERN.finditer(text):
        _add(m.group(0), m.group(1), "see")

    for m in _CF_PATTERN.finditer(text):
        _add(m.group(0), m.group(1), "see")

    for m in _COMPARE_PATTERN.finditer(text):
        _add(m.group(0), m.group(1), "see")

    return results
