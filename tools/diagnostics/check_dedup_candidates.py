"""Pre-deploy gate: abort if the contributor dedup report contains any
candidate pair not acknowledged in `data/contributor_aliases.json`.

Reads the JSON report produced by
``tools/db/dedup_contributors.py --report``, then filters out:

  • Pairs whose names are already linked by the `aliases` map (canonical
    + variant under the same key, or both variants under one key) — the
    NEXT rebuild's source-layer canonicalisation will collapse them.
    Surfacing them now is noise.
  • Pairs explicitly listed in the `distinct` section of aliases.json
    (acknowledged-different people that look similar).

If anything remains, prints the unreviewed candidates and exits 1 so
`rebuild_all.sh`'s ``set -e`` aborts before deploy.

Workflow when this gate fires:
  1. Review each candidate pair.
  2. Real dupe → add to aliases.json `aliases` (canonical → [variant]).
  3. Different people → add to aliases.json `distinct` ([name_a, name_b]).
  4. Re-run rebuild.

Usage:
    uv run python tools/diagnostics/check_dedup_candidates.py
        [--report PATH] [--aliases PATH]
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from britannica.contributors.aliases import normalize  # noqa: E402

DEFAULT_REPORT = "data/derived/quality_reports/dedup_candidates.json"
DEFAULT_ALIASES = "data/contributor_aliases.json"


def _load_aliases_index(path: Path) -> tuple[dict[str, str], set[frozenset[str]]]:
    """Build (variant→canonical normalized map, set of distinct pairs).

    Variant→canonical includes the identity entry (canonical→canonical)
    so a candidate that names the canonical on both sides is recognised.
    """
    if not path.exists():
        return {}, set()
    raw = json.loads(path.read_text(encoding="utf-8"))
    canon_map: dict[str, str] = {}
    for canonical, variants in (raw.get("aliases") or {}).items():
        c_n = normalize(canonical)
        canon_map[c_n] = c_n
        for v in variants:
            canon_map[normalize(v)] = c_n
    distinct = {
        frozenset((normalize(a), normalize(b)))
        for pair in (raw.get("distinct") or [])
        if isinstance(pair, list) and len(pair) == 2
        for a, b in [pair]
    }
    return canon_map, distinct


def _already_aliased(a_name: str, b_name: str, canon_map: dict[str, str]) -> bool:
    """True iff both names resolve to the same canonical via aliases."""
    a = canon_map.get(normalize(a_name))
    b = canon_map.get(normalize(b_name))
    return a is not None and a == b


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", default=DEFAULT_REPORT,
                    help=f"path to dedup report JSON "
                         f"(default {DEFAULT_REPORT})")
    ap.add_argument("--aliases", default=DEFAULT_ALIASES,
                    help=f"path to aliases JSON "
                         f"(default {DEFAULT_ALIASES})")
    args = ap.parse_args()

    report_path = Path(args.report)
    if not report_path.exists():
        print(f"dedup report not found: {report_path}", file=sys.stderr)
        return 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    candidates = report.get("candidates", [])

    canon_map, distinct = _load_aliases_index(Path(args.aliases))

    unreviewed = []
    skipped_aliased = 0
    skipped_distinct = 0
    for cand in candidates:
        a = cand.get("a_name", "")
        b = cand.get("b_name", "")
        if _already_aliased(a, b, canon_map):
            skipped_aliased += 1
            continue
        if frozenset((normalize(a), normalize(b))) in distinct:
            skipped_distinct += 1
            continue
        unreviewed.append(cand)

    threshold = report.get("threshold", "?")
    print(f"Dedup report: {len(candidates)} total candidate(s) at "
          f"sim ≥ {threshold}")
    print(f"  {skipped_aliased} already covered by aliases.json `aliases`")
    print(f"  {skipped_distinct} acknowledged in aliases.json `distinct`")
    print(f"  {len(unreviewed)} unreviewed")

    if not unreviewed:
        print("OK: no unreviewed contributor-dupe candidates.")
        return 0

    print()
    print("FAIL: unreviewed contributor-dupe candidates — resolve in "
          "data/contributor_aliases.json before deploy:")
    for c in unreviewed:
        print(f"  sim={c.get('sim'):.3f}  "
              f"{c.get('a_name')!r} (id={c.get('a_id')}, "
              f"{c.get('a_articles')} articles)  "
              f"vs  {c.get('b_name')!r} (id={c.get('b_id')}, "
              f"{c.get('b_articles')} articles)")
    print()
    print("If same person: add the variant under the canonical in `aliases`.")
    print("If different people: add the pair to `distinct`.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
