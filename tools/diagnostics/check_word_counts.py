"""Is every article's shipped `word_count` the count a reader sees?

    uv run python tools/diagnostics/check_word_counts.py

The field has ONE owner, `britannica.markers.countable_words`.  The export
computed it correctly from 2026-09-21, and it still shipped wrong: a later
phase, `resolve_xrefs_post.py`, recomputed it as `len(body.split())` after
rewriting the body, and every article left the 2026-09-26 rebuild with its old
marker-stream count.  Unit tests of `countable_words` passed throughout — they
tested the function, not the field the site renders.

So this checks the ARTIFACT: load the exported corpus and recount.  A mismatch
means some writer other than the owner touched the field after it was set.
Exit 1 on any mismatch; a gate that reports and carries on is not a gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from britannica.export.corpus import load_corpus    # noqa: E402
from britannica.markers import countable_words      # noqa: E402


def main() -> int:
    payloads, _ = load_corpus()
    wrong = []
    for path, article in payloads.items():
        expected = countable_words(article.get("body") or "")
        if article.get("word_count") != expected:
            wrong.append((path.stem, article.get("title", ""), article.get("word_count"), expected))
    print(f"WORD-COUNT GATE — {len(payloads)} articles")
    if not wrong:
        print("  OK: every shipped word_count is countable_words(body).")
        return 0
    print(f"  FAIL: {len(wrong)} articles ship a count the owner would not give.")
    for stem, title, have, want in wrong[:10]:
        print(f"    {stem}  {title[:30]:<30}  shipped {have}  owner {want}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
