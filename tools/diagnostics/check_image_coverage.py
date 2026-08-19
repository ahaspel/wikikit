"""Pre-deploy gate: every `<img>` the corpus renders must resolve to a real file.

The site serves images from `data/images/` and the render points at
`/data/images/<name>`.  Nothing checked that the file was THERE.  69 references
across 18 articles pointed at nothing, and the only place it was ever reported
was the EPUB build's log line ("N referenced image(s) absent from the image
store, placeholder bundled") — a message that appears when somebody builds a
576 MB artifact, which is not a schedule.  On the site those figures were simply
broken, and the damage was concentrated rather than spread: CARNIVORA lost ALL
seven of its figures, CARYOPHYLLACEAE all three, MAP 16 of 62, CARPENTRY 13 of
34.  A 0.6% corpus-wide rate hid eight articles that had lost most of what they
illustrate ([[feedback_content_integrity_over_count]]).

This is the cheap check that was missing: it needs no network and no browser —
just the exported corpus and a directory listing.

THE RATCHET SHAPE, not a counter.  A missing image is either FETCHABLE (run
`tools/pipeline/download_images.py`) or genuinely absent from Commons, and the
second kind has to be written down rather than tolerated silently — otherwise
the gate is noise and gets ignored, which is how the EPUB's line was ignored.
So an unacknowledged missing image FAILS; an acknowledged one is listed in
`data/image_exceptions.json` with the reason it cannot be had.  Same contract as
the contributor-dedup gate: fix it, or say why it stays.

EXTERNAL `<img>` SOURCES FAIL TOO.  Eleven references hotlinked
`upload.wikimedia.org/score/…` for the musical notation in BAG-PIPE, BINIOU and
CITTERN — somebody else's host, leaking a reader's IP, with nothing noticing if
it stopped.  The files were in `data/images/` the whole time: the downloader
peeled a URL to its last segment and the render didn't, so one image class had
two answers to "what is this stored as".  With that closed there is no longer a
legitimate external `<img>`, so this gate treats one as a defect rather than
reporting it forever ([[feedback_sweepers_hide_bugs]]).

Usage:
    uv run python tools/diagnostics/check_image_coverage.py
        [--images DIR] [--exceptions PATH]
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from britannica.export.corpus import load_corpus     # noqa: E402

IMAGE_DIR = Path("data/images")
EXCEPTIONS = Path("data/image_exceptions.json")
SITE_PREFIX = "/data/images/"

# `src` of every rendered image.  Quote-delimited, so a filename with spaces or
# an apostrophe (`EB1911_Boraginaceae_Fig._1_Viper's_Bugloss.jpg`) survives.
_IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]*)"')


def audit(image_dir: Path = IMAGE_DIR):
    """-> (missing {name: [articles]}, external {src: [articles]}, totals)."""
    have = {p.name for p in image_dir.iterdir() if p.is_file()}
    payloads, _ = load_corpus()
    missing: dict[str, list[str]] = collections.defaultdict(list)
    external: dict[str, list[str]] = collections.defaultdict(list)
    refs = 0
    for path, d in payloads.items():
        title = d.get("title") or path.stem
        for src in _IMG_SRC_RE.findall(d.get("rendered_html") or ""):
            if not src.startswith(SITE_PREFIX):
                external[src].append(title)
                continue
            refs += 1
            # The render percent-encodes the name; compare against the file as
            # it sits on disk, or every non-ASCII figure reads as missing.
            name = urllib.parse.unquote(src[len(SITE_PREFIX):])
            if name not in have:
                missing[name].append(title)
    return missing, external, {"refs": refs, "have": len(have)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default=str(IMAGE_DIR))
    ap.add_argument("--exceptions", default=str(EXCEPTIONS))
    args = ap.parse_args()

    missing, external, totals = audit(Path(args.images))
    allowed = {}
    ex_path = Path(args.exceptions)
    if ex_path.exists():
        allowed = json.loads(ex_path.read_text(encoding="utf-8"))
    unreviewed = {n: a for n, a in missing.items() if n not in allowed}

    print(f"  image store: {totals['have']} files; corpus renders {totals['refs']} "
          f"<img> references")
    if allowed:
        print(f"  {len(allowed)} acknowledged-absent image(s) in {ex_path.name}")

    if external:
        n = sum(len(v) for v in external.values())
        arts_hit = {a for v in external.values() for a in v}
        print()
        print(f"  {n} reference(s) point OUTSIDE the image store, in "
              f"{len(arts_hit)} article(s):")
        for src, arts in sorted(external.items()):
            print(f"    {src[:72]}  ({', '.join(sorted(set(arts)))})")
        print()
        print("  Fix: mirror it into data/images/ and reference it as "
              f"{SITE_PREFIX}<name> — see image_assets.local_image_filename.")

    if not unreviewed and not external:
        print("  OK: every rendered image resolves in the store.")
        return 0
    if not unreviewed:
        return 1

    # Per ARTICLE, because that is the damage a reader meets — a count of files
    # hides an article that lost every figure it has.
    by_article: dict[str, int] = collections.Counter()
    for name, arts in unreviewed.items():
        for a in arts:
            by_article[a] += 1
    print(f"\n  {len(unreviewed)} referenced image(s) are ABSENT from the store, "
          f"across {len(by_article)} article(s):")
    for a, n in by_article.most_common():
        print(f"    {a[:44]:<44} {n} broken figure(s)")
    print("\n  Fix: uv run python tools/pipeline/download_images.py")
    print("  Or, if an image is genuinely not on Commons, add it to "
          f"{ex_path.name} as {{\"<filename>\": \"<why>\"}}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
