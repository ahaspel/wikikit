"""The post-export pass: ONE load, every corpus transform, ONE write.

The export writes each article's body with its raw producer markers and no
``rendered_html``; everything that needs corpus-wide knowledge then happens here,
AFTER the classified TOC (6b) and the kind index (6b3) exist.

This used to be three phases — 4c math annotation, 6b4 contributors, 6b5 xrefs +
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
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "tools/pipeline")

from britannica.db.models import Article
from britannica.db.session import SessionLocal
from britannica.export.article_json import register_stable_id_dedup
from britannica.export.corpus import ARTICLES_DIR, load_corpus, write_corpus

from annotate_math_markers import annotate_payloads
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

        resolve_and_render(session, payloads)
        tick("xrefs resolved + rendered")

        n = write_corpus(payloads)
        tick(f"wrote {n} articles")
    finally:
        session.close()


if __name__ == "__main__":
    main()
