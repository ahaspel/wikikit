"""Assemble the corpus in memory and serialize it — walk > assemble >
decorate > serialize, with no per-article DB body/xref/title writes.

Walks every article into an in-memory ``{id: body}`` corpus via
``walk_article``, builds the resolution index off that corpus, then
serializes each volume through the export's decorator path
(``body_override`` + ``link_index``).  This is the single pass that replaces the
``transform_articles`` → ``extract_xrefs`` → ``resolve_xrefs`` → export
chain: the body, the title, and every cross-link are produced once, held
in memory, and read straight into the JSON — the DB is never written.
"""
from __future__ import annotations

import json
from pathlib import Path

from britannica.contributors.link_frontmatter import link_from_frontmatter
from britannica.contributors.link_vol29_articles import link_vol29_articles
from britannica.db.models import Article, ArticleContributor, ContributorInitials
from britannica.db.session import SessionLocal
from britannica.export.article_json import (
    export_articles_to_json, register_stable_id_dedup)
from britannica.pipeline.stages.extract_contributors import (
    _harvest_signature_contributors,
)
from britannica.pipeline.stages.resolve_xrefs import build_resolution_index
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
    # Contributor harvest rides THIS walk (folds in the old Phase-2
    # extract-contributors re-walk): each walked body is scanned for its rendered
    # `(initials)` sign-offs and bound to the article.  Clean slate first — the old
    # stage assumed `rebuild_contributors` had truncated the table.
    session.query(ArticleContributor).delete()
    initials_map = {
        ci.initials: ci.contributor_id
        for ci in session.query(ContributorInitials).all()
    }
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
            # 5e on the 5b walk: bind the footer sign-offs scanned from this body.
            for seq, cid in enumerate(
                    _harvest_signature_contributors(body, initials_map),
                    start=1):
                session.add(ArticleContributor(
                    article_id=article.id, contributor_id=cid, sequence=seq))
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
        # Contributor LINKING folded in (was Phase 3b / 3b2): the harvest above
        # bound footer sign-offs; these bind the front-matter-table fallback and
        # the vol-29 master-Index attributions.  In-process, because the corpus is
        # in-memory and can't survive a hop out to separate CLI passes — they must
        # land before the export reads `ArticleContributor`.
        print("  [assemble] linking contributors (front-matter + vol-29)…",
              flush=True)
        link_from_frontmatter(apply_mode=True)
        link_vol29_articles(apply_mode=True)
        print("  [assemble] building cross-reference resolution index…", flush=True)
        idx = build_resolution_index(all_articles, corpus=corpus)
        volumes = ([only_volume] if only_volume is not None
                   else sorted({a.volume for a in all_articles}))
        print(f"  [export] exporting {len(volumes)} volume(s)…", flush=True)
        total = 0
        xref_rows: list = []
        for volume in volumes:
            n = export_articles_to_json(
                volume, out_dir,
                body_override=corpus,
                link_index=idx,
                xref_sink=xref_rows,
                # Phase F: defer xref resolution + baking + render to the
                # post-export resolve phase (tools/pipeline/resolve_xrefs_post.py,
                # rebuild phase 6b4), which can disambiguate against the kind
                # index.  [[project_resolver_consolidation]]
                defer_xrefs=True,
            )
            total += n
            print(f"  [export] vol {volume}: {n} articles ({total} total)", flush=True)

        # Persist the full resolution (resolved AND unresolved) as one diffable
        # snapshot — the unresolved half is otherwise discarded.  Lives beside
        # article_index.tsv (data/derived/, not the deployed articles dir), so it
        # is git-trackable for cross-rebuild resolution diffs.
        if only_volume is None:
            dump = Path(out_dir).parent / "xref_resolution.jsonl"
            with dump.open("w", encoding="utf-8") as fh:
                for row in xref_rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"  [export] persisted {len(xref_rows)} xrefs → {dump}", flush=True)
        return total
    finally:
        session.close()
