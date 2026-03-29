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
    # Strip trailing period (encyclopedia formatting convention)
    title = re.sub(r"\.$", "", title)
    return title


def _has_valid_title_content(title: str) -> bool:
    """Require at least one run of 2+ consecutive uppercase letters.

    Also rejects chemical formulas:
    - contain middle-dot or arrow characters
    - contain digits (from preserved subscript/superscript numbers)
    """
    if "\u00b7" in title or "\u2192" in title:
        return False
    if re.search(r"\d", title):
        return False
    return bool(re.search(r"[A-Z]{2,}", title))


def _extract_heading(line: str) -> tuple[str | None, str]:
    line = line.strip()

    if not line:
        return None, ""

    # Simple all-uppercase heading line
    if line.upper() == line and len(line) <= 255:
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
        r"[A-Z][A-Z’’.\-]+"
        r"(?:\s+[A-Z][A-Z’’.\-]+)*"
        r"(?:\s*\([^)]*\))?"
        r"(?:,\s*[A-Z][A-Za-z’’.\-]+(?:\s+[A-Z][A-Za-z’’.\-]+)*)?"
        r"(?:\s*\([^)]*\))?"
        r")"
        r"(.*)$",
        line,
    )
    if not m:
        return None, line

    raw_title = m.group(1).strip()
    remainder = m.group(2).lstrip(" ,")

    title = _normalize_title(raw_title)

    if len(title) > 255:
        return None, line

    if not _has_valid_title_content(title):
        return None, line

    return title, remainder


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
    for i, line in enumerate(lines):
        if not line:
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

    for line in article_lines:
        if not line:
            # Blank line = paragraph break within body
            current_body_lines.append(line)
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
    """Join lines, treating empty strings as paragraph-break markers."""
    paragraphs: list[list[str]] = [[]]
    for line in lines:
        if not line:
            if paragraphs[-1]:
                paragraphs.append([])
        else:
            paragraphs[-1].append(line)
    return "\n\n".join(
        " ".join(p) for p in paragraphs if p
    ).strip()


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
        article.body = (
            (article.body.rstrip() + " " + segment_text).strip()
            if article.body
            else segment_text
        )


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