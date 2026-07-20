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

import re
import sys
from pathlib import Path

sys.path.insert(0, "src")

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


def annotate_payloads(payloads: dict) -> tuple[int, int]:
    """Re-hint every math marker IN MEMORY → ``(changed, with_math)``.

    The phase as a pure transform over the loaded corpus, so the merged
    post-export pass (``tools/pipeline/post_export.py``) can apply it without a
    corpus round-trip of its own.  ``main()`` below is the standalone wrapper."""
    changed = with_math = 0
    for data in payloads.values():
        body = data.get("body", "")
        if not isinstance(body, str) or "«MATH" not in body:
            continue
        with_math += 1
        new_body = _annotate(body)
        if new_body != body:
            data["body"] = new_body
            changed += 1
    return changed, with_math


def main() -> int:
    """Standalone: load the corpus, annotate, write back what changed."""
    from britannica.export.corpus import load_corpus, write_payload
    payloads, _ = load_corpus(ARTICLES_DIR)
    before = {p: d.get("body", "") for p, d in payloads.items()}
    changed, with_math = annotate_payloads(payloads)
    for path, data in payloads.items():
        if data.get("body", "") != before[path]:
            write_payload(path, data)
    print(f"Annotated math markers: {changed} articles updated / {with_math} with math")
    return 0


if __name__ == "__main__":
    sys.exit(main())
