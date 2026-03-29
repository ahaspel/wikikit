import json
from pathlib import Path

from britannica.db.models import (
    Article, ArticleContributor, ArticleImage, ArticleSegment,
    Contributor, CrossReference,
)
from britannica.db.session import SessionLocal


def _safe_filename(page_start: int, title: str) -> str:
    raw = f"{page_start:04d}-{title}.json"
    return "".join(
        ch if ch.isalnum() or ch in ("-", "_", ".") else "_"
        for ch in raw
    )


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

            xref_list = []
            for xref in xrefs:
                entry = {
                    "surface_text": xref.surface_text,
                    "normalized_target": xref.normalized_target,
                    "xref_type": xref.xref_type,
                    "status": xref.status,
                    "target_article_id": xref.target_article_id,
                }
                if xref.target_article_id is not None:
                    target = session.get(Article, xref.target_article_id)
                    if target:
                        entry["target_filename"] = _safe_filename(
                            target.page_start, target.title
                        )
                xref_list.append(entry)

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
                "xrefs": xref_list,
                "images": [
                    {
                        "filename": img.filename,
                        "caption": img.caption,
                        "commons_url": img.commons_url,
                        "source_page_id": img.source_page_id,
                    }
                    for img in (
                        session.query(ArticleImage)
                        .filter(ArticleImage.article_id == article.id)
                        .order_by(ArticleImage.source_page_id, ArticleImage.sequence_in_article)
                        .all()
                    )
                ],
                "contributors": [
                    {
                        "initials": contrib.initials,
                        "full_name": contrib.full_name,
                    }
                    for contrib in (
                        session.query(Contributor)
                        .join(ArticleContributor, ArticleContributor.contributor_id == Contributor.id)
                        .filter(ArticleContributor.article_id == article.id)
                        .order_by(ArticleContributor.sequence)
                        .all()
                    )
                ],
            }

            safe_filename = _safe_filename(article.page_start, article.title)

            (out_path / safe_filename).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            exported += 1

        # Write index file for the viewer
        index = []
        for article in articles:
            xref_count = (
                session.query(CrossReference)
                .filter(CrossReference.article_id == article.id)
                .count()
            )
            resolved_count = (
                session.query(CrossReference)
                .filter(
                    CrossReference.article_id == article.id,
                    CrossReference.status == "resolved",
                )
                .count()
            )
            body = article.body or ""
            # First ~10 words of body for disambiguation in the index
            first_line = body.split("\n")[0]
            words = first_line.split()
            if len(words) > 10:
                body_start = " ".join(words[:10]) + "\u2026"
            else:
                body_start = " ".join(words)

            index.append({
                "title": article.title,
                "filename": _safe_filename(article.page_start, article.title),
                "volume": article.volume,
                "page_start": article.page_start,
                "page_end": article.page_end,
                "body_length": len(body.split()),
                "body_start": body_start,
                "xref_count": xref_count,
                "resolved_count": resolved_count,
            })

        # Merge with existing index (from other volumes)
        index_path = out_path / "index.json"
        if index_path.exists():
            existing = json.loads(index_path.read_text(encoding="utf-8"))
            # Remove entries for this volume, keep other volumes
            existing = [e for e in existing if e.get("volume") != volume]
            index = existing + index

        index_path.write_text(
            json.dumps(index, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Build contributor index
        contrib_map: dict[str, dict] = {}
        for article in articles:
            contribs = (
                session.query(Contributor)
                .join(ArticleContributor, ArticleContributor.contributor_id == Contributor.id)
                .filter(ArticleContributor.article_id == article.id)
                .order_by(ArticleContributor.sequence)
                .all()
            )
            for c in contribs:
                if c.full_name not in contrib_map:
                    contrib_map[c.full_name] = {
                        "full_name": c.full_name,
                        "initials": c.initials,
                        "articles": [],
                    }
                contrib_map[c.full_name]["articles"].append({
                    "title": article.title,
                    "filename": _safe_filename(article.page_start, article.title),
                })

        # Merge with existing contributors (from other volumes)
        contrib_path = out_path / "contributors.json"
        if contrib_path.exists():
            existing_contribs = json.loads(contrib_path.read_text(encoding="utf-8"))
            for ec in existing_contribs:
                name = ec["full_name"]
                if name in contrib_map:
                    # Merge article lists, avoiding duplicates
                    existing_fns = {a["filename"] for a in contrib_map[name]["articles"]}
                    for a in ec["articles"]:
                        if a["filename"] not in existing_fns:
                            contrib_map[name]["articles"].append(a)
                else:
                    contrib_map[name] = ec

        def _sort_name(c: dict) -> str:
            # Strip parenthetical dates, then sort by last name
            import re as _re
            name = _re.sub(r"\s*\([^)]*\)", "", c["full_name"]).strip()
            return name.rsplit(None, 1)[-1].lower()

        def _display_name(full_name: str) -> str:
            """Convert 'First Middle Last' to 'Last, First Middle'."""
            import re as _re
            name = _re.sub(r"\s*\([^)]*\)", "", full_name).strip()
            parts = name.rsplit(None, 1)
            if len(parts) == 2:
                return f"{parts[1]}, {parts[0]}"
            return name

        for entry in contrib_map.values():
            entry["display_name"] = _display_name(entry["full_name"])

        contrib_list = sorted(contrib_map.values(), key=_sort_name)
        contrib_path.write_text(
            json.dumps(contrib_list, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        return exported

    finally:
        session.close()