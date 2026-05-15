"""Measure rendered widths of every display-mode math marker corpus-wide.

For each unique LaTeX expression:
  * Render via KaTeX in a headless Chromium with `displayMode: true`
  * Measure `.katex-display` bounding-box width at 100% font-size
  * If wider than ``TARGET_WIDTH``, find the SMALLEST font-size that
    produces a width ≤ ``TARGET_WIDTH`` by trying 95, 90, 85, ... 50.
  * Record per-LaTeX result in ``data/derived/math_widths.json``.

Output schema:
  {
    "<sha256(latex)>": {
      "natural_w": float,         # px at 100% font-size
      "best_fs": int | null,      # smallest font-size where width ≤ TARGET; null if unscalable
      "best_w": float | null,     # px at best_fs
      "unscalable": bool,         # true if no fs ≥ MIN_FS fits the target
      "sample_articles": [str],   # which articles use this latex (up to 3)
    }
  }

Foundation for the auto-scaling pipeline (Approach C from the math-quality session).
Cached: hash-keyed, so subsequent runs only measure new LaTeX.
"""
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None

TARGET_WIDTH = 520     # px; body-text column width minus breathing room
MIN_FS = 50            # smallest font-size we'll try (50%)
FS_STEPS = list(range(95, MIN_FS - 1, -5))  # 95, 90, 85, ..., 50
CACHE_PATH = Path("data/derived/math_widths.json")
MATH_RE = re.compile(r"«MATH:([^«]*)«/MATH»", re.DOTALL)

KATEX_HTML = """<!DOCTYPE html>
<html><head>
<link rel="stylesheet"
      href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.js"></script>
<style>
  body { margin: 0; padding: 0; font-family: Georgia, "Times New Roman", serif;
         font-size: 17.28px; }
  #host { display: inline-block; }
</style>
</head>
<body><div id="host"></div></body></html>
"""


def _hash(latex: str) -> str:
    return hashlib.sha256(latex.encode("utf-8")).hexdigest()[:16]


def _collect_latex() -> dict[str, list[str]]:
    """Return {hash → (latex, sample_articles)} for every unique
    display-candidate math marker in the corpus.

    Display candidates: markers with `\\begin{` (definitely display)
    plus all markers >100 chars (any of these MIGHT render display
    depending on paragraph context; measuring is cheap).
    """
    by_hash: dict[str, dict] = {}
    for path in sorted(Path("data/derived/articles").glob("*.json")):
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        body = d.get("body", "")
        if not isinstance(body, str):
            continue
        title = d.get("title", path.stem)
        for m in MATH_RE.finditer(body):
            latex = m.group(1)
            if len(latex) < 100 and "\\begin{" not in latex:
                continue
            h = _hash(latex)
            if h not in by_hash:
                by_hash[h] = {"latex": latex, "articles": []}
            if title not in by_hash[h]["articles"]:
                by_hash[h]["articles"].append(title)
    return by_hash


def _measure(page, latex: str) -> dict:
    """Measure widths of ``latex`` at decreasing font-sizes.  Return
    the natural width and the smallest font-size achieving width ≤
    TARGET_WIDTH (or unscalable=True if none fits)."""
    # Natural width
    page.evaluate(f"""() => {{
      const h = document.getElementById('host');
      h.style.fontSize = '100%';
      h.innerHTML = '';
      katex.render({json.dumps(latex)}, h, {{
        throwOnError: false, displayMode: true,
      }});
    }}""")
    natural_w = page.evaluate(
        "() => { const el = document.querySelector('.katex-display'); "
        "return el ? el.getBoundingClientRect().width : 0; }"
    )
    if natural_w <= TARGET_WIDTH + 2:
        return {"natural_w": natural_w, "best_fs": 100,
                "best_w": natural_w, "unscalable": False}
    # Try smaller font sizes
    best_fs = None
    best_w = None
    for fs in FS_STEPS:
        page.evaluate(f"""() => {{
          const h = document.getElementById('host');
          h.style.fontSize = '{fs}%';
          h.innerHTML = '';
          katex.render({json.dumps(latex)}, h, {{
            throwOnError: false, displayMode: true,
          }});
        }}""")
        w = page.evaluate(
            "() => { const el = document.querySelector('.katex-display'); "
            "return el ? el.getBoundingClientRect().width : 0; }"
        )
        if w <= TARGET_WIDTH + 2:
            best_fs = fs
            best_w = w
            break
    if best_fs is None:
        # Use MIN_FS as the best we could do
        return {"natural_w": natural_w, "best_fs": MIN_FS,
                "best_w": w, "unscalable": True}
    return {"natural_w": natural_w, "best_fs": best_fs,
            "best_w": best_w, "unscalable": False}


def main():
    # Load existing cache
    cache: dict = {}
    if CACHE_PATH.exists():
        try:
            cache = json.load(open(CACHE_PATH, encoding="utf-8"))
        except Exception:
            cache = {}
    print(f"Existing cache: {len(cache)} entries")

    by_hash = _collect_latex()
    print(f"Display-candidate markers in corpus: {len(by_hash)} unique")

    new_hashes = [h for h in by_hash if h not in cache]
    print(f"New (uncached): {len(new_hashes)}")
    if not new_hashes:
        print("All measurements cached.  Re-running stats only.")
    else:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content(KATEX_HTML)
            page.wait_for_function("typeof katex !== 'undefined'")
            for i, h in enumerate(new_hashes):
                latex = by_hash[h]["latex"]
                try:
                    result = _measure(page, latex)
                except Exception as e:
                    print(f"  [{i+1}/{len(new_hashes)}] {h}: ERROR {e}")
                    continue
                result["sample_articles"] = by_hash[h]["articles"][:3]
                cache[h] = result
                if (i + 1) % 50 == 0:
                    print(f"  [{i+1}/{len(new_hashes)}] measured "
                          f"(natural {result['natural_w']:.0f}px, "
                          f"best_fs={result.get('best_fs')})")
                    # Periodic save in case we crash
                    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    json.dump(cache, open(CACHE_PATH, "w", encoding="utf-8"),
                              indent=1, ensure_ascii=False)
            browser.close()

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    json.dump(cache, open(CACHE_PATH, "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    print(f"\nSaved {len(cache)} measurements → {CACHE_PATH}")

    # Stats
    print()
    print("=== Distribution ===")
    fit_natural = sum(1 for v in cache.values()
                      if v["best_fs"] == 100 and not v.get("unscalable"))
    scaled = sum(1 for v in cache.values()
                 if v["best_fs"] is not None and v["best_fs"] < 100
                 and not v.get("unscalable"))
    unscalable = sum(1 for v in cache.values() if v.get("unscalable"))
    print(f"  Fits at natural size: {fit_natural}")
    print(f"  Scaled to fit:        {scaled}")
    print(f"  Unscalable:           {unscalable}")

    if scaled:
        print()
        print("=== Scaled distribution by font-size ===")
        by_fs = defaultdict(int)
        for v in cache.values():
            if not v.get("unscalable") and v["best_fs"] is not None and v["best_fs"] < 100:
                by_fs[v["best_fs"]] += 1
        for fs in sorted(by_fs.keys(), reverse=True):
            print(f"  fs={fs}%: {by_fs[fs]:4d} markers")

    if unscalable:
        print()
        print("=== Unscalable: top 20 worst (highest natural width) ===")
        worst = sorted(
            ((h, v) for h, v in cache.items() if v.get("unscalable")),
            key=lambda kv: -kv[1]["natural_w"],
        )
        for h, v in worst[:20]:
            arts = ", ".join(v.get("sample_articles", []))[:60]
            print(f"  natural={v['natural_w']:6.0f}px  min_w={v.get('best_w', 0):6.0f}px  "
                  f"  ({arts})")


if __name__ == "__main__":
    main()
