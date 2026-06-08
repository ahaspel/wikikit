"""Write diff-friendly snapshots of articles and cross-references.

Two outputs:
  data/derived/article_index.tsv — vol, page_start, page_end,
                                    printed_page_start, printed_page_end,
                                    article_type, title
  data/derived/xref_index.tsv    — source_vol, source_title, xref_type,
                                    surface_text, status, target_title

page_start/page_end are wikisource leaf numbers (the DB Article model
uses these).  printed_page_start/printed_page_end are the corresponding
human-readable printed page numbers from data/derived/printed_pages.json;
they're what readers see in the book and what we cite in bug reports.

Both sorted deterministically.  Run in the rebuild script (Phase 3d
and Phase 3e) after the article + xref pipeline stages finish, so
we always have a record of "what came out of this rebuild".

Commit both to git after every rebuild — `git log -p` then shows
article-list and xref churn over time, and we can identify which
specific articles or xrefs changed between any two rebuilds without
needing to re-run the pipeline.
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8") if hasattr(
    sys.stdout, "reconfigure") else None

from britannica.db.session import SessionLocal
from britannica.db.models import Article
from britannica.export.pages import _printed_page


def _pp(vol: int, leaf: int) -> str:
    try:
        return str(_printed_page(vol, leaf))
    except Exception:
        return ""


def main():
    out_articles = Path("data/derived/article_index.tsv")
    out_xrefs = Path("data/derived/xref_index.tsv")
    s = SessionLocal()
    try:
        articles = (s.query(Article)
                    .order_by(Article.volume, Article.page_start,
                              Article.title)
                    .all())
        with out_articles.open("w", encoding="utf-8") as f:
            f.write("vol\tpage_start\tpage_end\t"
                    "printed_page_start\tprinted_page_end\t"
                    "article_type\ttitle\n")
            for a in articles:
                pps = _pp(a.volume, a.page_start)
                ppe = _pp(a.volume, a.page_end)
                f.write(
                    f"{a.volume}\t{a.page_start}\t{a.page_end}\t"
                    f"{pps}\t{ppe}\t"
                    f"{a.article_type or 'article'}\t{a.title}\n")
        print(f"Wrote {len(articles)} articles → {out_articles}")

        # Xrefs live in the exported JSON now, not the DB.
        import glob
        import json
        rows = []
        for fp in sorted(glob.glob("data/derived/articles/*.json")):
            if fp.endswith(("index.json", "contributors.json")):
                continue
            rec = json.loads(Path(fp).read_text(encoding="utf-8"))
            for x in rec.get("xref_list", []):
                rows.append((
                    rec.get("volume", ""), rec.get("title", ""),
                    x.get("xref_type", ""), x.get("surface_text", ""),
                    x.get("status", ""), x.get("normalized_target", ""),
                ))
        rows.sort(key=lambda r: (str(r[0]), str(r[1]), str(r[2]), str(r[3])))
        with out_xrefs.open("w", encoding="utf-8") as f:
            f.write("source_vol\tsource_title\txref_type\tsurface_text\t"
                    "status\ttarget\n")
            for r in rows:
                f.write("\t".join(str(c) for c in r) + "\n")
        print(f"Wrote {len(rows)} xrefs → {out_xrefs}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
