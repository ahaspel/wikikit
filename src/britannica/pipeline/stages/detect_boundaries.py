from dataclasses import dataclass, field

from britannica.db.models import Article, ArticleSegment, SourcePage
from britannica.db.session import SessionLocal
import re

# Raw wikitext section-begin tag.
_SEC_MARKER = re.compile(r'<section\s+begin="([^"]+)"\s*/?>', re.IGNORECASE)

# Section-end tags — stripped during preprocessing.
_SEC_END = re.compile(r'<section\s+end="[^"]*"\s*/?>', re.IGNORECASE)

# <noinclude> blocks — stripped during preprocessing (page headers, quality tags).
_NOINCLUDE = re.compile(r"<noinclude>.*?</noinclude>", re.DOTALL | re.IGNORECASE)

# Generic Wikisource section IDs that are never real article titles.
_GENERIC_SEC_ID = re.compile(
    r"^(?:part|s|text|rpart|plate)\d*$", re.IGNORECASE
)


def _is_article_section_id(sec_id: str) -> bool:
    """Return True if a section ID looks like a real article title, not a
    generic Wikisource continuation marker (part1, s2, text1, etc.)."""
    if _GENERIC_SEC_ID.match(sec_id):
        return False
    # Trailing digits on an otherwise-valid name (Egypt2, Egypt3) are
    # Wikisource continuations, not separate articles.
    if re.search(r"\d+$", sec_id):
        return False
    # Single letters are handled separately — they are only valid as
    # letter-of-the-alphabet articles, detected via _is_letter_article.
    if len(sec_id) == 1:
        return False
    return True


def _is_letter_article(sec_id: str, sec_text: str) -> bool:
    """Return True if this section is a single-letter encyclopedia article
    (e.g. the article about the letter A, B, C, etc.).
    These have a single-letter section ID and text about the letter itself."""
    if len(sec_id) != 1 or not sec_id.isalpha():
        return False
    # The text should be about the letter — check for characteristic phrases
    lower = sec_text[:200].lower()
    return any(phrase in lower for phrase in [
        "letter", "alphabet", "symbol", "phoenician",
    ])


def _preprocess_wikitext(text: str) -> str:
    """Minimal preprocessing of raw wikitext for boundary detection.

    Strips <noinclude> blocks, <section end> tags, and normalizes line endings.
    Preserves <section begin> tags and all other raw wikitext.
    """
    text = _NOINCLUDE.sub("", text)
    text = _SEC_END.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


# ── Detection output types ─────────────────────────────────────────────


@dataclass
class SegmentInfo:
    """A text segment belonging to an article, with its source page."""
    source_page_id: int
    page_number: int
    sequence: int
    text: str


@dataclass
class DetectedArticle:
    """An article boundary detected from raw wikitext.  Pure data — no DB models."""
    title: str
    volume: int
    page_start: int
    page_end: int
    article_type: str  # "article" or "plate"
    segments: list[SegmentInfo] = field(default_factory=list)

    @property
    def body(self) -> str:
        """Reconstruct full body from segments."""
        parts = []
        for seg in sorted(self.segments, key=lambda s: s.sequence):
            text = (seg.text or "").strip()
            if not text:
                continue
            if parts:
                joiner = "\n\n" if re.match(r"\[\[(?:File|Image):", text, re.IGNORECASE) else " "
                parts.append(joiner)
            parts.append(text)
        return "".join(parts).strip()


# ── Per-page parsing helpers ───────────────────────────────────────────


@dataclass
class CandidateArticle:
    title: str
    body: str
    is_tentative: bool = False  # True if created from named section without bold


@dataclass
class ParsedPage:
    prefix_text: str
    candidates: list[CandidateArticle]


def _parse_page_by_sections(text: str) -> ParsedPage | None:
    """Parse a page using section markers if present.

    Returns None if no section markers found (fall back to heuristic parsing).
    """
    markers = list(_SEC_MARKER.finditer(text))
    if not markers:
        return None

    # Split text into sections
    sections: list[tuple[str, str]] = []  # (section_id, section_text)
    for i, m in enumerate(markers):
        start = m.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        section_text = text[start:end].strip()
        sections.append((m.group(1), section_text))

    # Text before the first marker is prefix (continuation of previous article)
    prefix = text[:markers[0].start()].strip()

    # Pre-split: a single section may contain multiple articles if there
    # are bold headings mid-text.  Split on '''ALLCAPS patterns at
    # paragraph boundaries only (double newline).  Raw wikitext has
    # hard line breaks within paragraphs, so a single \n would create
    # false splits on bold words that happen to start a line.
    _BOLD_SPLIT = re.compile(
        r"\n\n(?='{3}[A-Z])"
    )
    expanded_sections = []
    for sec_id, sec_text in sections:
        parts = _BOLD_SPLIT.split(sec_text)
        for j, part in enumerate(parts):
            # First part keeps the original section ID; subsequent parts
            # are anonymous sub-sections created by the split.
            sub_id = sec_id if j == 0 else f"s{900 + len(expanded_sections)}"
            expanded_sections.append((sub_id, part.strip()))

    candidates = []
    for sec_id, sec_text in expanded_sections:
        # Determine the article title
        # Named sections (not s1, s2, s3...) use the section ID as the title
        is_named = not re.match(r"^s\d+$", sec_id)

        # Strip link wrappers and bold markers for heading detection.
        # Some Wikisource headings are wrapped in [[link|'''TITLE''']] wrappers.
        # Skip leading non-content lines (HTML comments, tables, images,
        # templates) to find the actual first content line.
        first_line = ""
        _sec_lines = sec_text.split("\n")
        _first_line_idx = 0
        for _i, _line in enumerate(_sec_lines):
            stripped = _line.strip()
            if not stripped:
                continue
            if re.match(r"^(<!--.*?-->|<table\b|\{\||<tr|<td|\[\[(?:File|Image):|\{\{)", stripped, re.IGNORECASE):
                continue
            first_line = stripped
            _first_line_idx = _i
            break
        first_line_unwrapped = re.sub(
            r"\[\[[^\]|]*\|(.*?)\]\]",
            r"\1", first_line,
        )
        clean_first = first_line_unwrapped.replace("'''", "")

        # Extract the title from the start of the line.  The pattern must
        # handle Mc/Mac/O'/d' prefixes which mix case (McCORMICK, O'BRIEN).
        # Each word starts with uppercase (or a known prefix + uppercase).
        # After the prefix, remaining chars must be uppercase to avoid
        # matching into body text like "ABACUS A calculating..."
        heading_match = re.match(
            r"^([A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE''\u2019\-]*"
            r"(?:[\s,]+[A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE''\u2019\-]+)*)",
            clean_first,
        )

        # A bold heading '''TITLE''' at the start is the definitive signal
        # for a new article.  Named sections without bold are continuations
        # of the previous article repeated across pages on Wikisource.
        has_bold_heading = first_line_unwrapped.startswith("'''")
        _is_tentative = False

        # Bold heading is the sole signal for a new article, whether
        # the section is named or anonymous.
        # Exception: a named section without bold that is the first content
        # on the page (no candidates yet, no prefix) is treated as a new
        # article — it's not a continuation if there's nothing to continue.
        _used_bold_fallback = False
        if has_bold_heading:
            heading_title = heading_match.group(1).strip().rstrip(",.") if heading_match else None
            # If the regex couldn't extract the title, fall back to
            # extracting the full bold text (handles Mc/Mac/O'/d' prefixes
            # and extended Unicode that the heading regex doesn't cover).
            if heading_title is None or not _has_valid_title_content(_normalize_title(heading_title)):
                bold_match = re.match(r"^'''([^']+)'''", first_line)
                if bold_match:
                    fallback = bold_match.group(1).strip().rstrip(",.")
                    # Strip any template wrappers (e.g. {{sc|...}})
                    fallback = re.sub(r"\{\{[^{}|]*\|([^{}]*)\}\}", r"\1", fallback)
                    heading_title = fallback
                    _used_bold_fallback = True
            # Prefer section ID when the heading match is a partial capture
            # (e.g. "TISIO" from "TISIO (or Tisi), BENVENUTO")
            if (heading_title and is_named
                    and heading_title.upper() != sec_id.upper()
                    and sec_id.upper().startswith(heading_title.upper().split(",")[0].split()[0])):
                title = sec_id.upper()
            elif heading_title and _has_valid_title_content(
                _normalize_title(heading_title)
            ):
                title = heading_title
            elif is_named and (len(sec_id) != 1 or _is_letter_article(sec_id, sec_text)):
                title = sec_id.upper()
            else:
                title = None
        elif _is_letter_article(sec_id, sec_text):
            # Single-letter article about the letter itself
            title = sec_id.upper()
        elif is_named and not candidates and not prefix and _is_article_section_id(sec_id):
            # First named section on the page, no bold — tentatively new article
            title = sec_id.upper()
            _is_tentative = True
        else:
            # No bold heading — check if this is a numbered continuation
            # of a candidate on this page (e.g. Japan01 → Japan).
            base_id = re.sub(r"\d+$", "", sec_id)
            matched_candidate = None
            if base_id:
                for c in reversed(candidates):
                    if c.title.upper() == base_id.upper():
                        matched_candidate = c
                        break
            if matched_candidate:
                matched_candidate.body += "\n\n" + sec_text
                continue
            # Otherwise, continuation of previous article
            if prefix:
                prefix = prefix + "\n\n" + sec_text
            else:
                prefix = sec_text
            continue

        if not title:
            if prefix:
                prefix = prefix + "\n\n" + sec_text
            else:
                prefix = sec_text
            continue

        # Extract body — find where the heading ends in the ORIGINAL text
        # (which may have bold markers)
        if heading_match and not _used_bold_fallback:
            # Find the heading text in the original first line and skip past it
            heading_text = heading_match.group(0)
            # The original might have bold markers (''') around the heading.
            # Strip ALL bold groups at the start (handles multi-word titles
            # like '''PRAXIAS''' and '''ANDROSTHENES,''')
            bold_heading = re.match(
                r"^(?:'{3}[^']+'{3}[\s,.\-]*(?:and\s+|&\s+)?)+",
                first_line, re.IGNORECASE,
            )
            if bold_heading:
                body = first_line[bold_heading.end():].lstrip(" ,.")
            else:
                # Fall back: strip bold markers, find heading, take the rest
                body = clean_first[len(heading_text):].lstrip(" ,.")
            # Add remaining lines (after the heading line, skipping
            # any leading tables/images that were before the heading)
            remaining_lines = _sec_lines[_first_line_idx + 1:]
            if remaining_lines:
                remaining = "\n".join(remaining_lines).strip()
                if body and remaining:
                    body = body + "\n" + remaining
                elif remaining:
                    body = remaining
            body = body.strip()
        elif has_bold_heading:
            # Bold heading present but regex couldn't parse it — strip
            # the bold text from the first line to get the body.
            stripped_first = re.sub(r"^'''[^']+'''\s*", "", first_line).lstrip(" ,.")
            remaining_lines = _sec_lines[_first_line_idx + 1:]
            if remaining_lines:
                remaining = "\n".join(remaining_lines).strip()
                if stripped_first and remaining:
                    body = stripped_first + "\n" + remaining
                elif remaining:
                    body = remaining
                else:
                    body = stripped_first
            else:
                body = stripped_first
            body = body.strip()
        else:
            body = "\n".join(_sec_lines[_first_line_idx:]).strip()

        if title:
            candidates.append(CandidateArticle(
                title=title, body=body, is_tentative=_is_tentative,
            ))

    return ParsedPage(prefix_text=prefix, candidates=candidates)


def _split_on_bold_headings(text: str) -> ParsedPage:
    """Split text with no section markers on bold headings.

    Returns a ParsedPage with prefix (text before first bold heading)
    and candidates for each bold heading found.
    """
    # Split on bold headings at line/paragraph boundaries
    # In raw wikitext, bold is '''TEXT'''
    _BOLD_HEADING = re.compile(
        r"(?:^|\n\n?)('{3}"
        r"[A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE'\u2019\-,. ]+'{3})",
    )

    parts = _BOLD_HEADING.split(text)

    if len(parts) <= 1:
        # No bold headings found — entire text is continuation
        return ParsedPage(prefix_text=text, candidates=[])

    prefix = parts[0].strip()
    candidates = []

    # parts alternates: text, bold-match, text, bold-match, text, ...
    i = 1
    while i < len(parts):
        bold_marker = parts[i]
        body_after = parts[i + 1].strip() if i + 1 < len(parts) else ""
        # Extract title from the bold marker
        clean = bold_marker.replace("'''", "")
        title = clean.strip().rstrip(",.")
        if title:
            body_text = body_after.lstrip(" ,.")
            candidates.append(CandidateArticle(title=title, body=body_text))
        i += 2

    return ParsedPage(prefix_text=prefix, candidates=candidates)


def _is_heading(line: str) -> bool:
    title, _ = _extract_heading(line)
    return title is not None


def _normalize_title(title: str) -> str:
    """Strip parentheticals, trailing periods, and spacing artifacts."""
    # Strip all parenthetical content (etymologies, dates, alternate names)
    title = re.sub(r"\s*\([^)]*\)", "", title)
    # Collapse whitespace
    title = re.sub(r"\s+", " ", title).strip()
    # Clean comma artifacts from parenthetical removal (e.g. "SMITH, , JOHN")
    title = re.sub(r",\s*,", ",", title).strip(", ")
    # Strip trailing phrase after comma if it's:
    # - mixed-case (descriptor like "Greek", "Grand Master")
    # - a 2-letter fragment not in the valid title list (formula like "CH")
    if "," in title:
        before, _, after = title.rpartition(",")
        after = after.strip()
        if after and not after.isupper():
            title = before.strip()
        elif after and len(after) == 2 and after not in _VALID_TWO_LETTER:
            title = before.strip()
    # Strip trailing period (encyclopedia formatting convention)
    title = re.sub(r"\.$", "", title)
    return title


# Matches pure Roman numerals (II, IV) and numbered section headings (IV. TOPIC)
_ROMAN_NUMERAL = re.compile(r"^[IVXLCDM]+\.?(\s|$)")

# Two-letter article titles that actually exist in the encyclopedia.
# Other 2-letter combinations (CH, RO, OF, etc.) are fragments.
_VALID_TWO_LETTER = frozenset({
    "AA", "AB", "AD", "AE", "AI", "AL", "AM", "AN", "AR", "AS", "AT",
    "AX", "AY",
})


def _has_valid_title_content(title: str) -> bool:
    """Require at least one run of 2+ consecutive uppercase letters.

    Also rejects:
    - chemical formulas (middle-dot, arrow, or digits mixed with letters)
    - pure Roman numerals (section numbers like II, III, IV)
    - two-letter titles not in the known allowlist
    """
    if "\u00b7" in title or "\u2192" in title:
        return False
    # Reject digits mixed with letters (chemical formulas like CH3, C6H5)
    # but allow standalone numbers (dates like 1812 in "WAR OF 1812")
    if re.search(r"[A-Za-z]\d|\d[A-Za-z]", title):
        return False
    if title and title[0].isdigit():
        return False
    # Single letter titles (A, B, C) exist but are too ambiguous —
    # they conflict with contributor initials on front matter pages.
    # These 26 articles will be handled as special cases later.
    if _ROMAN_NUMERAL.match(title):
        return False
    # Reject numbered section headings (ORDER I, PART II, CLASS IV, etc.)
    if re.match(
        r"^(?:ORDER|PART|SECTION|CLASS|BOOK|CHAPTER|DIVISION|GROUP|SERIES|PERIOD|GRADE|LEGION|BRIGADE|FAMILY|TRIBE|GENUS|SUBORDER|SUBFAMILY)\s+[IVXLCDM]+\.?$",
        title,
    ):
        return False
    # Reject unknown 2-letter titles (common source of fragments)
    if len(title) == 2 and title not in _VALID_TWO_LETTER:
        return False
    return bool(re.search(r"[A-Z\u00C0-\u00DE]{2,}", title))


def _extract_heading(line: str) -> tuple[str | None, str]:
    """Extract an all-caps heading from the start of a line.

    Used by _parse_page_by_sections for anonymous sections only.
    Returns (title, remainder) or (None, line) if no heading found.
    """
    line = line.strip()
    if not line:
        return None, ""

    # Strip formatting markers before heading detection
    # In raw wikitext: bold ('''), italic (''), wiki markup
    clean = line.replace("'''", "").replace("''", "")

    # Match all-caps word(s) at the start, optionally with parenthetical and comma-name
    m = re.match(
        r"^([A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE''\u2019.\-]+"
        r"(?:[\s]+[A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE''\u2019.\-]+)*"
        r"(?:\s+\([^)]*\))?"
        r"(?:,\s*[A-Z\u00C0-\u00DE][A-Za-z\u00C0-\u00FF''\u2019\-]+(?:\s+[A-Z\u00C0-\u00DE][A-Za-z\u00C0-\u00FF''\u2019\-]+)*)?"
        r")",
        clean,
    )
    if not m:
        return None, line

    raw_title = m.group(0).strip().rstrip(",.")
    # Strip parentheticals from title
    title = re.sub(r"\s*\([^)]*\)", "", raw_title).strip()
    title = re.sub(r"\.$", "", title)

    if not title or len(title) > 255:
        return None, line

    # Get remainder from the clean line
    remainder = clean[m.end():].lstrip(" ,.")
    return title, remainder


def _split_plate_sections(text: str) -> list[tuple[str | None, str]]:
    """Split a plate page into sections by all-caps headings.

    Returns list of (title, body) tuples. If no headings are found,
    returns one section with no title.
    """
    lines = text.split("\n")
    sections: list[tuple[str | None, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check if this is a section heading (all-caps, short, not a marker)
        if (stripped.upper() == stripped
                and not stripped.startswith("[[File:")
                and not stripped.startswith("[[Image:")
                and not stripped.startswith(("{|", "|-", "|}"))
                and 2 < len(stripped) <= 60
                and any(c.isalpha() for c in stripped)
                and not any(c.isdigit() for c in stripped)):
            # New section — save the previous one
            heading = stripped.rstrip(".")
            if heading != current_title:
                if current_lines:
                    sections.append((current_title, "\n\n".join(current_lines)))
                    current_lines = []
                current_title = heading
        else:
            current_lines.append(stripped)

    # Save the last section
    if current_lines or current_title:
        sections.append((current_title, "\n\n".join(current_lines)))

    # If no sections were created, return the whole text as one section
    if not sections:
        sections.append((None, text.strip()))

    # Merge initial untitled section into the first titled section
    if len(sections) > 1 and sections[0][0] is None and sections[1][0] is not None:
        combined_body = sections[0][1] + "\n\n" + sections[1][1] if sections[0][1] else sections[1][1]
        sections = [(sections[1][0], combined_body)] + sections[2:]

    # Merge sections with the same title
    merged: dict[str | None, list[str]] = {}
    order: list[str | None] = []
    for title, body in sections:
        if title not in merged:
            merged[title] = []
            order.append(title)
        if body:
            merged[title].append(body)

    return [(title, "\n\n".join(merged[title])) for title in order if merged[title]]


def _is_plate_page(text: str) -> bool:
    """Detect plate pages — mostly images with little prose.

    In raw wikitext, images are [[File:...]] or [[Image:...]].
    Plate pages typically have 3+ images and under 500 words of prose,
    with images often wrapped in wiki tables ({|...|}), not bare.
    """
    stripped = text.strip()
    img_count = len(re.findall(r"\[\[(?:File|Image):", stripped, re.IGNORECASE))
    if img_count < 3:
        return False

    # Count non-image, non-table words (actual prose)
    prose = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", "", stripped, flags=re.IGNORECASE)
    prose = re.sub(r"\{\|.*?\|\}", "", prose, flags=re.DOTALL)
    prose_words = len(prose.split())

    return prose_words <= 500


def _parse_page(text: str) -> ParsedPage:
    # Split into paragraph blocks (separated by blank lines), preserving structure.
    raw_lines = text.splitlines()

    # Collapse into non-empty lines, preserving blank-line boundaries as "\n\n".
    lines: list[str] = []
    prev_blank = False
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            prev_blank = True
            continue
        if prev_blank and lines:
            # Insert a paragraph-break marker before this line
            lines.append("")
        lines.append(stripped)
        prev_blank = False

    # Filter to only non-empty lines for heading detection
    content_lines = [l for l in lines if l]

    if not content_lines:
        return ParsedPage(prefix_text="", candidates=[])

    first_heading_index: int | None = None
    in_table = False
    for i, line in enumerate(lines):
        if not line:
            continue
        if line.startswith("{|"):
            in_table = True
        if in_table:
            if line.startswith("|}"):
                in_table = False
            continue
        title, _ = _extract_heading(line)
        if title is not None:
            first_heading_index = i
            break

    if first_heading_index is None:
        return ParsedPage(
            prefix_text=_join_lines(lines),
            candidates=[],
        )

    prefix_lines = lines[:first_heading_index]
    article_lines = lines[first_heading_index:]

    candidates: list[CandidateArticle] = []
    current_title: str | None = None
    current_body_lines: list[str] = []
    in_table = False

    for line in article_lines:
        if not line:
            # Blank line = paragraph break within body
            current_body_lines.append(line)
            continue

        # Skip heading detection inside table blocks
        if line.startswith("{|"):
            in_table = True
        if in_table:
            if current_title is not None:
                current_body_lines.append(line)
            if line.startswith("|}"):
                in_table = False
            continue

        title, remainder = _extract_heading(line)

        if title is not None:
            if current_title is not None:
                candidates.append(
                    CandidateArticle(
                        title=current_title,
                        body=_join_lines(current_body_lines),
                    )
                )

            current_title = title
            current_body_lines = []

            if remainder:
                current_body_lines.append(remainder)
        else:
            if current_title is not None:
                current_body_lines.append(line)

    if current_title is not None:
        candidates.append(
            CandidateArticle(
                title=current_title,
                body=_join_lines(current_body_lines),
            )
        )

    return ParsedPage(
        prefix_text=_join_lines(prefix_lines),
        candidates=candidates,
    )


def _join_lines(lines: list[str]) -> str:
    """Join lines, treating empty strings as paragraph-break markers.

    Table blocks ({|...|}  in raw wikitext) are preserved with their
    internal newlines.
    """
    paragraphs: list[list[str]] = [[]]
    in_table = False

    for line in lines:
        if not line:
            if not in_table and paragraphs[-1]:
                paragraphs.append([])
            continue

        if line.startswith("{|"):
            in_table = True
        if in_table:
            paragraphs[-1].append(line)
            if line.startswith("|}"):
                in_table = False
            continue

        paragraphs[-1].append(line)

    result_parts = []
    for p in paragraphs:
        if not p:
            continue
        # If this paragraph contains a table, join with newlines
        if any(l.startswith("{|") for l in p):
            result_parts.append("\n".join(p))
        else:
            result_parts.append(" ".join(p))

    return "\n\n".join(result_parts).strip()


# ── Detection (pure) ──────────────────────────────────────────────────


def detect_boundaries(volume: int) -> list[DetectedArticle]:
    """Detect article boundaries from raw wikitext.

    Reads SourcePages from the database but writes nothing.
    Returns a list of DetectedArticle with titles, page ranges, and
    raw wikitext segments.
    """
    session = SessionLocal()

    try:
        pages = (
            session.query(SourcePage)
            .filter(SourcePage.volume == volume)
            .order_by(SourcePage.page_number)
            .all()
        )

        articles: list[DetectedArticle] = []
        open_article: DetectedArticle | None = None

        for page in pages:
            # Read raw wikitext and preprocess minimally.
            raw = (page.wikitext or "").strip()
            if not raw:
                continue
            text = _preprocess_wikitext(raw)

            # Try to parse article boundaries from this page.
            parsed = _parse_page_by_sections(text)
            if parsed is None:
                parsed = _split_on_bold_headings(text)

            # If the page has no article boundaries, check whether it's
            # a plate page (full-page image layout) or a continuation.
            # Plate pages occupy a full page, start no articles, and are
            # mostly images — they should not be folded into the open
            # article's body text.
            if not parsed.candidates and _is_plate_page(text):
                # Extract plate title from page header templates
                plate_title = None
                header_match = re.search(
                    r"<noinclude>(.*?)</noinclude>", raw, re.DOTALL)
                if header_match:
                    hdr = header_match.group(1)
                    # {{x-larger|TITLE}} or {{larger|TITLE}}
                    t = re.search(r"\{\{x-larger\|([^}]+)\}\}", hdr)
                    if not t:
                        t = re.search(r"\{\{larger\|([^}]+)\}\}", hdr)
                    # {{EB1911 Page Heading|...|TITLE|...}}
                    if not t:
                        t = re.search(
                            r"\{\{EB1911 Page Heading\|[^|]*\|([^|]+)\|",
                            hdr)
                    if t:
                        plate_title = t.group(1).strip()
                if not plate_title:
                    plate_title = f"PLATE (VOL. {page.volume}, P. {page.page_number})"

                plate = DetectedArticle(
                    title=plate_title,
                    volume=page.volume,
                    page_start=page.page_number,
                    page_end=page.page_number,
                    article_type="plate",
                    segments=[SegmentInfo(
                        source_page_id=page.id,
                        page_number=page.page_number,
                        sequence=1,
                        text=text,
                    )],
                )
                articles.append(plate)

                # Don't update open_article — let it continue past the plate
                continue

            # Prefix text belongs to the currently open article — unless
            # it starts with a bold ALL-CAPS heading, which signals a new
            # article placed before the first section marker on this page.
            # Only all-caps titles are treated as articles; mixed-case bold
            # text is a subsection heading within the current article.
            if parsed.prefix_text:
                _prefix_art_match = re.match(
                    r"'''([A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE''\u2019\s,.\-]+)'''",
                    parsed.prefix_text,
                )
                if (_prefix_art_match and parsed.candidates
                        and _has_valid_title_content(
                            _normalize_title(_prefix_art_match.group(1).strip().rstrip(",.")))):
                    # Split the prefix: text before bold heading → open article,
                    # bold heading onward → new candidate prepended to the list.
                    prefix_parsed = _split_on_bold_headings(parsed.prefix_text)
                    if prefix_parsed.prefix_text and open_article is not None:
                        next_seq = len(open_article.segments) + 1
                        open_article.segments.append(SegmentInfo(
                            source_page_id=page.id,
                            page_number=page.page_number,
                            sequence=next_seq,
                            text=prefix_parsed.prefix_text,
                        ))
                        open_article.page_end = page.page_number
                    parsed.candidates = prefix_parsed.candidates + parsed.candidates
                elif open_article is not None:
                    next_seq = len(open_article.segments) + 1
                    open_article.segments.append(SegmentInfo(
                        source_page_id=page.id,
                        page_number=page.page_number,
                        sequence=next_seq,
                        text=parsed.prefix_text,
                    ))
                    open_article.page_end = page.page_number

            # If there are no headings on the page, the whole page is continuation.
            if not parsed.candidates:
                if parsed.prefix_text and open_article is not None:
                    open_article.page_end = page.page_number
                continue

            # Process headings found on the page.
            for candidate in parsed.candidates:
                body_text = (candidate.body or "").strip()

                # Wikisource repeats <section begin="X"> on continuation pages.
                # If the currently open article has the same title, this is
                # continuation — append rather than creating a duplicate.
                exact_match = (
                    open_article is not None
                    and open_article.title == candidate.title
                )
                _open_base = re.sub(r"\d+$", "", open_article.title) if open_article else ""
                _cand_base = re.sub(r"\d+$", "", candidate.title)
                fuzzy_match = (
                    open_article is not None
                    and candidate.is_tentative
                    and (
                        _open_base.startswith(_cand_base)
                        or _cand_base.startswith(_open_base)
                    )
                    and _cand_base
                )
                if (exact_match or fuzzy_match) and body_text and candidate.is_tentative:
                    next_seq = len(open_article.segments) + 1
                    open_article.segments.append(SegmentInfo(
                        source_page_id=page.id,
                        page_number=page.page_number,
                        sequence=next_seq,
                        text=body_text,
                    ))
                    open_article.page_end = page.page_number
                    continue

                # New article
                detected = DetectedArticle(
                    title=candidate.title,
                    volume=page.volume,
                    page_start=page.page_number,
                    page_end=page.page_number,
                    article_type="article",
                )
                if body_text:
                    detected.segments.append(SegmentInfo(
                        source_page_id=page.id,
                        page_number=page.page_number,
                        sequence=1,
                        text=body_text,
                    ))
                articles.append(detected)
                open_article = detected

        return articles

    finally:
        session.close()


# ── Persistence ────────────────────────────────────────────────────────


def persist_articles(detected: list[DetectedArticle]) -> int:
    """Create Article and ArticleSegment records from detected boundaries."""
    session = SessionLocal()
    try:
        for det in detected:
            article = Article(
                title=det.title,
                volume=det.volume,
                page_start=det.page_start,
                page_end=det.page_end,
                body=det.body,
                article_type=det.article_type if det.article_type == "plate" else None,
            )
            session.add(article)
            session.flush()

            for seg in det.segments:
                session.add(ArticleSegment(
                    article_id=article.id,
                    source_page_id=seg.source_page_id,
                    sequence_in_article=seg.sequence,
                    segment_text=(seg.text or "").strip(),
                ))

        session.commit()
        return len(detected)
    finally:
        session.close()
