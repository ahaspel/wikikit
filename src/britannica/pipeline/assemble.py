"""Assemble the corpus in memory and serialize it — walk > assemble >
decorate > serialize, with no per-article DB body/xref/title writes.

Walks every article into an in-memory ``{id: body}`` corpus (plus a
parallel ``{id: title_display}`` map) via ``produce_article``, builds the
resolution index off that corpus, then serializes each volume through the
export's decorator path (``body_override`` + ``title_display_override`` +
``link_index``).  This is the single pass that replaces the
``transform_articles`` → ``extract_xrefs`` → ``resolve_xrefs`` → export
chain: the body, the title, and every cross-link are produced once, held
in memory, and read straight into the JSON — the DB is never written.
"""
from __future__ import annotations

from britannica.db.models import Article
from britannica.db.session import SessionLocal
from britannica.export.article_json import export_articles_to_json
from britannica.pipeline.stages.resolve_xrefs import build_resolution_index
from britannica.pipeline.stages.transform_articles import produce_article


def assemble_corpus(session):
    """Walk every article into in-memory ``(corpus, title_display)`` maps.

    Returns ``(all_articles, corpus, title_display)`` — the article rows
    (for the index), ``{id: body}``, and ``{id: title_display|None}``.
    """
    all_articles = session.query(Article).order_by(Article.id).all()
    corpus: dict[int, str] = {}
    title_display: dict[int, str | None] = {}
    for article in all_articles:
        body, disp = produce_article(session, article)
        corpus[article.id] = body
        title_display[article.id] = disp
    return all_articles, corpus, title_display


def assemble_and_export(out_dir, only_volume: int | None = None) -> int:
    """Walk → assemble → decorate → serialize the whole corpus.

    Builds the in-memory corpus + resolution index once, then exports each
    volume off them.  No ``article.body`` / ``CrossReference`` reads.
    ``only_volume`` restricts which volumes are serialized (the corpus +
    index are always corpus-wide, since resolution spans volumes).
    """
    session = SessionLocal()
    try:
        all_articles, corpus, title_display = assemble_corpus(session)
        idx = build_resolution_index(all_articles, corpus=corpus)
        volumes = ([only_volume] if only_volume is not None
                   else sorted({a.volume for a in all_articles}))
        total = 0
        for volume in volumes:
            total += export_articles_to_json(
                volume, out_dir,
                body_override=corpus,
                link_index=idx,
                title_display_override=title_display,
            )
        return total
    finally:
        session.close()
