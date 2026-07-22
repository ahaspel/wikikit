"""EPUB math assets — cached LaTeX renders, one per target.

The reader/site path runs KaTeX client-side; the EPUB targets can't, so math is
pre-rendered here (Playwright, like ``measure_math_widths``) and the render just
reads the cache — a rebuild renders only NEW equations.

Two formats, decided by reader testing (the spike):
  * **SVG** (MathJax, ``currentColor``) — the DIRECT EPUB.  Inline in the XHTML,
    so it inherits the reader's text colour (adapts to night/sepia) and carries
    its own baseline offset.  Perfect on every real EPUB3 reader.
  * **PNG** (KaTeX, opaque white, hi-DPI) — the KINDLE edition.  Kindle renders
    no SVG, so math ships as images that survive Amazon's conversion; baseline
    measured against a true baseline marker.

Keyed by ``(latex, display)``.  Caches:
  data/derived/math_svg.json   hash -> svg string
  data/derived/math_png.json   hash -> {w_em, h_em, depth_em}
  data/derived/math_png/<hash>.png
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

_DERIVED = Path("data/derived")
SVG_CACHE = _DERIVED / "math_svg.json"
PNG_META = _DERIVED / "math_png.json"
PNG_DIR = _DERIVED / "math_png"

FONT_PX = 16.0        # em = px / FONT_PX so math scales with the reader's font
PNG_SCALE = 2         # device pixel ratio for the Kindle PNGs (crisp, not huge)


def math_key(latex: str, display: bool) -> str:
    return hashlib.sha256(
        (("D:" if display else "I:") + latex).encode("utf-8")).hexdigest()[:16]


# ── readers (used by the render — no Playwright) ─────────────────────────
_svg_cache: dict | None = None
_png_meta: dict | None = None


def _load(path: Path, slot: str):
    val = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    globals()[slot] = val
    return val


def svg_for(latex: str, display: bool) -> str | None:
    cache = _svg_cache if _svg_cache is not None else _load(SVG_CACHE, "_svg_cache")
    return cache.get(math_key(latex, display))


def png_meta(latex: str, display: bool) -> dict | None:
    meta = _png_meta if _png_meta is not None else _load(PNG_META, "_png_meta")
    return meta.get(math_key(latex, display))


def reset_caches() -> None:
    """Test/rebuild hook — drop the in-process caches so a freshly-generated
    file is re-read within one process."""
    global _svg_cache, _png_meta
    _svg_cache = _png_meta = None


# ── collect mode: the render decides `display` per equation, so a collect pass
# harvests the EXACT (latex, display) set before generation (render → collect →
# generate → render-for-real) ─────────────────────────────────────────────
_collector: set | None = None


def start_collect() -> None:
    global _collector
    _collector = set()


def take_collected() -> set:
    global _collector
    got = _collector or set()
    _collector = None
    return got


def collect(latex: str, display: bool) -> bool:
    """Called by the render's math element.  In collect mode, records the request
    and returns True (the caller emits a throwaway placeholder); else False (the
    caller reads the cache)."""
    if _collector is None:
        return False
    _collector.add((latex, display))
    return True


# ── generation (Playwright; batch, cached) ───────────────────────────────
_KATEX_HTML = f"""<!DOCTYPE html><html><head>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.js"></script>
<style>body{{margin:0;padding:0;font-family:Georgia,serif;font-size:{FONT_PX}px;background:#fff;}}
#bl{{display:inline-block;width:0;height:1em;vertical-align:baseline;}}
#host{{display:inline-block;padding:1px;}}</style>
</head><body><span id="bl"></span><span id="host"></span></body></html>"""

_MJ_HTML = """<!DOCTYPE html><html><head><script>
window.MathJax={loader:{load:['input/tex','output/svg']},svg:{fontCache:'none'}};
</script><script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
</head><body></body></html>"""


def generate(requests, *, svg=True, png=True, log=print) -> int:
    """Render + cache every ``(latex, display)`` in ``requests`` not already
    cached.  Returns the number of newly-rendered equations.  Idempotent: a
    second run over the same corpus renders nothing."""
    from britannica.render.article import _process_latex
    from playwright.sync_api import sync_playwright

    uniq = {math_key(la, d): (la, d) for la, d in requests}
    svg_c = _load(SVG_CACHE, "_svg_cache") if svg else {}
    meta_c = _load(PNG_META, "_png_meta") if png else {}
    todo_svg = [k for k in uniq if svg and k not in svg_c]
    todo_png = [k for k in uniq if png and (k not in meta_c or not (PNG_DIR / f"{k}.png").exists())]
    if not todo_svg and not todo_png:
        log(f"math assets: all {len(uniq)} cached")
        return 0
    log(f"math assets: {len(todo_svg)} SVG + {len(todo_png)} PNG to render "
        f"({len(uniq)} unique)")
    PNG_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        b = p.chromium.launch()
        if todo_svg:
            pg = b.new_page()
            pg.set_content(_MJ_HTML)
            pg.wait_for_function(
                "() => window.MathJax && MathJax.startup && MathJax.startup.document",
                timeout=60000)
            pg.evaluate("() => MathJax.startup.promise")
            for k in todo_svg:
                la, d = uniq[k]
                r = pg.evaluate("""([tex,disp])=>{try{
                    const c=MathJax.tex2svg(tex,{display:disp});
                    const s=c.querySelector('svg');
                    return s?s.outerHTML:'';}catch(e){return '';}}""",
                    [_process_latex(la), d])
                svg_c[k] = r
            pg.close()
        if todo_png:
            ctx = b.new_context(device_scale_factor=PNG_SCALE)
            pg = ctx.new_page()
            pg.set_content(_KATEX_HTML)
            pg.wait_for_function("typeof katex !== 'undefined'")
            for k in todo_png:
                la, d = uniq[k]
                dims = pg.evaluate("""([tex,disp])=>{
                    const host=document.getElementById('host'), bl=document.getElementById('bl');
                    host.innerHTML='';
                    katex.render(tex, host, {throwOnError:false, displayMode:disp});
                    const el=host.querySelector(disp?'.katex-display':'.katex')||host;
                    const r=el.getBoundingClientRect(), br=bl.getBoundingClientRect();
                    return {w:r.width, h:r.height, depth:Math.max(0, r.bottom-br.bottom)};
                }""", [_process_latex(la), d])
                el = pg.query_selector(".katex-display" if d else ".katex")
                if el:
                    (PNG_DIR / f"{k}.png").write_bytes(el.screenshot())  # opaque white bg
                    meta_c[k] = {"w_em": dims["w"] / FONT_PX,
                                 "h_em": dims["h"] / FONT_PX,
                                 "depth_em": dims["depth"] / FONT_PX}
            pg.close()
        b.close()

    if todo_svg:
        SVG_CACHE.write_text(json.dumps(svg_c, ensure_ascii=False), encoding="utf-8")
    if todo_png:
        PNG_META.write_text(json.dumps(meta_c, ensure_ascii=False), encoding="utf-8")
    reset_caches()
    log(f"math assets: rendered {len(todo_svg)} SVG + {len(todo_png)} PNG")
    return len(todo_svg) + len(todo_png)
