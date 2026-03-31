from dataclasses import dataclass

from britannica.db.models import Article, ArticleSegment, SourcePage
from britannica.db.session import SessionLocal
import re

_SEC_MARKER = re.compile(r"\u00abSEC:([^\u00bb]+)\u00bb")


@dataclass
class CandidateArticle:
    title: str
    body: str


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

    candidates = []
    for sec_id, sec_text in sections:
        # Determine the article title
        # Named sections (not s1, s2, s3...) use the section ID as the title
        is_named = not re.match(r"^s\d+$", sec_id)

        # Strip bold markers for heading detection
        first_line = sec_text.split("\n")[0] if sec_text else ""
        clean_first = re.sub(r"\u00abB\u00bb|\u00ab/B\u00bb", "", first_line)

        heading_match = re.match(
            r"^([A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE''.\-]+"
            r"(?:[\s,]+[A-Z\u00C0-\u00DE][A-Za-z\u00C0-\u00FF''.\-]*)*)",
            clean_first,
        )

        if is_named:
            if heading_match:
                title = heading_match.group(1).strip().rstrip(",.")
            else:
                title = sec_id.upper()
        else:
            if heading_match:
                title = heading_match.group(1).strip().rstrip(",.")
            else:
                # No heading found — continuation block
                if prefix:
                    prefix = prefix + "\n\n" + sec_text
                else:
                    prefix = sec_text
                continue

        # Extract body — find where the heading ends in the ORIGINAL text
        # (which may have bold markers)
        if heading_match:
            # Find the heading text in the original first line and skip past it
            heading_text = heading_match.group(0)
            # The original might have bold markers around the heading
            bold_heading = re.match(
                r"^(?:\u00abB\u00bb)?" + re.escape(heading_text) + r"(?:\u00ab/B\u00bb)?",
                first_line,
            )
            if bold_heading:
                body = first_line[bold_heading.end():].lstrip(" ,.")
            else:
                body = first_line[len(heading_text):].lstrip(" ,.")
            # Add remaining lines
            remaining_lines = sec_text.split("\n")[1:]
            if remaining_lines:
                remaining = "\n".join(remaining_lines).strip()
                if body and remaining:
                    body = body + "\n" + remaining
                elif remaining:
                    body = remaining
            body = body.strip()
        else:
            body = sec_text

        if title:
            candidates.append(CandidateArticle(title=title, body=body))

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
    clean = re.sub(r"\u00ab/?[BIS]C?\u00bb", "", line)

    # Match all-caps word(s) at the start, optionally with parenthetical and comma-name
    m = re.match(
        r"^([A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE’’.\-]+"
        r"(?:[\s]+[A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE’’.\-]+)*"
        r"(?:\s+\([^)]*\))?"
        r"(?:,\s*[A-Z\u00C0-\u00DE][A-Za-z\u00C0-\u00FF’’\-]+(?:\s+[A-Z\u00C0-\u00DE][A-Za-z\u00C0-\u00FF’’\-]+)*)?"
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
                and not stripped.startswith("{{IMG:")
                and not stripped.startswith(("{{TABLE:", "{{TABLEH:"))
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

    # Count non-image, non-marker words (actual prose)
    prose = re.sub(r"\{\{IMG:[^}]*\}\}", "", stripped)
    prose = re.sub(r"\{\{TABLE.*?\}TABLE\}", "", prose, flags=re.DOTALL)
    prose_words = len(prose.split())

    # Plate pages have many images relative to prose
    # A page with >500 words of prose is an article page, not a plate
    if prose_words > 500:
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
        if line.startswith(("{{TABLE:", "{{TABLEH:")):
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
        if line.startswith(("{{TABLE:", "{{TABLEH:")):
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

        if line.startswith(("{{TABLE:", "{{TABLEH:")):
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
        if any(l.startswith(("{{TABLE:", "{{TABLEH:")) for l in p):
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

            parsed = _parse_page_by_sections(text)
            if parsed is None:
                # No section markers — entire page is continuation of previous article
                parsed = ParsedPage(prefix_text=text, candidates=[])

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