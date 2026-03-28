from dataclasses import dataclass

from britannica.db.models import Article, ArticleSegment, SourcePage
from britannica.db.session import SessionLocal


@dataclass
class CandidateArticle:
    title: str
    body: str


def _is_heading(line: str) -> bool:
    line = line.strip()

    if not line:
        return False

    if len(line) > 120:
        return False

    if not any(ch.isalpha() for ch in line):
        return False

    return line.upper() == line


def _extract_candidates_from_page(text: str) -> list[CandidateArticle]:
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    candidates: list[CandidateArticle] = []
    current_title: str | None = None
    current_body_lines: list[str] = []

    for line in lines:
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

    return candidates


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
        open_article_id: int | None = None
        open_article_last_segment_seq = 0

        for page in pages:
            text = (page.cleaned_text or page.raw_text or "").strip()
            candidates = _extract_candidates_from_page(text)

            # No heading on this page: treat as continuation of the open article.
            if not candidates:
                if open_article_id is not None and text:
                    open_article_last_segment_seq += 1

                    session.add(
                        ArticleSegment(
                            article_id=open_article_id,
                            source_page_id=page.id,
                            sequence_in_article=open_article_last_segment_seq,
                            segment_text=text,
                        )
                    )

                    article = session.get(Article, open_article_id)
                    if article is not None:
                        article.page_end = page.page_number
                        article.body = (
                            (article.body.rstrip() + "\n" + text).strip()
                            if article.body
                            else text
                        )

                continue

            # One or more headings on this page.
            for candidate in candidates:
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
                    open_article_id = existing.id
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

                open_article_id = article.id
                open_article_last_segment_seq = 1
                created += 1

        session.commit()
        return created

    finally:
        session.close()