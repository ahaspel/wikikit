"""Viewer-loop check — the OTHER half of totality. The leak-scan verifies
source→markers (producer side); this verifies markers→render (viewer side).

Runs the REAL producer (`_transform_text_v2`, the full transform — NOT render_proto,
which is only `_apply_markup`) over a sample, collects every marker that survives into
its final output, and checks each against the viewer's actual handler vocabulary
(`viewer.html` + the Python export). A producer marker the viewer can't render = an
OPEN loop: it shows literally or drops. Closing it is the step-2 viewer work.

Usage: uv run python -m wikikit.diagnostics.viewer_loop [N]   (default 120)
"""
from __future__ import annotations

import io
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import wikikit.export
from wikikit.db.session import SessionLocal
from wikikit.db.models import Article
from wikikit.pipeline.stages.elements import ElementContext, process_elements
from wikikit.site.build import PAGES

# Viewer handler vocabulary: « markers and {{ markers it references in source.
viewer_src = (PAGES / "viewer.html").read_text(encoding="utf-8")
export_src = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                       for p in (Path(wikikit.export.__file__).parent).glob("*.py"))
_src = viewer_src + export_src
# Handler refs appear BOTH as literal « (comments, title/cell handlers) AND as
# JS «…» escapes (the main body renderer writes its regexes that way).
# Counting only literal « falsely reports every «-escaped handler as a gap.
HANDLED = set(re.findall(r"«(/?[A-Za-z]+)", _src))
HANDLED |= set(re.findall(r"\\u00ab(/?[A-Za-z]+)", _src))
HANDLED |= {f"{{{{{m}" for m in re.findall(r"\{\{([A-Za-z]+)", viewer_src)}


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    s = SessionLocal()
    arts = (s.query(Article).filter(Article.article_type != "plate")
            .order_by(Article.volume, Article.page_start).limit(n).all())
    emitted = Counter()
    for i, a in enumerate(arts):
        if i and i % 40 == 0:
            print(f"  …{i}/{len(arts)}", flush=True)
        # Stored whole — read, don't rebuild
        # ([[project_page_position_out_of_band]]).
        raw = a.body or ""
        try:
            out = process_elements(raw, ElementContext(volume=a.volume))
        except Exception:
            continue
        emitted.update(re.findall(r"«([A-Z][A-Za-z0-9]*)»", out))   # opening « markers in FINAL output
        emitted.update(f"{{{{{m}" for m in re.findall(r"\{\{([A-Za-z]+):", out))  # {{ markers
    s.close()
    print(f"\n=== markers in REAL producer output ({n} articles) vs viewer handlers ===")
    for m, cnt in emitted.most_common():
        tag = "OK" if (m in HANDLED or m.lstrip("{") in HANDLED) else "*** GAP — viewer can't render ***"
        print(f"  {cnt:7d}  {m:14s} {tag}")


if __name__ == "__main__":
    main()
