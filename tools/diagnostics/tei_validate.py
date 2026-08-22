"""Does every article's TEI validate against the OFFICIAL TEI P5 schema?

    uv run --with lxml python tools/diagnostics/tei_validate.py            # whole corpus
    uv run --with lxml python tools/diagnostics/tei_validate.py --sample 500
    uv run --with lxml python tools/diagnostics/tei_validate.py --article AFRICA

THE SCHEMA IS EXTERNAL ON PURPOSE.  A RELAX NG hand-written from our own output
would only restate what `export/tei.py` already does — a tautology, and the
`current output is not an oracle` trap.  `tools/schema/tei_all.rng` is the TEI
Consortium's own schema, vendored (1.0 MB) so the gate never depends on
tei-c.org being reachable during a build.

WHAT IT CATCHES THAT A LEAK SCAN CANNOT.  Validation is a genuinely independent
net: a `<cell>` outside a `<row>`, an unclosed `<hi>`, a `<p>` inside an inline
element, a `<formula>` with element content, an empty `<editorialDecl/>`, a
duplicate `@xml:id`.  Every one of those was a real defect in the first writer,
and none of them leaves a marker behind for a leak scan to find.  It found seven
distinct classes on the day it was first run.

lxml is fetched on demand (`--with lxml`) rather than added as a project
dependency — the same pattern `deploy.sh` uses for `huggingface_hub`.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

SCHEMA = ROOT / "tools" / "schema" / "tei_all.rng"
ARTICLES = ROOT / "data" / "derived" / "articles"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0,
                    help="validate a random N instead of the whole corpus")
    ap.add_argument("--article", help="validate one article by title")
    ap.add_argument("--seed", type=int, default=5)
    args = ap.parse_args()

    try:
        from lxml import etree
    except ImportError:
        print("  lxml is required: uv run --with lxml python "
              "tools/diagnostics/tei_validate.py", file=sys.stderr)
        return 2
    if not SCHEMA.is_file():
        print(f"  missing schema: {SCHEMA}", file=sys.stderr)
        return 2

    from britannica.export.tei import article_to_tei

    rng = etree.RelaxNG(etree.parse(str(SCHEMA)))
    files = sorted(glob.glob(str(ARTICLES / "*.json")))
    files = [f for f in files if os.path.basename(f) not in
             ("index.json", "contributors.json")]
    if args.sample:
        import random
        random.seed(args.seed)
        files = random.sample(files, min(args.sample, len(files)))

    ok = invalid = malformed = 0
    errs: collections.Counter = collections.Counter()
    offenders: list = []
    t0 = time.time()
    for f in files:
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict) or not d.get("body"):
            continue
        if args.article and (d.get("title") or "") != args.article:
            continue
        xml = article_to_tei(d)
        try:
            doc = etree.fromstring(xml.encode("utf-8"))
        except etree.XMLSyntaxError as e:
            malformed += 1
            errs[f"NOT WELL-FORMED: {str(e)[:60]}"] += 1
            if len(offenders) < 20:
                offenders.append((d.get("title"), str(e)[:80]))
            continue
        if rng.validate(doc):
            ok += 1
        else:
            invalid += 1
            first = list(rng.error_log)[0]
            errs[first.message[:70]] += 1
            if len(offenders) < 20:
                offenders.append((d.get("title"), first.message[:80]))

    n = ok + invalid + malformed
    print(f"  TEI validation — {n:,} article(s) in {time.time() - t0:.0f}s")
    print(f"    valid           : {ok:,}")
    print(f"    INVALID         : {invalid:,}")
    print(f"    not well-formed : {malformed:,}")
    if errs:
        print("\n  by first error:")
        for m, c in errs.most_common(12):
            print(f"     {c:>6}  {m}")
        print("\n  offenders:")
        for t, m in offenders:
            print(f"     {str(t)[:30]:<32} {m}")
        return 1
    print("\n  OK: every article validates against TEI P5.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
