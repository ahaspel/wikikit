import json
import re
from pathlib import Path

from britannica.db.models import (
    Article, ArticleContributor, ArticleSegment, Contributor, SourcePage,
)
from britannica.db.session import SessionLocal


_RAW_DIRS = [
    Path("data/raw/wikisource"),
]

# Matches: {{EB1911 footer initials|Full Name|Initials|name2=Name2|initials2=Init2}}
_FOOTER_PATTERN = re.compile(
    r"\{\{EB1911 footer initials\|([^}]+)\}\}",
    re.IGNORECASE,
)


def _parse_contributors(template_content: str) -> list[dict[str, str]]:
    """Parse contributor names and initials from a footer template."""
    results = []
    parts = template_content.split("|")

    # First contributor: positional args (skip font-size like "108%")
    positional = [p.strip() for p in parts if "=" not in p and "%" not in p]
    if len(positional) >= 2:
        results.append({
            "full_name": positional[0],
            "initials": positional[1],
        })

    # Additional contributors: name2=...|initials2=..., name3=..., etc.
    named = {}
    for part in parts:
        if "=" in part:
            key, _, value = part.partition("=")
            named[key.strip()] = value.strip()

    for n in range(2, 10):
        name_key = f"name{n}"
        init_key = f"initials{n}"
        if name_key in named and init_key in named:
            results.append({
                "full_name": named[name_key],
                "initials": named[init_key],
            })

    return results


def _load_raw_wikitext(volume: int, page_number: int) -> str | None:
    """Load the original wikitext from the cached JSON file on disk."""
    padded = f"vol{volume:02d}-page{page_number:04d}.json"
    for raw_dir in _RAW_DIRS:
        for subdir in sorted(raw_dir.iterdir()) if raw_dir.exists() else []:
            if not subdir.is_dir():
                continue
            path = subdir / padded
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data.get("raw_text", "")
    return None


def _get_or_create_contributor(
    session, full_name: str, initials: str
) -> Contributor:
    """Find or create a contributor record."""
    existing = (
        session.query(Contributor)
        .filter(Contributor.initials == initials)
        .first()
    )
    if existing:
        return existing

    contributor = Contributor(initials=initials, full_name=full_name)
    session.add(contributor)
    session.flush()
    return contributor


def extract_contributors_for_volume(volume: int) -> int:
    session = SessionLocal()

    try:
        pages = (
            session.query(SourcePage)
            .filter(SourcePage.volume == volume)
            .order_by(SourcePage.page_number)
            .all()
        )

        # Build map: source_page_id -> article_id (last article on each page)
        page_articles: dict[int, int] = {}
        segments = (
            session.query(ArticleSegment)
            .join(Article, ArticleSegment.article_id == Article.id)
            .filter(Article.volume == volume)
            .order_by(ArticleSegment.source_page_id, ArticleSegment.sequence_in_article)
            .all()
        )
        for seg in segments:
            page_articles[seg.source_page_id] = seg.article_id

        created = 0

        for page in pages:
            article_id = page_articles.get(page.id)
            if article_id is None:
                continue

            raw = _load_raw_wikitext(volume, page.page_number)
            if not raw:
                continue

            for match in _FOOTER_PATTERN.finditer(raw):
                contributors = _parse_contributors(match.group(1))

                for i, contrib in enumerate(contributors):
                    contributor = _get_or_create_contributor(
                        session, contrib["full_name"], contrib["initials"]
                    )

                    existing = (
                        session.query(ArticleContributor)
                        .filter(
                            ArticleContributor.article_id == article_id,
                            ArticleContributor.contributor_id == contributor.id,
                        )
                        .first()
                    )
                    if existing:
                        continue

                    session.add(
                        ArticleContributor(
                            article_id=article_id,
                            contributor_id=contributor.id,
                            sequence=i + 1,
                        )
                    )
                    created += 1

        session.commit()
        return created

    finally:
        session.close()
