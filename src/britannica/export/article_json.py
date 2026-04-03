import json
import re
from collections import Counter
from pathlib import Path

from britannica.db.models import (
    Article, ArticleContributor, ArticleImage, ArticleSegment,
    Contributor, CrossReference, SourcePage,
)
from britannica.db.session import SessionLocal


_QUALITY_NOTES = {
    0: "Untranscribed page",
    1: "Unproofread OCR text",
    2: "Problematic transcription",
}


_TITLE_PREFIXES = re.compile(
    r"^(Sir |Rev\.? |Colonel |Major-General |Lieut\.-Gen\. |"
    r"Right Hon\.? |The |Hon\.? |Rt\.? Rev\.? |Very Rev\.? |"
    r"Viscount |Lord |Rear-Admiral |Field-Marshal |Mrs |"
    r"Prince |Princess |Earl of |Baron |Dr\.? )+",
    re.IGNORECASE,
)


def _resolve_bio_articles(session, contrib_map: dict[str, dict]) -> None:
    """Add bio_article_filename to contributors with biographical articles."""
    # Build title -> filename lookup from all articles in DB
    all_articles = session.query(Article).all()
    title_map: dict[str, str] = {}
    for a in all_articles:
        title_map[a.title.upper()] = _safe_filename(a.page_start, a.title)

    for entry in contrib_map.values():
        desc = (entry.get("description") or "").lower()
        if "biographical article" not in desc:
            continue

        full_name = entry["full_name"]
        # Strip parenthetical dates
        clean = re.sub(r"\s*\([^)]*\)", "", full_name).strip()
        # Also strip trailing ordinals like "1st Baron Farnborough"
        clean = re.sub(r",?\s+\d+\w*\s+Baron\s+.*$", "", clean, flags=re.IGNORECASE).strip()
        # Strip titles/honorifics
        stripped = _TITLE_PREFIXES.sub("", clean).strip()

        parts = stripped.split()
        if not parts:
            continue

        last = parts[-1].upper()
        firsts = " ".join(parts[:-1]).upper()

        # Build candidate inversions: without and with honorifics
        candidates: list[str] = []
        if firsts:
            candidates.append(f"{last}, {firsts}")
            # Also try with "Sir", "Rev." etc. between last name and first
            title_match = _TITLE_PREFIXES.match(clean)
            if title_match:
                title = title_match.group(0).strip().upper()
                candidates.append(f"{last}, {title} {firsts}")
        else:
            candidates.append(last)

        fn = None
        for candidate in candidates:
            fn = title_map.get(candidate)
            if fn:
                break
            fn = next(
                (title_map[t] for t in title_map if t.startswith(candidate)),
                None,
            )
            if fn:
                break
        if not fn and len(parts) > 1:
            prefix = f"{last}, {parts[0].upper()}"
            fn = next(
                (title_map[t] for t in title_map if t.startswith(prefix)),
                None,
            )
        # Fallback: word-set containment (A⊆B or B⊆A)
        if not fn:
            name_words = {w.upper().rstrip(".,") for w in stripped.split()}
            for title, title_fn in title_map.items():
                # Only consider biographical articles (LAST, FIRST...)
                if "," not in title:
                    continue
                title_words = {w.rstrip(".,") for w in title.split()}
                if name_words <= title_words or title_words <= name_words:
                    fn = title_fn
                    break

        if fn:
            entry["bio_article_filename"] = fn


def _source_quality(session, article: Article) -> dict:
    """Build source quality metadata from page quality levels."""
    pages = (
        session.query(SourcePage)
        .filter(
            SourcePage.volume == article.volume,
            SourcePage.page_number >= article.page_start,
            SourcePage.page_number <= article.page_end,
        )
        .all()
    )
    levels = Counter()
    for page in pages:
        m = re.search(r'pagequality level="(\d)"', page.raw_text or "")
        level = int(m.group(1)) if m else 3  # default to proofread
        levels[level] += 1

    lowest = min(levels.keys()) if levels else 3
    note = _QUALITY_NOTES.get(lowest)

    return {
        "page_levels": {str(k): v for k, v in sorted(levels.items())},
        "lowest_level": lowest,
        "note": note,
    }


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

            quality = _source_quality(session, article)

            # For plates, find the parent article
            parent_article_info = None
            if article.article_type == "plate":
                parent = (
                    session.query(Article)
                    .filter(
                        Article.article_type == "article",
                        Article.volume == article.volume,
                        Article.page_start <= article.page_start,
                        Article.page_end >= article.page_start - 5,
                    )
                    .order_by(Article.page_start.desc())
                    .first()
                )
                if parent:
                    parent_article_info = {
                        "title": parent.title,
                        "filename": _safe_filename(parent.page_start, parent.title),
                    }

            # Resolve inline link markers: embed target filename for resolved xrefs
            # «LN:target|display«/LN» → «LN:filename|target|display«/LN»
            body = article.body or ""
            link_targets: dict[str, str] = {}  # normalized_target → filename
            for xref in xrefs:
                if xref.target_article_id is not None and xref.normalized_target:
                    target = session.get(Article, xref.target_article_id)
                    if target:
                        link_targets[xref.normalized_target.lower()] = _safe_filename(
                            target.page_start, target.title
                        )

            def _resolve_link(m: re.Match) -> str:
                target_text, display = m.group(1), m.group(2)
                fn = link_targets.get(target_text.strip().lower())
                if fn:
                    return f"\u00abLN:{fn}|{target_text}|{display}\u00ab/LN\u00bb"
                return m.group(0)  # leave unresolved as-is (2-part)

            body = re.sub(
                r"\u00abLN:([^|]*)\|([^\u00ab]*)\u00ab/LN\u00bb",
                _resolve_link,
                body,
            )

            payload = {
                "id": article.id,
                "title": article.title,
                "article_type": article.article_type,
                "volume": article.volume,
                "page_start": article.page_start,
                "page_end": article.page_end,
                "source_quality": quality,
                "parent_article": parent_article_info,
                "body": body,
                "segments": [
                    {
                        "sequence_in_article": seg.sequence_in_article,
                        "source_page_id": seg.source_page_id,
                        "page_number": (
                            session.get(SourcePage, seg.source_page_id).page_number
                            if seg.source_page_id else None
                        ),
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
                "plates": [
                    {
                        "title": plate.title,
                        "filename": _safe_filename(plate.page_start, plate.title),
                        "page": plate.page_start,
                    }
                    for plate in (
                        session.query(Article)
                        .filter(
                            Article.article_type == "plate",
                            Article.volume == article.volume,
                            Article.page_start >= article.page_start,
                            Article.page_start <= article.page_end + 5,
                        )
                        .order_by(Article.page_start)
                        .all()
                    )
                ],
                "contributors": [
                    {
                        "initials": contrib.initials,
                        "full_name": contrib.full_name,
                        "credentials": contrib.credentials,
                        "description": contrib.description,
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
            article_json = json.dumps(payload, indent=2, ensure_ascii=False)

            (out_path / safe_filename).write_text(article_json, encoding="utf-8")

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
                "id": article.id,
                "title": article.title,
                "article_type": article.article_type,
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
                        "credentials": c.credentials or "",
                        "description": c.description or "",
                        "articles": [],
                    }
                contrib_map[c.full_name]["articles"].append({
                    "id": article.id,
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

        # Resolve biographical article links for contributors
        _resolve_bio_articles(session, contrib_map)

        contrib_list = sorted(contrib_map.values(), key=_sort_name)
        contrib_path.write_text(
            json.dumps(contrib_list, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        return exported

    finally:
        session.close()