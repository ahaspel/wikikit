"""Phase 6b5: resolve inline xrefs POST-EXPORT.

With ``defer_xrefs`` the export writes each article's body with its raw producer
markers (``«LN:target|display»`` / ``«EB9»``) and no ``rendered_html``.  This
phase does the REORDER: it runs the SAME decoration + render the export used to
do inline — ``_xrefs_from_body`` → ``xref_panel_entries`` → ``_link_xrefs_in_body``
→ ``render_article`` — but NOW, after the topic resolution + kind index + the
classified TOC exist.  Resolution routes through the shared ``LinkResolver``
(fill + prose-fish; the see tier filters on the source article's TOC topics) —
docs/xref_resolution_strategy.md, [[project_resolver_consolidation]].

Sole (re)writer of `xref_resolution.jsonl`; patches each article JSON in place
(body, word_count, xrefs panel, rendered_html).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
from britannica.db.models import Article
from britannica.db.session import SessionLocal
from britannica.export.article_json import (
    _link_xrefs_in_body, _safe_filename, _xrefs_from_body,
    register_stable_id_dedup, xref_panel_entries,
)
from britannica.link_resolver import LinkResolver
from britannica.render.article import render_article

ART = Path("data/derived/articles")
_SKIP = {"index.json", "contributors.json"}


def resolve_and_render(session, payloads: dict) -> int:
    """Resolve every article's xrefs, bake the links, render — IN MEMORY.

    The phase as a transform over the loaded corpus (the caller owns load/write),
    so the merged post-export pass applies it without its own corpus round-trip.
    Writes ``xref_resolution.jsonl`` (it is the sole writer of that snapshot).
    Returns the number of articles processed.

    NOTE the caller must have replayed ``register_stable_id_dedup`` before any
    ``_safe_filename`` call — otherwise a BOG/BOGÓ-type pair drops its -N suffix
    and bakes a dangling ``resolved_to``.
    """
    all_articles = session.query(Article).all()
    # The ONE name→article resolver (reads the exported index.json — same
    # filename space as _safe_filename post-dedup); fn_to_id maps its picks
    # back to DB ids for the resolved Xref values.
    resolver = LinkResolver(aliases=True)
    fn_to_id = {_safe_filename(a, a.title): a.id for a in all_articles
                if a.article_type != "plate"}
    g2f = {a.title.upper(): _safe_filename(a, a.title) for a in all_articles}

    xref_rows: list = []
    n = 0
    for d in payloads.values():
        article = session.get(Article, d["id"])
        if article is None:
            continue
        body = d["body"]
        xrefs = _xrefs_from_body(body, article.id, resolver,
                                 fn_to_id=fn_to_id,
                                 self_fn=d["stable_id"] + ".json")
        xref_list = xref_panel_entries(xrefs, session)
        src = d["stable_id"]
        for e in xref_list:
            xref_rows.append({
                "source": src, "surface": e["surface_text"],
                "target": e["normalized_target"], "type": e["xref_type"],
                "status": e["status"], "resolved_to": e.get("target_filename"),
                **({"section": e["target_section"]}
                   if e.get("target_section") else {}),
            })
        body = _link_xrefs_in_body(body, xrefs, src, session, g2f)
        d["body"] = body
        d["word_count"] = len(body.split())
        d["xrefs"] = [e for e in xref_list if e["status"] == "resolved"]
        d["rendered_html"] = render_article(d, is_local=False, target="site")
        n += 1
        if n % 5000 == 0:
            print(f"  [xrefs] resolved + rendered {n} articles…", flush=True)

    dump = ART.parent / "xref_resolution.jsonl"
    with dump.open("w", encoding="utf-8") as fh:
        for row in xref_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Resolved xrefs for {n} articles; {len(xref_rows)} xrefs → {dump}")
    return n


def main() -> None:
    """Standalone: load the corpus, resolve + render, write back."""
    from britannica.export.corpus import load_corpus, write_corpus
    session = SessionLocal()
    try:
        register_stable_id_dedup(session.query(Article).all())
        payloads, _ = load_corpus(ART)
        resolve_and_render(session, payloads)
        write_corpus(payloads)
    finally:
        session.close()


if __name__ == "__main__":
    main()
