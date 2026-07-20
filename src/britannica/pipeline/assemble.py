"""Assemble the corpus in memory and serialize it — walk > assemble >
decorate > serialize, with no per-article DB body/xref/title writes.

Walks every article into an in-memory ``{id: body}`` corpus via
``walk_article``, then serializes each volume through the export
(``body_override``).  This is the single pass that replaces the
``transform_articles`` → ``extract_xrefs`` → ``resolve_xrefs`` → export
chain: the body and the title are produced once, held in memory, and read
straight into the JSON — the DB is never written.  Xref resolution +
«LN»-baking + render are DEFERRED wholesale to the post-export resolve phase
(``tools/pipeline/resolve_xrefs_post.py``), which runs after the classified
TOC + kind index exist and routes through the shared ``LinkResolver``.
"""
from __future__ import annotations

from britannica.db.models import Article
from britannica.db.session import SessionLocal
from britannica.export.article_json import (
    export_articles_to_json, register_stable_id_dedup)
from britannica.pipeline.stages.transform_articles import walk_article


def assemble_corpus(session):
    """Walk every article into the in-memory ``corpus`` map.

    Returns ``(all_articles, corpus)`` — the article rows (for the index)
    and ``{id: body}``.
    """
    # NOTE: no order_by — must match resolve_xrefs_all's query order exactly,
    # because the collider tie-break (title_map first-wins) depends on the
    # order articles arrive in.  Imposing order_by(id) here silently reorders
    # every fuzzy-resolved collider (see NARSES (Roman General)).
    all_articles = session.query(Article).all()
    total = len(all_articles)
    print(f"  [assemble] walking {total} articles…", flush=True)
    # ALL contributor binding moved OUT of the walk into one post-export phase
    # (Phase 6b5, tools/pipeline/resolve_contributors_post.py) that runs after the
    # kind index (6b3) and so can use each contributor's kind FOOTPRINT to
    # disambiguate vol-29 credits.  The walk keeps only what it uniquely owns —
    # the body and the article_type bit.  [[project_resolver_consolidation]]
    corpus: dict[int, str] = {}
    for i, article in enumerate(all_articles, 1):
        body = walk_article(session, article)
        corpus[article.id] = body
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
    # `article.title`), not at detection.  The body and xrefs stay in-memory
    # only, but `title` and `article_type` are real DB fields the export + xref
    # resolution read straight from the DB — so commit them before they're read
    # (covers the fast pipeline, which skips classify).  This folds in the old
    # `classify_articles` stage, which re-walked the whole corpus solely to
    # recompute that one `article_type` bit.
    session.commit()
    return all_articles, corpus


def assemble_and_export(out_dir, only_volume: int | None = None) -> int:
    """Walk → assemble → decorate → serialize the whole corpus.

    Builds the in-memory corpus + resolution index once, then exports each
    volume off them.  No ``article.body`` / ``CrossReference`` reads.
    ``only_volume`` restricts which volumes are serialized (the corpus +
    index are always corpus-wide, since resolution spans volumes).
    """
    session = SessionLocal()
    try:
        all_articles, corpus = assemble_corpus(session)
        # Assign deterministic stable_id collision suffixes ONCE, corpus-wide, BEFORE any
        # id / filename / «LN» / resolution-index baking reads stable_id.  With the hashed,
        # title-independent `{id}.json` key a same-slug pair (BOG/BOGÓ → both "bog") would
        # write to one file and silently drop an article; the suffix keeps both.
        n_dedup = register_stable_id_dedup(all_articles)
        print(f"  [assemble] stable_id dedup: {n_dedup} collision suffix(es)", flush=True)
        # Contributor binding (signatures + front-matter + vol-29) moved to the
        # post-export Phase 6b4 (resolve_contributors_post.py) — it runs after the
        # kind index (6b3), so it can footprint-disambiguate vol-29 credits.  The
        # export writes empty `contributors`; 6b4 fills them.
        volumes = ([only_volume] if only_volume is not None
                   else sorted({a.volume for a in all_articles}))
        print(f"  [export] exporting {len(volumes)} volume(s)…", flush=True)
        total = 0
        for volume in volumes:
            n = export_articles_to_json(
                volume, out_dir,
                body_override=corpus,
                # Phase F: defer xref resolution + baking + render to the
                # post-export resolve phase (tools/pipeline/resolve_xrefs_post.py,
                # rebuild phase 6b5), which resolves through the shared
                # LinkResolver and is the sole writer of xref_resolution.jsonl.
                # [[project_resolver_consolidation]]
                defer_xrefs=True,
            )
            total += n
            print(f"  [export] vol {volume}: {n} articles ({total} total)", flush=True)
        return total
    finally:
        session.close()
