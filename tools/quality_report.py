#!/usr/bin/env python3
"""Comprehensive quality report with before/after comparison.

Runs both DB-level and file-level quality checks, saves results to
data/derived/quality_reports/, and diffs against the previous run.
"""
import json
import re
import glob
import sys
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPORT_DIR = Path("data/derived/quality_reports")


def run_db_checks() -> dict:
    """Database-level quality checks."""
    from britannica.db.models import (
        Article, ArticleSegment, CrossReference, SourcePage,
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

        results = {}

        # Article count
        results["total_articles"] = len(articles)

        # Length distribution
        length_buckets = Counter()
        for a in articles:
            wc = len((a.body or "").split())
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
        results["titles_with_lowercase"] = sum(
            1 for a in articles if re.search(r"[a-z]", a.title)
        )
        results["titles_1_char"] = sum(1 for a in articles if len(a.title) == 1)

        # Body starts lowercase (almost always legitimate — encyclopedia
        # definitions continue from the bold title)
        results["lowercase_body_total"] = sum(
            1 for a in articles if (a.body or "") and (a.body or "")[0].islower()
        )

        # Embedded bold headings
        bold_open = "\u00abB\u00bb"
        bold_close = "\u00ab/B\u00bb"
        embedded = 0
        for a in articles:
            body = a.body or ""
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

        # Cross-references
        resolved = session.query(CrossReference).filter(
            CrossReference.status == "resolved"
        ).count()
        unresolved = session.query(CrossReference).filter(
            CrossReference.status == "unresolved"
        ).count()
        results["xrefs_resolved"] = resolved
        results["xrefs_unresolved"] = unresolved

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
    """File-level quality checks on exported articles."""
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
        if not title:
            continue
        if not body:
            continue

        # Stray wiki markup
        if "{{" in body and not any(
            m in body for m in ["{{IMG:", "{{TABLE", "{{FN:", "{{VERSE:"]
        ):
            issues["stray_braces"] += 1
        if "}}" in body and "TABLE}" not in body and "IMG:" not in body and "VERSE}" not in body:
            issues["stray_close_braces"] += 1
        if re.search(r"\[\[.*?\]\]", body):
            issues["stray_wikilink"] += 1
        # Stray wiki italic — exclude occurrences inside MATH markers
        body_no_math = re.sub(r"\u00abMATH:.*?\u00ab/MATH\u00bb", "", body, flags=re.DOTALL)
        if "''" in body_no_math:
            issues["stray_wiki_italic"] += 1

        # Bare HTML tags — exclude single-letter "tags" that are really
        # math comparisons (a<b, x<n) and OCR artifacts
        bare_body_for_html = re.sub(r"\u00abMATH:.*?\u00ab/MATH\u00bb", "", body, flags=re.DOTALL)
        if re.search(r"<(?:table|tr|td|th|div|span|br|sub|sup|ref|poem|score|math)\b[^>]*>",
                      bare_body_for_html, re.I):
            issues["html_tag"] += 1

        # Unclosed markers
        if body.count("\u00abFN:") != body.count("\u00ab/FN\u00bb"):
            issues["unclosed_footnote"] += 1
        if body.count("{{TABLE") != body.count("}TABLE}"):
            issues["unclosed_table"] += 1

        # Very short body
        words = len(body.split())
        if words < 5 and a.get("article_type") == "article":
            issues["tiny_article"] += 1

        # Pipe leaks — tabular data that should be inside TABLE markers.
        # Strip tables, verses, and math before checking.
        bare_body = re.sub(
            r"\{\{TABLE.*?\}TABLE\}", "", body, flags=re.DOTALL
        )
        bare_body = re.sub(
            r"\{\{VERSE:.*?\}VERSE\}", "", bare_body, flags=re.DOTALL
        )
        bare_body = re.sub(
            r"\u00abMATH:.*?\u00ab/MATH\u00bb", "", bare_body, flags=re.DOTALL
        )
        # Only count lines that start with | or || (table row pattern),
        # or have figure|image leaked markup
        pipe_lines = sum(
            1 for line in bare_body.split("\n")
            if re.match(r"\s*\|{1,2}\s*\S", line)
            or "figure |" in line or "figure|" in line
        )
        if pipe_lines > 3:
            issues["pipe_leak"] += 1

        # Stray control characters (\x01 = page markers, \x02 = tables)
        for i in range(9):
            if chr(i) in body and i not in (1, 2):
                issues[f"stray_control_x0{i}"] += 1
                break

        # Leaked template names
        if re.search(r"nowrap|colspan|rowspan|cellpadding", body):
            issues["leaked_html_attr"] += 1

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

    lines.append("=== File-Level Issues ===")
    for issue, count in files.get("issues", {}).items():
        lines.append(f"  {issue:30s} {count:6d}")
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
