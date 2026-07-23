#!/usr/bin/env python3
"""Comprehensive quality report with before/after comparison.

Runs both DB-level and file-level quality checks, saves results to
data/derived/quality_reports/, and diffs against the previous run.

The leak signal is ONE honest number: `find_render_leaks` over each article's
`rendered_html` (`render_leak_*`).  The old body-level `stray_*` heuristics were
retired — they read the pre-render marker stream, so they conflated legit content
with leaks (prime `a'''` as italic, math `}}` as a stray brace, `<sub>` chem as a
tag) AND missed what the render actually emits.  Alongside the oracle, a few
structural-integrity checks (marker imbalance, dropped bodies) catch producer
bugs that don't surface as visible output residue.
"""
import json
import re
import glob
import sys
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from britannica.render.leaks import find_render_leaks  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

REPORT_DIR = Path("data/derived/quality_reports")


def run_db_checks() -> dict:
    """Database-level quality checks."""
    from britannica.db.models import (
        Article, ArticleSegment, SourcePage,
    )
    from britannica.db.session import SessionLocal

    session = SessionLocal()
    try:
        articles = (
            session.query(Article)
            .filter(Article.article_type == "article")
            .order_by(Article.volume, Article.page_start)
            .all()
        )

        # Body + xrefs now live in the exported JSON, not the DB.
        import glob
        _recs = [
            json.loads(open(_f, encoding="utf-8").read())
            for _f in glob.glob("data/derived/articles/*.json")
            if not _f.endswith(("index.json", "contributors.json"))
        ]
        bodies = {r["id"]: (r.get("body") or "") for r in _recs}
        # Xref totals come from the export's index.json aggregate fields:
        # `xref_count` (total found in the body) and `resolved_count`.  The
        # per-article files carry ONLY the resolved entries (key "xrefs",
        # filtered to status=="resolved" in article_json.py) — unresolved is
        # recoverable only as (total − resolved) from the index.  The old
        # `r.get("xref_list", …)` read a field that never existed in the
        # export, so both counts silently sat at zero.
        _xr_resolved = 0
        _xr_unresolved = 0
        _index_path = "data/derived/articles/index.json"
        if os.path.exists(_index_path):
            _index = json.loads(open(_index_path, encoding="utf-8").read())
            for _e in _index:
                _total = _e.get("xref_count", 0)
                _res = _e.get("resolved_count", 0)
                _xr_resolved += _res
                _xr_unresolved += max(0, _total - _res)

        results = {}

        # Article count
        results["total_articles"] = len(articles)

        # Length distribution
        length_buckets = Counter()
        for a in articles:
            wc = len(bodies.get(a.id, "").split())
            if wc == 0:
                length_buckets["0_empty"] += 1
            elif wc <= 5:
                length_buckets["1-5_words"] += 1
            elif wc <= 20:
                length_buckets["6-20_words"] += 1
            elif wc <= 100:
                length_buckets["21-100_words"] += 1
            elif wc <= 500:
                length_buckets["101-500_words"] += 1
            elif wc <= 2000:
                length_buckets["501-2000_words"] += 1
            elif wc <= 10000:
                length_buckets["2001-10000_words"] += 1
            else:
                length_buckets["10001+_words"] += 1
        results["length_distribution"] = dict(sorted(length_buckets.items()))

        # Title issues
        results["titles_with_period"] = sum(1 for a in articles if "." in a.title)

        # Lowercase check: ignore legitimate sources of lowercase —
        # parenthetical aliases ("(or Mapes)"), bracketed alternates
        # ("[Ælfheah]"), and Mc/Mac/De/La/Di/Van/O' name prefixes.
        def _has_stray_lowercase(title):
            stripped = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", title)
            stripped = re.sub(
                r"\b(?:Mc|Mac|De|La|Di|Van|O’|O')[A-Z]\w*", "",
                stripped)
            return bool(re.search(r"[a-z]", stripped))
        results["titles_with_lowercase"] = sum(
            1 for a in articles if _has_stray_lowercase(a.title))
        results["titles_1_char"] = sum(1 for a in articles if len(a.title) == 1)

        # Body starts lowercase (almost always legitimate — encyclopedia
        # definitions continue from the bold title)
        results["lowercase_body_total"] = sum(
            1 for a in articles
            if bodies.get(a.id, "") and bodies[a.id][0].islower()
        )

        # Embedded bold headings
        bold_open = "«B»"
        bold_close = "«/B»"
        embedded = 0
        for a in articles:
            body = bodies.get(a.id, "")
            search_body = body[20:] if len(body) > 20 else ""
            for m in re.finditer(
                re.escape(bold_open) + r"([A-Z][A-Z\s,.\-']+)" + re.escape(bold_close),
                search_body,
            ):
                candidate = m.group(1).strip().rstrip(",.")
                if len(candidate) < 3:
                    continue
                before = search_body[max(0, m.start() - 5):m.start()]
                if before and not before.rstrip().endswith(("\n", ".")):
                    continue
                embedded += 1
                break
        results["embedded_bold_headings"] = embedded

        # Cross-references (from the exported JSON)
        results["xrefs_resolved"] = _xr_resolved
        results["xrefs_unresolved"] = _xr_unresolved

        # Uncovered pages (mid-volume only)
        uncovered_mid = 0
        for vol in sorted(set(a.volume for a in articles)):
            pages = session.query(SourcePage).filter(
                SourcePage.volume == vol
            ).all()
            covered = set(
                seg.source_page_id for seg in
                session.query(ArticleSegment.source_page_id)
                .join(Article, ArticleSegment.article_id == Article.id)
                .filter(Article.volume == vol)
                .all()
            )
            first_art = min(
                (a.page_start for a in articles if a.volume == vol),
                default=9999,
            )
            for p in pages:
                if p.id not in covered and p.page_number >= first_art:
                    # Skip pages with only headers/footers (<200 chars)
                    text = (p.wikitext or "").strip()
                    if len(text) < 200:
                        continue
                    uncovered_mid += 1
        results["uncovered_pages_mid_volume"] = uncovered_mid

        # Per-volume counts
        vol_counts = Counter(a.volume for a in articles)
        results["per_volume"] = {str(v): vol_counts[v] for v in sorted(vol_counts)}

        return results
    finally:
        session.close()


def run_file_checks() -> dict:
    """File-level quality checks on exported articles.

    Two families:
      * ``render_leak_*`` — the HONEST leak oracle, `find_render_leaks` over the
        actual `rendered_html`.  This is the single leak number; it replaces the
        retired body-level `stray_*` heuristics (see the module docstring).
      * structural integrity — an unbalanced `«FN»`/`«TABLE»` marker, a dropped
        (tiny) body, a stray control char, wikitable rows escaped from `«TABLE»`.
        These are producer bugs that need NOT surface as visible output residue,
        so the render-leak oracle can't see them.
    """
    files = sorted(
        f for f in glob.glob("data/derived/articles/*.json")
        if "index.json" not in f and "contributors.json" not in f
    )

    results = {"files_scanned": len(files)}
    issues = Counter()

    for f in files:
        with open(f, encoding="utf-8") as fh:
            a = json.load(fh)
        title = a.get("title", "")
        body = a.get("body", "")
        if not title or not body:
            continue

        # ── Structural integrity — producer bugs that need not show as output. ──
        # `«FN:` (unnamed) / `«FN[NAME]:` (named, from `<ref name=X>`) share the
        # `«/FN»` closer, so count any `«FN` opener regardless of suffix.
        fn_opens = len(re.findall(r"«FN(?:\[[^\]]+\])?:", body))
        if fn_opens != body.count("«/FN»"):
            issues["unclosed_footnote"] += 1
        if body.count("{{TABLE") != body.count("}TABLE}"):
            issues["unclosed_table"] += 1
        if len(body.split()) < 5 and a.get("article_type") == "article":
            issues["tiny_article"] += 1
        # Stray control chars (\x01 = page markers, \x02 = tables are legitimate).
        for _i in range(9):
            if chr(_i) in body and _i not in (1, 2):
                issues[f"stray_control_x0{_i}"] += 1
                break
        # Pipe leaks — wikitable rows that escaped their `«TABLE»` wrapper.  Strip
        # the legit block markers, then count line-initial `|`/`||` rows.
        bare = re.sub(r"\{\{TABLE.*?\}TABLE\}", "", body, flags=re.DOTALL)
        bare = re.sub(r"\{\{VERSE:.*?\}VERSE\}", "", bare, flags=re.DOTALL)
        bare = re.sub(r"«MATH:.*?«/MATH»", "", bare, flags=re.DOTALL)
        if sum(1 for ln in bare.split("\n")
               if re.match(r"\s*\|{1,2}\s*\S", ln) or "figure|" in ln) > 3:
            issues["pipe_leak"] += 1

        # ── The leak signal: the HONEST oracle over the ACTUAL rendered output. ──
        for _cat in {_c for _c, _ in find_render_leaks(a.get("rendered_html", ""))}:
            issues[f"render_leak_{_cat}"] += 1

    results["issues"] = dict(issues.most_common())
    return results


def format_report(db: dict, files: dict) -> str:
    """Format results as readable text."""
    lines = []
    lines.append(f"Quality Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 60)
    lines.append("")

    lines.append("=== Summary ===")
    lines.append(f"  Articles: {db['total_articles']}")
    lines.append(
        f"  Xrefs: {db['xrefs_resolved']} resolved, "
        f"{db['xrefs_unresolved']} unresolved "
        f"({100 * db['xrefs_resolved'] / max(1, db['xrefs_resolved'] + db['xrefs_unresolved']):.0f}%)"
    )
    lines.append(f"  Files scanned: {files['files_scanned']}")
    lines.append("")

    lines.append("=== Article Length Distribution ===")
    for bucket, count in db["length_distribution"].items():
        lines.append(f"  {bucket:25s} {count:6d}")
    lines.append("")

    lines.append("=== Title Issues ===")
    lines.append(f"  Titles with period:    {db['titles_with_period']}")
    lines.append(f"  Titles with lowercase: {db['titles_with_lowercase']}")
    lines.append(f"  Single-char titles:    {db['titles_1_char']}")
    lines.append("")

    lines.append("=== Body Issues ===")
    lines.append(f"  Lowercase body starts:          {db['lowercase_body_total']}")
    lines.append(f"  Embedded bold headings:         {db['embedded_bold_headings']}")
    lines.append(f"  Uncovered pages (mid-volume):   {db['uncovered_pages_mid_volume']}")
    lines.append("")

    # Split the file-level issues: the render-leak oracle (the leak signal) first,
    # then structural-integrity checks — no noisy body-level heuristics remain.
    all_issues = files.get("issues", {})
    leaks = {k: v for k, v in all_issues.items() if k.startswith("render_leak_")}
    structural = {k: v for k, v in all_issues.items() if not k.startswith("render_leak_")}

    lines.append("=== Render Leaks (honest oracle — articles with ANY residue in rendered_html) ===")
    if leaks:
        for issue, count in sorted(leaks.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {issue:30s} {count:6d}")
    else:
        lines.append("  (none — clean)")
    lines.append("")

    lines.append("=== Structural Integrity ===")
    if structural:
        for issue, count in sorted(structural.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {issue:30s} {count:6d}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("=== Per-Volume Article Counts ===")
    for vol, count in db.get("per_volume", {}).items():
        lines.append(f"  Vol {vol:>2s}: {count:5d}")

    return "\n".join(lines)


def diff_reports(current: dict, previous: dict) -> str:
    """Generate a comparison between two report data dicts."""
    lines = []
    lines.append("")
    lines.append("=== Changes from Previous Build ===")

    def _diff(label, cur, prev):
        delta = cur - prev
        if delta == 0:
            return
        arrow = "▲" if delta > 0 else "▼"
        lines.append(f"  {label:35s} {prev:>6d} → {cur:>6d}  {arrow}{abs(delta)}")

    cur_db = current["db"]
    prev_db = previous["db"]

    _diff("Articles", cur_db["total_articles"], prev_db["total_articles"])
    _diff("Xrefs resolved", cur_db["xrefs_resolved"], prev_db["xrefs_resolved"])
    _diff("Xrefs unresolved", cur_db["xrefs_unresolved"], prev_db["xrefs_unresolved"])
    _diff("Titles with period", cur_db["titles_with_period"], prev_db["titles_with_period"])
    _diff("Titles with lowercase", cur_db["titles_with_lowercase"], prev_db["titles_with_lowercase"])
    _diff("Embedded bold headings", cur_db["embedded_bold_headings"], prev_db["embedded_bold_headings"])
    _diff("Uncovered pages (mid-volume)", cur_db["uncovered_pages_mid_volume"], prev_db["uncovered_pages_mid_volume"])

    cur_issues = current["files"].get("issues", {})
    prev_issues = previous["files"].get("issues", {})
    all_issues = sorted(set(cur_issues) | set(prev_issues))
    for issue in all_issues:
        _diff(f"[file] {issue}", cur_issues.get(issue, 0), prev_issues.get(issue, 0))

    if len(lines) == 2:
        lines.append("  (no changes)")

    return "\n".join(lines)


def main():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    print("Running DB checks...")
    db_results = run_db_checks()

    print("Running file checks...")
    file_results = run_file_checks()

    # Format and print
    report_text = format_report(db_results, file_results)
    print()
    print(report_text)

    # Save data for comparison
    current = {
        "timestamp": datetime.now().isoformat(),
        "db": db_results,
        "files": file_results,
    }

    # Find previous report
    prev_files = sorted(REPORT_DIR.glob("report_*.json"))
    if prev_files:
        previous = json.loads(prev_files[-1].read_text(encoding="utf-8"))
        diff_text = diff_reports(current, previous)
        print(diff_text)
        report_text += "\n" + diff_text

    # Save current report
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    data_path = REPORT_DIR / f"report_{ts}.json"
    text_path = REPORT_DIR / f"report_{ts}.txt"

    data_path.write_text(
        json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    text_path.write_text(report_text, encoding="utf-8")

    print(f"\nSaved to {data_path} and {text_path}")


if __name__ == "__main__":
    main()
