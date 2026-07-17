"""Phase 6b4: resolve inline xrefs POST-EXPORT.

With ``defer_xrefs`` the export writes each article's body with its raw producer
markers (``«LN:target|display»`` / ``«EB9»``) and no ``rendered_html``.  This
phase does the REORDER: it runs the SAME decoration + render the export used to
do inline — ``_xrefs_from_body`` → ``xref_panel_entries`` → ``_link_xrefs_in_body``
→ ``render_article`` — but NOW, after the topic resolution + kind index exist, so
it can disambiguate collisions against them.  Nothing is rewritten; only the call
site moves.  ([[project_resolver_consolidation]] F.)

Decoration + PAGE-marker replacement + `«LN»`-baking commute (disjoint body
spans), so with the standard resolution index this is byte-identical to the old
in-export decoration; the kind-aware index (C-full) changes only collisions.

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
from britannica.render.article import render_article
from britannica.xrefs.resolver import build_index

ART = Path("data/derived/articles")
_SKIP = {"index.json", "contributors.json"}


def main() -> None:
    session = SessionLocal()
    try:
        all_articles = session.query(Article).all()
        # Replay the corpus-wide stable_id collision-suffix assignment the
        # export made in-process (assemble.register_stable_id_dedup): this is a
        # separate process, so _STABLE_ID_SUFFIX starts empty and _safe_filename
        # would drop the -N suffix on a BOG/BOGÓ-type pair — baking a wrong
        # (dangling) resolved_to.  Must run before any _safe_filename call.
        register_stable_id_dedup(all_articles)
        # Load every article payload; the undecorated bodies double as the
        # disambiguation corpus (body_opening strips PAGE markers, so the
        # printed-page form here matches the in-memory ws form).
        payloads: dict[Path, dict] = {}
        corpus: dict[int, str] = {}
        for fn in ART.glob("*.json"):
            if fn.name in _SKIP:
                continue
            try:
                d = json.loads(fn.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(d, dict) and "id" in d and "body" in d:
                payloads[fn] = d
                corpus[d["id"]] = d["body"]

        link_index = build_index(all_articles, corpus=corpus)
        g2f = {a.title.upper(): _safe_filename(a, a.title) for a in all_articles}

        xref_rows: list = []
        n = 0
        for fn, d in payloads.items():
            article = session.get(Article, d["id"])
            if article is None:
                continue
            body = d["body"]
            xrefs = _xrefs_from_body(body, article.id, link_index)
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
            fn.write_text(json.dumps(d, indent=2, ensure_ascii=False),
                          encoding="utf-8")
            n += 1
            if n % 5000 == 0:
                print(f"  [6b4] resolved + rendered {n} articles…", flush=True)

        dump = ART.parent / "xref_resolution.jsonl"
        with dump.open("w", encoding="utf-8") as fh:
            for row in xref_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"Resolved xrefs for {n} articles; {len(xref_rows)} xrefs → {dump}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
