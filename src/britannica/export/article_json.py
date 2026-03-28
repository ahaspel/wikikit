import json
from pathlib import Path

from britannica.db.models import Article, ArticleSegment, CrossReference
from britannica.db.session import SessionLocal


def export_articles_to_json(volume: int, out_dir: str | Path) -> int:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    session = SessionLocal()

    try:
        articles = (
            session.query(Article)
            .filter(Article.volume == volume)
            .order_by(Article.page_start, Article.title)
            .all()
        )

        exported = 0

        for article in articles:
            segments = (
                session.query(ArticleSegment)
                .filter(ArticleSegment.article_id == article.id)
                .order_by(ArticleSegment.sequence_in_article)
                .all()
            )

            xrefs = (
                session.query(CrossReference)
                .filter(CrossReference.article_id == article.id)
                .order_by(CrossReference.id)
                .all()
            )

            payload = {
                "id": article.id,
                "title": article.title,
                "volume": article.volume,
                "page_start": article.page_start,
                "page_end": article.page_end,
                "body": article.body,
                "segments": [
                    {
                        "sequence_in_article": seg.sequence_in_article,
                        "source_page_id": seg.source_page_id,
                        "segment_text": seg.segment_text,
                    }
                    for seg in segments
                ],
                "xrefs": [
                    {
                        "surface_text": xref.surface_text,
                        "normalized_target": xref.normalized_target,
                        "xref_type": xref.xref_type,
                        "status": xref.status,
                        "target_article_id": xref.target_article_id,
                    }
                    for xref in xrefs
                ],
            }

            filename = f"{article.page_start:04d}-{article.title}.json"
            safe_filename = "".join(
                ch if ch.isalnum() or ch in ("-", "_", ".") else "_"
                for ch in filename
            )

            (out_path / safe_filename).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            exported += 1

        return exported

    finally:
        session.close()