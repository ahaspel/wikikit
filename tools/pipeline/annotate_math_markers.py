"""Re-annotate `«MATH:` markers in exported article JSONs with cached scale hints.

Runs AFTER ``measure_math_widths.py`` has refreshed
``data/derived/math_widths.json``.  Walks every article in
``data/derived/articles/``, finds each math marker (plain
``«MATH:`` or already-hinted ``«MATH[fs=N]:`` / ``«MATH[popout]:``),
looks up its scale hint, and rewrites the marker.

Pure text transform on the exported body — no re-rendering, no
re-export.  Lets the rebuild emerge with every math marker
correctly hinted in one pass, even though Phase 4 (export) may
have run with a stale cache for newly added or changed LaTeX.

If the cache has no entry, the marker is rewritten as plain
``«MATH:`` (i.e. the script also acts as a hint-stripper for any
expression that fell out of the cache).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Force a fresh cache load — measure_math_widths.py may have just
# rewritten the file from under us.
import britannica.math_widths as _mw
_mw._LOOKUP = None
from britannica.math_widths import scale_hint  # noqa: E402

ARTICLES_DIR = Path("data/derived/articles")
# Capture the existing hint slot so the producer-carried `display` token (block-
# vs-inline) survives this refresh — we only re-derive the width hint (fs/popout).
MATH_RE = re.compile(r"«MATH(?:\[([^\]]*)\])?:([^«]*)«/MATH»")


def _annotate(body: str) -> str:
    def _replace(m: re.Match) -> str:
        existing = (m.group(1) or "").split(",")
        latex = m.group(2)
        tokens = ["display"] if "display" in existing else []
        hint = scale_hint(latex)
        if hint:
            tokens.append(hint)
        if tokens:
            return f"«MATH[{','.join(tokens)}]:{latex}«/MATH»"
        return f"«MATH:{latex}«/MATH»"
    return MATH_RE.sub(_replace, body)


def main() -> int:
    files = sorted(ARTICLES_DIR.glob("*.json"))
    total = 0
    changed = 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  skip {path.name}: {e}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            continue
        body = data.get("body", "")
        if not isinstance(body, str) or "«MATH" not in body:
            continue
        total += 1
        new_body = _annotate(body)
        if new_body == body:
            continue
        data["body"] = new_body
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        changed += 1
    print(f"Annotated math markers: {changed} articles updated / {total} with math")
    return 0


if __name__ == "__main__":
    sys.exit(main())
