from dataclasses import dataclass

from britannica.db.models import Article, ArticleSegment, SourcePage
from britannica.db.session import SessionLocal


@dataclass
class CandidateArticle:
    title: str
    body: str


@dataclass
class ParsedPage:
    prefix_text: str
    candidates: list[CandidateArticle]


def _is_heading(line: str) -> bool:
    line = line.strip()

    if not line:
        return False

    if len(line) > 120:
        return False

    if not any(ch.isalpha() for ch in line):
        return False

    return line.upper() == line


def _parse_page(text: str) -> ParsedPage:
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    if not lines:
        return ParsedPage(prefix_text="", candidates=[])

    first_heading_index: int | None = None
    for i, line in enumerate(lines):
        if _is_heading(line):
            first_heading_index = i
            break

    if first_heading_index is None:
        return ParsedPage(
            prefix_text="\n".join(lines).strip(),
            candidates=[],
        )

    prefix_lines = lines[:first_heading_index]
    article_lines = lines[first_heading_index:]

    candidates: list[CandidateArticle] = []
    current_title: str | None = None
    current_body_lines: list[str] = []

    for line in article_lines:
        if _is_heading(line):
            if current_title is not None:
                candidates.append(
                    CandidateArticle(
                        title=current_title,
                        body="\n".join(current_body_lines).strip(),
                    )
                )
            current_title = line
            current_body_lines = []
        else:
            if current_title is not None:
                current_body_lines.append(line)

    if current_title is not None:
        candidates.append(
            CandidateArticle(
                title=current_title,
                body="\n".join(current_body_lines).strip(),
            )
        )

    return ParsedPage(
        prefix_text="\n".join(prefix_lines).strip(),
        candidates=candidates,
    )


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
            (article.body.rstrip() + "\n" + segment_text).strip()
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