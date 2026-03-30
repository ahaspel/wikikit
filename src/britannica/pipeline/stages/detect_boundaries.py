from dataclasses import dataclass

from britannica.db.models import Article, ArticleSegment, SourcePage
from britannica.db.session import SessionLocal
import re

@dataclass
class CandidateArticle:
    title: str
    body: str


@dataclass
class ParsedPage:
    prefix_text: str
    candidates: list[CandidateArticle]


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
    line = line.strip()

    if not line:
        return None, ""

    # Skip lines that ARE markers (start with marker syntax)
    if (line.startswith("{{IMG:") or line.startswith("{{TABLE:")
            or line.startswith("\u00abFN:") or line.startswith("\u00abMATH:")
            or line.startswith("}TABLE}")):
        return None, line

    # Skip lines with bare pipes (table content) — but not pipes inside markers or tables
    stripped_markers = re.sub(r"\u00ab[A-Z]+:[^\u00bb]*\u00bb", "", line)
    stripped_markers = re.sub(r"\{\{TABLE:.*?\}TABLE\}", "", stripped_markers, flags=re.DOTALL)
    if "|" in stripped_markers:
        return None, line

    # Simple all-uppercase heading line (max 40 chars — longer lines are
    # figure captions like "INTERIOR OF ST. LUKE'S, NEAR DELPHI")
    if line.upper() == line and len(line) <= 40:
        title = _normalize_title(line)
        if not _has_valid_title_content(title):
            return None, line
        return title, ""

    # Britannica biographical heading at start of a line, followed by prose.
    # Example:
    # ACCURSIUS (Ital. Accorso), FRANCISCUS (1182–1260), Italian jurist, was born...
    # ACCORSO (Accursius), MARIANGELO (c. 1490–1544), Italian critic, was born...
    m = re.match(
        r"^("
        r"[A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE’’.\-]+"
        r"(?:\s+[A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE’’.\-]+)*"
        r"(?:\s+\([^)]*\))?"
        r"(?:,\s*[A-Z\u00C0-\u00DE][A-Za-z\u00C0-\u00FF’’\-]+(?:\s+[A-Z\u00C0-\u00DE][A-Za-z\u00C0-\u00FF’’\-]+)*)?"
        r"(?:\s+\([^)]*\))?"
        r")"
        r"(.*)$",
        line,
    )
    if not m:
        return None, line

    # Reject if the title runs directly into lowercase text (e.g. "HAMITESFellahin")
    after_match = m.group(2)
    if after_match and after_match[0].islower():
        return None, line

    raw_title = m.group(1).strip()
    remainder = after_match.lstrip(" ,.")

    # Pull a trailing standalone number into the title (e.g. "WAR OF" + "1812")
    num_match = re.match(r"^(\d+)\b(.*)$", remainder)
    if num_match and raw_title.endswith((" OF", " OF THE")):
        raw_title = raw_title + " " + num_match.group(1)
        remainder = num_match.group(2).lstrip(" ,.")

    title = _normalize_title(raw_title)

    if len(title) > 255:
        return None, line

    if not _has_valid_title_content(title):
        return None, line

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
                and not stripped.startswith("{{IMG:")
                and not stripped.startswith("{{TABLE:")
                and not stripped.endswith("}TABLE}")
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
    """Detect plate pages — mostly image markers with little prose."""
    stripped = text.strip()
    img_count = stripped.count("{{IMG:")
    if img_count < 3:
        return False
    # Check if images appear very early (within first few lines)
    lines = [l.strip() for l in stripped.split("\n") if l.strip()]
    for line in lines[:3]:
        if line.startswith("{{IMG:"):
            return True
    return False


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
        if line.startswith("{{TABLE:"):
            in_table = True
        if in_table:
            if line.endswith("}TABLE}"):
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
        if line.startswith("{{TABLE:"):
            in_table = True
        if in_table:
            if current_title is not None:
                current_body_lines.append(line)
            if line.endswith("}TABLE}"):
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

    Table blocks ({{TABLE:...}TABLE}) are preserved with their internal newlines.
    """
    paragraphs: list[list[str]] = [[]]
    in_table = False

    for line in lines:
        if not line:
            if not in_table and paragraphs[-1]:
                paragraphs.append([])
            continue

        if line.startswith("{{TABLE:"):
            in_table = True
        if in_table:
            paragraphs[-1].append(line)
            if line.endswith("}TABLE}"):
                in_table = False
            continue

        paragraphs[-1].append(line)

    result_parts = []
    for p in paragraphs:
        if not p:
            continue
        # If this paragraph contains a table, join with newlines
        if any(l.startswith("{{TABLE:") for l in p):
            result_parts.append("\n".join(p))
        else:
            result_parts.append(" ".join(p))

    return "\n\n".join(result_parts).strip()


def _append_segment(
    article: Article,
    source_page_id: int,
    sequence_in_article: int,
    text: str,
    session,
) -> None:
    segment_text = (text or "").strip()

    session.add(
        ArticleSegment(
            article_id=article.id,
            source_page_id=source_page_id,
            sequence_in_article=sequence_in_article,
            segment_text=segment_text,
        )
    )

    if segment_text:
        if article.body:
            # Use paragraph break if the new segment starts with an image
            # or the previous body ends mid-image block
            joiner = "\n\n" if segment_text.startswith("{{IMG:") else " "
            article.body = (article.body.rstrip() + joiner + segment_text).strip()
        else:
            article.body = segment_text


def detect_boundaries(volume: int) -> int:
    session = SessionLocal()

    try:
        pages = (
            session.query(SourcePage)
            .filter(SourcePage.volume == volume)
            .order_by(SourcePage.page_number)
            .all()
        )

        created = 0
        open_article: Article | None = None
        open_article_last_segment_seq = 0

        for page in pages:
            text = (page.cleaned_text or page.raw_text or "").strip()

            # Detect plate pages: mostly images with little text.
            # These should not create article boundaries — create a plate
            # entry with all the content and let the previous article continue.
            if _is_plate_page(text):
                # Split plate page into sections by all-caps headings.
                # Each section becomes its own plate entry.
                sections = _split_plate_sections(text)

                for section_title, section_body in sections:
                    plate_article = Article(
                        title=section_title or f"PLATE (VOL. {page.volume}, P. {page.page_number})",
                        volume=page.volume,
                        page_start=page.page_number,
                        page_end=page.page_number,
                        body=section_body,
                        article_type="plate",
                    )
                    session.add(plate_article)
                    session.flush()
                    session.add(
                        ArticleSegment(
                            article_id=plate_article.id,
                            source_page_id=page.id,
                            sequence_in_article=1,
                            segment_text=section_body,
                        )
                    )
                    created += 1

                # Don't update open_article — let it continue past the plate
                continue

            parsed = _parse_page(text)

            # Prefix text belongs to the currently open article, if any.
            if parsed.prefix_text and open_article is not None:
                open_article_last_segment_seq += 1
                _append_segment(
                    article=open_article,
                    source_page_id=page.id,
                    sequence_in_article=open_article_last_segment_seq,
                    text=parsed.prefix_text,
                    session=session,
                )
                open_article.page_end = page.page_number

            # If there are no headings on the page, the whole page is continuation.
            if not parsed.candidates:
                if parsed.prefix_text and open_article is not None:
                    open_article.page_end = page.page_number
                continue

            # Create articles for headings found on the page.
            for candidate in parsed.candidates:
                body_text = (candidate.body or "").strip()

                existing = (
                    session.query(Article)
                    .filter(
                        Article.volume == page.volume,
                        Article.page_start == page.page_number,
                        Article.title == candidate.title,
                    )
                    .first()
                )

                if existing:
                    open_article = existing
                    open_article_last_segment_seq = (
                        session.query(ArticleSegment)
                        .filter(ArticleSegment.article_id == existing.id)
                        .count()
                    )
                    continue

                article = Article(
                    title=candidate.title,
                    volume=page.volume,
                    page_start=page.page_number,
                    page_end=page.page_number,
                    body=body_text,
                )
                session.add(article)
                session.flush()

                session.add(
                    ArticleSegment(
                        article_id=article.id,
                        source_page_id=page.id,
                        sequence_in_article=1,
                        segment_text=body_text,
                    )
                )

                open_article = article
                open_article_last_segment_seq = 1
                created += 1

        session.commit()
        return created

    finally:
        session.close()