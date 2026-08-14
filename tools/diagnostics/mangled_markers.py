#!/usr/bin/env python3
"""Mangled-marker gate — our output may not invent a guillemet the source lacks.

    uv run python tools/diagnostics/mangled_markers.py [--limit N] [--detail]

A `«` is the marker delimiter, so one standing outside a well-formed token is
either a marker WE mangled or a guillemet the SOURCE already had.  Counting them
in the output alone cannot tell those apart, and the corpus has both: 108 of the
125 in the 2026-08-13 build are Wikisource's own mojibake (`108Â° 38' E.`, whose
live page is byte-identical to our copy), concentrated in the unproofread volume
25 math.  A bare count would be 108 false alarms.

Comparing against the raw source separates them exactly, because the source is
static ([[project_source_is_static]]): an article whose output carries a
guillemet SIGNATURE its own source never had has had a marker mangled between
the two, and nothing else can produce that.  Zero false positives, no baseline
to accept, nothing to keep up to date.

Signatures, not counts.  A footnote renders twice — inline and in the Notes
list — so TIMOTHY, SECOND EPISTLE TO carries its source's two Greek-OCR
guillemets four times in `rendered_html` with nothing wrong.  What cannot
happen is a `«#I»` or a `«BR)` that the source has no `«#I»` or `«BR)` for.

This is the check the leak oracle structurally cannot make.  `find_leaks` scans
one finished text against the marker lexicon and asks what survived raw; a
MANGLED marker is not a marker, so it matches nothing and reads as clean.  That
is how `subpage_target` shipped `«#I»` — it split a link target on `/`, and a
close marker's slash is not a path separator.  The 3-part «LN» opener grammar
then rejected the field (`[^|«]*` cannot span a `«`), the marker collapsed to
its 2-part reading, and 17 links rendered with the filename in the href and a
raw pipe in the anchor text.  Every check we had was silent.

Exits nonzero on any finding, so `set -e` aborts the rebuild before deploy.
"""
import argparse
import glob
import re
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.stdout.reconfigure(encoding="utf-8")
from britannica.export.corpus import NON_ARTICLE               # noqa: E402
from britannica.markers import unaccounted_guillemets          # noqa: E402
from britannica.outputs import outputs_for                     # noqa: E402
from britannica.render.leaks import mask_math                  # noqa: E402

# The tags a RENDERER emits, and nothing looser.  A permissive tag pattern eats
# the untranscribed math's stray `<`…`>` (`<i,X iV^A\**^\W\W>`) and any
# guillemet caught inside it — which it does to the raw source but not to the
# HTML, where the renderer escaped those same characters.  That asymmetry
# invented 15 findings in SPHERICAL HARMONICS alone.
_REAL_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")

ART = "data/derived/articles"
OUT = Path("data/derived/quality_reports/mangled_markers.tsv")


def _comparable(text, fmt="text"):
    """One text put in the form the SIGNATURE is comparable in.

    Escaping and tags are how the same source character reaches different
    consumers — a `«` before a quote is `«&quot;` in HTML — so both come off
    before the guillemet's neighbours are read.  Whitespace is handled inside
    the signature itself.

    Math goes first and goes WHOLE.  A formula's rendered form shares nothing
    with its source: the marker stream carries LaTeX and the HTML carries a
    KaTeX span, so every guillemet in the untranscribed math of SPHERICAL
    HARMONICS and SURVEYING reads as newly invented.  `mask_math` is the
    renderer's own answer to where each format keeps its math.
    """
    # Unescape LAST: `&lt;` is text that only LOOKS like a tag, and restoring it
    # before the strip would delete what the source keeps as literal characters.
    return unescape(_REAL_TAG.sub(" ", mask_math(text or "", fmt)))


def scan(path):
    """`(id, stem, title, {where: [context, …]})` for one article, or None.

    The marker stream `body` is scanned beside the consumer outputs: a mangling
    starts there, and catching it at the source beats catching four copies of it
    downstream.
    """
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        return ("unreadable", os.path.basename(path)[:-5], str(exc)[:80], {})
    if not isinstance(d, dict):
        return None
    found = {}
    for where, text, fmt in [("body", d.get("body"), "text")] + [
            (consumer, text, fmt) for consumer, fmt, text in outputs_for(d)]:
        hits = unaccounted_guillemets(_comparable(text, fmt))
        if hits:
            found.setdefault(where, []).extend(hits)
    if not found:
        return None
    return (d.get("id"), d.get("stable_id") or os.path.basename(path)[:-5],
            d.get("title"), found)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="stop after N articles")
    ap.add_argument("--detail", action="store_true",
                    help="print the source-owned ones too")
    args = ap.parse_args()

    files = [f for f in sorted(glob.glob(f"{ART}/*.json"))
             if os.path.basename(f) not in NON_ARTICLE]
    if args.limit:
        files = files[:args.limit]

    flagged = []
    with ProcessPoolExecutor() as ex:
        for r in ex.map(scan, files, chunksize=200):
            if r:
                flagged.append(r)

    # Only the articles that carry ANY unaccounted guillemet need the source,
    # so the database side stays small however big the corpus gets.
    from britannica.db.models import Article
    from britannica.db.session import SessionLocal
    session = SessionLocal()

    ours, theirs, unjoined, rows = [], [], [], []
    for art_id, stem, title, found in flagged:
        if art_id == "unreadable":
            unjoined.append((stem, title, "unreadable JSON"))
            continue
        row = session.get(Article, art_id) if art_id is not None else None
        if row is None:
            # Never pass an article we could not check: an unjoinable row is an
            # unanswered question, not a clean one.
            unjoined.append((stem, title, f"no source row for id={art_id}"))
            continue
        src = {sig for sig, _ctx in unaccounted_guillemets(_comparable(row.body))}
        invented = {}
        for where, hits in found.items():
            new = [(sig, ctx) for sig, ctx in hits if sig not in src]
            if new:
                invented[where] = new
        (ours if invented else theirs).append(
            (stem, title, sorted(src), {k: len(v) for k, v in
                                        (invented or found).items()}))
        for where, hits in sorted(invented.items()):
            for sig, ctx in hits:
                rows.append((stem, str(title), where, repr(sig),
                             ctx.replace("\t", " ").replace("\n", "\\n")))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        fh.write("stem\ttitle\twhere\tsignature\tcontext\n")
        for r in rows:
            fh.write("\t".join(r) + "\n")

    print(f"MANGLED-MARKER GATE — {len(files)} articles\n")
    print(f"  carrying an unaccounted «     : {len(flagged)}")
    print(f"  explained by the raw source   : {len(theirs)}")
    print(f"  INVENTED BY US                : {len(ours)}")
    if unjoined:
        print(f"  UNCHECKABLE                   : {len(unjoined)}")

    for stem, title, why in unjoined:
        print(f"    ?? {title}  [{stem}]  {why}")
    for stem, title, src, counts in sorted(ours, key=lambda r: -max(r[3].values())):
        detail = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"    !! {title}  [{stem}]  invented: {detail}"
                  f"   (source has {src or 'no unaccounted «'})")
    if args.detail:
        print()
        for stem, title, src, counts in sorted(theirs, key=lambda r: r[1] or ""):
            detail = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"    -- {title}  [{stem}]  {detail}   source={src}")

    print(f"\n  per-occurrence detail → {OUT}")
    if ours or unjoined:
        print("\n  A guillemet we invented is a marker we mangled.  Find the "
              "string operation\n  that split or rewrote a marker token and make "
              "it hold markers aside.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
