from britannica.db.models import Article, CrossReference
from britannica.xrefs.scoring import find_fuzzy_match


def resolve_xref_exact(xref: CrossReference, articles: list[Article]) -> int | None:
    target = xref.normalized_target.strip().upper()

    for article in articles:
        if article.article_type == "plate":
            continue
        if article.title.strip().upper() == target:
            return article.id

    return None


def resolve_xref_fuzzy(
    xref: CrossReference, title_map: dict[str, int]
) -> int | None:
    target = xref.normalized_target.strip().upper()
    return find_fuzzy_match(target, title_map)