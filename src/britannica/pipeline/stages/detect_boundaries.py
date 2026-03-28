from britannica.db.models.article import Article
from britannica.db.models.article_segment import ArticleSegment
from britannica.db.models.source_page import SourcePage
from britannica.db.session import SessionLocal


def _split_title_and_body(text: str) -> tuple[str | None, str | None]:
    lines = [line.strip() for line in text.splitlines()]

    nonblank = [line for line in lines if line]
    if not nonblank:
        return None, None

    title = nonblank[0]

    if title.upper() != title:
        return None, None

    body_lines = nonblank[1:]
    body = "\n".join(body_lines).strip()

    return title, body


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

        for page in pages:
            text = page.cleaned_text or page.raw_text
            title, body = _split_title_and_body(text)

            if not title or body is None:
                continue

            article = Article(
                title=title,
                volume=page.volume,
                page_start=page.page_number,
                page_end=page.page_number,
                body=body,
            )
            session.add(article)
            session.flush()

            segment = ArticleSegment(
                article_id=article.id,
                source_page_id=page.id,
                sequence_in_article=1,
            )
            session.add(segment)

            created += 1

        session.commit()
        return created
    finally:
        session.close()