#!/usr/bin/env python3
"""Overlapping-span audit — how much of the corpus is `<A><B></A></B>`?

    uv run python tools/diagnostics/overlap_audit.py [--examples N]

A tree can represent NESTING (`<A><B></B></A>`) and nothing else.  When two
source spans CROSS — neither contains the other — no balanced matcher can bound
both, so the walker bounds one and carries the other's halves as leaves.  That
is a correct, lossless answer (the emitted tag stream equals the source's own,
and the browser reparents exactly as it does on Wikisource), but it IS the one
shape where the recursion cannot recurse, so its true frequency is worth
knowing rather than assuming.

This scans RAW source (the walker's own input) and reports every strictly
overlapping pair, by construct family:

    HTML  a paired `<name>`…`</name>` element (known tag names only — an OCR
          `<t` / a math `<e>` is a literal `<`, not a tag)
    PAIR  a `{{NAME/s}}`…`{{NAME/e}}` template wrapper (fine print, centring)
    TMPL  a `{{…}}` template
    TABLE a `{|`…`|}` wikitable
    LINK  a `[[…]]` bracket link

Opaque interiors (`<math>`/`<nowiki>`/`<score>`/…) and comments are skipped
whole — their contents are verbatim payload, not markup.  Dangling halves (an
open with no close anywhere, or a close with no open) are counted separately:
they are UNPAIRED, not overlapping, and a different problem.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "diagnostics"))
sys.stdout.reconfigure(encoding="utf-8")

# Real HTML/wiki tag names — the same real-tag-vs-garbage discrimination the
# walker and the leak oracle use.  Anything else after a `<` is literal text.
KNOWN = {
    "a", "abbr", "b", "big", "blockquote", "center", "cite", "code", "del",
    "div", "em", "font", "i", "includeonly", "ins", "li", "mark", "ol", "p",
    "poem", "q", "ref", "s", "small", "span", "strike", "strong", "sub", "sup",
    "table", "tbody", "td", "th", "thead", "tr", "u", "ul", "var",
}
VOID = {"br", "hr", "img", "wbr", "pagequality", "section", "references"}
OPAQUE = "math|nowiki|score|hiero|pre|syntaxhighlight|source|timeline"

_TOK = re.compile(
    r"<!--.*?-->"                                        # 0 comment
    rf"|<({OPAQUE})\b[^>]*>.*?</\1\s*>"                  # 1 opaque element
    r"|\{\{\s*([^{}|=\n]{1,40}?)\s*/([se])\s*\}\}"       # 2,3 paired wrapper
    r"|<(/?)([A-Za-z][A-Za-z0-9]*)\b([^>]*)>"            # 4,5,6 tag
    # Wikitable delimiters are LINE-ANCHORED in MediaWiki, and must be matched that
    # way or `{{name|}}` (an empty last arg) reads its `|}` as a table close — 81
    # phantom dangling closes before this was anchored.
    r"|(?:(?<=\n)|^)(\{\||\|\})"                         # 7 wikitable
    r"|(\{\{|\}\}|\[\[|\]\])",                           # 8 bracket
    re.DOTALL | re.IGNORECASE,
)

_BRACKET_KIND = {"{{": ("TMPL", 1), "}}": ("TMPL", -1),
                 "{|": ("TABLE", 1), "|}": ("TABLE", -1),
                 "[[": ("LINK", 1), "]]": ("LINK", -1)}


def _spans(raw: str):
    """Every paired construct in `raw` as (kind, name, start, end), plus the
    dangling-half counts.  One left-to-right pass with a per-(kind,name) stack:
    a close binds to the most recent same-named open, which is what both
    MediaWiki and a browser do."""
    open_stacks: dict[tuple[str, str], list[int]] = {}
    spans: list[tuple[str, str, int, int]] = []
    dangling_close_c: Counter = Counter()
    for m in _TOK.finditer(raw):
        if m.group(1) or m.group(0).startswith("<!--"):
            continue                                    # opaque / comment: skip whole
        if m.group(2) is not None:                      # {{NAME/s}} · {{NAME/e}}
            kind, name = "PAIR", m.group(2).strip().lower()
            delta = 1 if m.group(3).lower() == "s" else -1
        elif m.group(5) is not None:                    # <tag> · </tag>
            name = m.group(5).lower()
            if name in VOID or name not in KNOWN or m.group(6).rstrip().endswith("/"):
                continue                                # void / self-closing / not a tag
            kind, delta = "HTML", -1 if m.group(4) else 1
        else:                                           # {| |} · {{ }} [[ ]]
            kind, delta = _BRACKET_KIND[m.group(7) or m.group(8)]
            name = ""
        key = (kind, name)
        if delta > 0:
            open_stacks.setdefault(key, []).append(m.start())
        else:
            st = open_stacks.get(key)
            if st:
                spans.append((kind, name, st.pop(), m.end()))
            else:
                dangling_close_c[f"{kind}:{name}" if name else kind] += 1
    dangling_open_c = Counter({f"{k}:{n}" if n else k: len(v)
                               for (k, n), v in open_stacks.items() if v})
    return spans, dangling_open_c, dangling_close_c


def _overlaps(spans):
    """Strictly overlapping pairs: a.start < b.start < a.end < b.end.

    Event sweep — push each span at its start, pop at its end.  If the span
    ending is not on top of the stack, every span above it started inside it and
    ends after it: those are exactly the crossings."""
    events = []
    for i, (_k, _n, s, e) in enumerate(spans):
        events.append((s, 1, i))
        events.append((e, 0, i))                # ends sort before starts at a tie
    events.sort()
    stack: list[int] = []
    out: list[tuple[int, int]] = []
    for _pos, is_start, i in events:
        if is_start:
            stack.append(i)
        elif i in stack:
            k = stack.index(i)
            out.extend((i, j) for j in stack[k + 1:])
            stack.pop(k)
    return out


def _scan(row):
    aid, vol, _pg, raw = row
    spans, d_open, d_close = _spans(raw)
    pairs = _overlaps(spans)
    kinds = Counter()
    sample = []
    for i, j in pairs:
        a, b = spans[i], spans[j]
        ka = f"{a[0]}:{a[1]}" if a[1] else a[0]
        kb = f"{b[0]}:{b[1]}" if b[1] else b[0]
        kinds[f"{ka} × {kb}"] += 1
        if len(sample) < 2:
            sample.append((ka, kb, raw[a[2]:a[2] + 60].replace("\n", "⏎")))
    return aid, vol, len(pairs), kinds, d_open, d_close, sample


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--examples", type=int, default=12)
    ap.add_argument("--refresh", action="store_true",
                    help="rebuild the raw-corpus cache from the DB first "
                         "(required in a rebuild — the pickle is otherwise stale)")
    args = ap.parse_args()

    from _corpus_cache import load_corpus
    rows = list(load_corpus(refresh=args.refresh))
    print(f"scanning {len(rows)} articles …", flush=True)

    kinds = Counter()
    dirty = []
    total = 0
    d_open: Counter = Counter()
    d_close: Counter = Counter()
    with ProcessPoolExecutor() as ex:
        for aid, vol, n, k, do, dc, sample in ex.map(_scan, rows, chunksize=200):
            d_open.update(do)
            d_close.update(dc)
            if n:
                total += n
                kinds.update(k)
                dirty.append((n, aid, vol, sample))

    print(f"\nOVERLAP AUDIT — {len(rows)} articles")
    print(f"  articles with >=1 crossing : {len(dirty)} "
          f"({100 * len(dirty) / max(1, len(rows)):.2f}%)")
    print(f"  crossings total            : {total}")
    print("\n  by construct pair:")
    for k, n in kinds.most_common(30):
        print(f"    {n:6}  {k}")
    print(f"\n  DANGLING halves (no partner anywhere in the article) — "
          f"{sum(d_open.values())} opens / {sum(d_close.values())} closes:")
    for k in sorted(set(d_open) | set(d_close),
                    key=lambda k: -(d_open[k] + d_close[k]))[:25]:
        print(f"    open {d_open[k]:5}   close {d_close[k]:5}   {k}")
    print(f"\n  worst articles (crossings, aid, vol):")
    for n, aid, vol, sample in sorted(dirty, reverse=True)[:args.examples]:
        print(f"    {n:5}  aid={aid} v{vol}")
        for ka, kb, snip in sample:
            print(f"           {ka} × {kb}  ⟨{snip}⟩")
    return 0


if __name__ == "__main__":
    sys.exit(main())
