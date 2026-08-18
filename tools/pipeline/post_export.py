"""The post-export pass: ONE load, every corpus transform, ONE write.

The export writes each article's body with its raw producer markers and no
``rendered_html``; everything that needs corpus-wide knowledge then happens here,
AFTER the classified TOC (5.1) and the kind index (5.3) exist.

This used to be three phases — math annotation, contributors, xrefs +
render — each of which globbed all ~37k article JSONs, parsed them, and wrote
them all back.  Three full read+write cycles for one logical pass, with the
contributor phase's output re-read by the xref phase moments later, and three
separate replays of ``register_stable_id_dedup`` (a process-global the tools each
had to remember to prime before any ``_safe_filename`` call, or silently bake a
dangling filename).

Now each of those is a TRANSFORM over the in-memory corpus and this module owns
the I/O:

    load_corpus()  →  annotate math  →  bind contributors  →  resolve xrefs +
    render  →  write_corpus()

Order is the dependency order, not a preference: math hints must be on the body
before the render reads it; the contributor byline must be bound before the
render bakes it into ``rendered_html``; xref resolution wants the finished TOC
for its see-tier.  Each phase remains runnable alone (each module keeps a
``main()`` that loads and writes for itself) for targeted re-runs.

Loading is total — a payload that will not parse RAISES rather than being skipped
past, because a silently skipped article ships stale ([[feedback_honesty_surface_failures]]).
"""
import sys
import time

sys.path.insert(0, "src")
sys.path.insert(0, "tools/pipeline")

from britannica.db.models import Article
from britannica.db.session import SessionLocal
from britannica.export.article_json import register_stable_id_dedup
from britannica.export.corpus import ARTICLES_DIR, load_corpus, write_corpus
from britannica.table_widths import CACHE_PATH as TABLE_WIDTHS_CACHE

from annotate_math_markers import annotate_payloads
from annotate_table_markers import annotate_body
from resolve_contributors_post import bind_contributors
from resolve_xrefs_post import resolve_and_render


def main() -> None:
    t0 = time.time()

    def tick(label):
        print(f"  [post-export] {label} [{time.time() - t0:.0f}s]", flush=True)

    session = SessionLocal()
    try:
        # Prime the corpus-wide stable_id collision suffixes ONCE for the whole
        # pass (a process-global that every _safe_filename call below depends on;
        # the three old phases each replayed it in their own process).
        register_stable_id_dedup(session.query(Article).all())

        payloads, _ = load_corpus(ARTICLES_DIR)
        tick(f"loaded {len(payloads)} articles")

        changed, with_math = annotate_payloads(payloads)
        tick(f"math markers: {changed} re-hinted / {with_math} with math")

        wrote = bind_contributors(session, payloads)
        tick("contributors bound")
        if not wrote:                      # STEP5_DRYRUN — write nothing
            print("  [post-export] dry run — no JSONs written")
            return

        # Table wide-hints decorate the FINAL baked body inside the resolve →
        # render loop: the width cache is keyed on span bytes, and a span with
        # unresolved «LN» targets hashes differently — annotating any earlier
        # silently misses every linky table (see resolve_and_render's doc).
        import json as _json
        widths = (_json.loads(TABLE_WIDTHS_CACHE.read_text(encoding="utf-8"))
                  if TABLE_WIDTHS_CACHE.exists() else {})
        wide_stats: dict = {}
        resolve_and_render(
            session, payloads,
            decorate=lambda body, d: annotate_body(
                body, widths, stats=wide_stats,
                plate=d.get("article_type") == "plate"))
        tick("xrefs resolved + tables hinted + rendered")
        if wide_stats.get("unmeasured"):
            # A span whose bytes moved since the last measurement keeps its
            # existing hint rather than losing it silently — but the REBUILD
            # must still be told, or the corpus quietly drifts out of
            # measurement ([[project_wide_table_threshold]]).
            print(f"  [post-export] WARNING: {wide_stats['unmeasured']} table "
                  f"span(s) have no width measurement and kept their existing "
                  f"hint — run tools/diagnostics/measure_table_widths.py, then "
                  f"tools/pipeline/annotate_table_markers.py", flush=True)

        n = write_corpus(payloads)
        tick(f"wrote {n} articles")
    finally:
        session.close()


if __name__ == "__main__":
    main()
