from britannica.db.models import Article, CrossReference


def resolve_xref_exact(xref: CrossReference, articles: list[Article]) -> int | None:
    target = xref.normalized_target.strip().upper()

    for article in articles:
        if article.title.strip().upper() == target:
            return article.id

    return None