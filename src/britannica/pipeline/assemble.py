"""Assemble the corpus in memory and serialize it — walk > assemble >
decorate > serialize, with no per-article DB body/xref/title writes.

Walks every article into an in-memory ``{id: body}`` corpus (plus a
parallel ``{id: title_display}`` map) via ``walk_article``, builds the
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
from britannica.pipeline.stages.transform_articles import walk_article


def assemble_corpus(session):
    """Walk every article into in-memory ``(corpus, title_display)`` maps.

    Returns ``(all_articles, corpus, title_display)`` — the article rows
    (for the index), ``{id: body}``, and ``{id: title_display|None}``.
    """
    # NOTE: no order_by — must match resolve_xrefs_all's query order exactly,
    # because the collider tie-break (title_map first-wins) depends on the
    # order articles arrive in.  Imposing order_by(id) here silently reorders
    # every fuzzy-resolved collider (see NARSES (Roman General)).
    all_articles = session.query(Article).all()
    total = len(all_articles)
    print(f"  [assemble] walking {total} articles…", flush=True)
    corpus: dict[int, str] = {}
    title_display: dict[int, str | None] = {}
    for i, article in enumerate(all_articles, 1):
        body, disp = walk_article(session, article)
        corpus[article.id] = body
        title_display[article.id] = disp
        # article_type falls out of the body we just walked — no separate
        # classify pass: a plate stays a plate (boundary detection set it),
        # a non-empty body is an article, an empty one is front matter.
        if article.article_type != "plate":
            new_type = "article" if body.strip() else "front_matter"
            if article.article_type != new_type:
                article.article_type = new_type
        # Progress tick (flushed — corpus-export's stdout redirects to
        # rebuild.log, which block-buffers; without flush the whole walk is a
        # silent ~25-min black hole).
        if i % 2500 == 0 or i == total:
            print(f"  [assemble] walked {i}/{total}", flush=True)
    # MOVE 2: the title is produced in the transform (`walk_article` sets
    # `article.title`/`title_raw`/`title_display`), not at detection.  The body
    # and xrefs stay in-memory only, but `title` and `article_type` are real DB
    # fields the export + xref resolution read straight from the DB — so commit
    # them before they're read (covers the fast pipeline, which skips classify).
    # This folds in the old `classify_articles` stage, which re-walked the whole
    # corpus solely to recompute that one `article_type` bit.
    session.commit()
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
        print("  [assemble] building cross-reference resolution index…", flush=True)
        idx = build_resolution_index(all_articles, corpus=corpus)
        volumes = ([only_volume] if only_volume is not None
                   else sorted({a.volume for a in all_articles}))
        print(f"  [export] exporting {len(volumes)} volume(s)…", flush=True)
        total = 0
        for volume in volumes:
            n = export_articles_to_json(
                volume, out_dir,
                body_override=corpus,
                link_index=idx,
                title_display_override=title_display,
            )
            total += n
            print(f"  [export] vol {volume}: {n} articles ({total} total)", flush=True)
        return total
    finally:
        session.close()
