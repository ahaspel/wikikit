import json
import re
from pathlib import Path

from britannica.db.models import (
    Article, ArticleContributor, Contributor, ContributorInitials, SourcePage,
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


def _clean_footer_initials(initials: str) -> list[str]:
    """Clean and split footer initials string.

    Handles compound entries (E. H. P.; X.), brackets, crosses,
    leaked markup, and HTML entities. Returns a list of clean
    initials strings (excluding anonymous X. markers).
    """
    s = initials.strip()
    # Strip brackets, parentheses, and crosses
    s = s.strip("[]()").lstrip("✠").strip()
    # Decode HTML entities
    s = s.replace("&thinsp;", "").replace("&nbsp;", " ")
    # Unwrap wiki templates (e.g. {{small-caps|He}} → He)
    s = re.sub(r"\{\{[^{}|]*\|([^{}]*)\}\}", r"\1", s)
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\{\{[^{}]*", "", s)
    s = re.sub(r"\}\}", "", s)
    # Split on semicolons (compound entries like "E. H. P.; X.")
    parts = [p.strip() for p in s.split(";")]
    # Discard anonymous markers
    parts = [p for p in parts if p and p not in ("X.", "X")]
    return parts


def _parse_contributors(template_content: str) -> list[dict[str, str]]:
    """Parse contributor names and initials from a footer template."""
    results = []
    parts = template_content.split("|")

    # First contributor: positional args (skip font-size like "108%")
    positional = [p.strip() for p in parts if "=" not in p and "%" not in p]
    if len(positional) >= 2:
        for clean_init in _clean_footer_initials(positional[1]):
            results.append({
                "full_name": positional[0],
                "initials": clean_init,
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
            for clean_init in _clean_footer_initials(named[init_key]):
                results.append({
                    "full_name": named[name_key],
                    "initials": clean_init,
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


def _normalize_initials(initials: str) -> str:
    """Normalize initials for matching: strip markup, normalize spacing."""
    import re
    # Strip leaked wiki/HTML markup
    s = re.sub(r"\{\{[^{}]*", "", initials)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\}\}", "", s)
    # Normalize asterisk placement: "O*.", "O. *", "O.*" → "O.*"
    s = re.sub(r"\*\s*\.", ".*", s)
    s = re.sub(r"\.\s*\*", ".*", s)
    # Deduplicate repeated punctuation
    s = re.sub(r"\*+", "*", s)
    s = re.sub(r"\.+", ".", s)
    # Normalize spacing: "A.N." → "A. N.", but keep ".*" together
    s = re.sub(r"\.([A-Za-z])", r". \1", s)
    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


_initials_cache: dict[str, Contributor | None] = {}


def _find_contributor(session, initials: str) -> Contributor | None:
    """Find a contributor by initials via the contributor_initials table.

    The contributor table is pre-built from front matter. This function
    only looks up — it never creates records. Uses normalization as
    a fallback when exact match fails.
    """
    norm = _normalize_initials(initials)

    if norm in _initials_cache:
        return _initials_cache[norm]

    # Try exact match against contributor_initials table
    ci = (
        session.query(ContributorInitials)
        .filter(ContributorInitials.initials == initials)
        .first()
    )
    if ci:
        contributor = session.query(Contributor).get(ci.contributor_id)
        _initials_cache[norm] = contributor
        return contributor

    # Try normalized match
    for ci in session.query(ContributorInitials).all():
        if _normalize_initials(ci.initials) == norm:
            contributor = session.query(Contributor).get(ci.contributor_id)
            _initials_cache[norm] = contributor
            return contributor

    _initials_cache[norm] = None
    return None


def extract_contributors_for_volume(volume: int) -> int:
    session = SessionLocal()

    try:
        pages = (
            session.query(SourcePage)
            .filter(SourcePage.volume == volume)
            .order_by(SourcePage.page_number)
            .all()
        )

        # Build map: source_page_id -> article_id
        # Contributor footers appear at the end of articles, so when
        # multiple articles share a page, prefer the one ending there
        # (its footer is what we're extracting), not the one starting.
        articles = (
            session.query(Article)
            .filter(Article.volume == volume)
            .order_by(Article.page_start)
            .all()
        )
        page_articles: dict[int, int] = {}
        for page in pages:
            # Find all articles spanning this page
            candidates = [a for a in articles
                          if a.page_start <= page.page_number <= a.page_end]
            if candidates:
                # Prefer the article with the most content on this page:
                # the one that started earliest (its footer is at page bottom)
                page_articles[page.id] = min(candidates, key=lambda a: a.page_start).id

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
                    contributor = _find_contributor(
                        session, contrib["initials"]
                    )
                    if not contributor:
                        continue

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
