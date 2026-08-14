"""EPUB image diet — display-resolution re-encodes, content-keyed cache.

The image store holds scan-resolution files (1.37GB across the 10,660 the book
references); the reader displays a figure at the ~590px body column.  The diet caps
the long side at DIET_MAXDIM (retina-sharp at column width) and re-encodes best-of:
JPEG for halftones/photos, palette PNG for line art — grayscale when the plate is
actually monochrome (EB1911 is a B/W print; the colour plates keep their colour).
Full-resolution stays on the site.

Cache: data/derived/epub_img_cache/<sha1(source bytes + params)>.<ext> — a rebuild
re-encodes only new/changed images (the math-assets pattern).  If the re-encode is
not smaller, the original bytes ship unchanged.
"""
import hashlib
import io
import os

DIET_MAXDIM = 1000
DIET_JPEG_Q = 60
_GRAY_TOL = 12

CACHE_DIR = os.path.join("data", "derived", "epub_img_cache")


def _params_tag():
    # a1: alpha-aware — v1 dropped the alpha channel via convert(), compositing
    # transparent glyph backgrounds onto BLACK (solid-black letterforms in A).
    return f"d{DIET_MAXDIM}q{DIET_JPEG_Q}g{_GRAY_TOL}a1"


def diet_image(src_path, log=None):
    """→ (bytes, ext) — the display-sized encoding of the image at ``src_path``,
    or the original bytes (with the original extension) when the source is already
    small/undecodable/GIF.  ``ext`` includes the dot."""
    raw = open(src_path, "rb").read()
    orig_ext = os.path.splitext(src_path)[1].lower() or ".png"
    if orig_ext == ".gif" or len(raw) < 12 * 1024:      # tiny/animated: not worth touching
        return raw, orig_ext

    key = hashlib.sha1(raw + _params_tag().encode()).hexdigest()[:16]
    for ext in (".jpg", ".png"):
        p = os.path.join(CACHE_DIR, key + ext)
        if os.path.exists(p):
            return open(p, "rb").read(), ext
    marker = os.path.join(CACHE_DIR, key + ".orig")     # "original wins" memo
    if os.path.exists(marker):
        return raw, orig_ext

    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return raw, orig_ext

    # Transparency is CONTENT (letterform glyphs sit on the page background).
    # convert("L"/"RGB") DROPS alpha — transparent pixels keep their arbitrary
    # (usually black) RGB, blacking the whole image out.  Alpha-bearing images
    # keep alpha: no JPEG (it has none), palette PNG via FASTOCTREE.
    has_alpha = (im.mode in ("RGBA", "LA", "PA")
                 or (im.mode == "P" and "transparency" in im.info))

    w, h = im.size
    if max(w, h) > DIET_MAXDIM:
        r = DIET_MAXDIM / max(w, h)
        if im.mode == "P":
            im = im.convert("RGBA" if has_alpha else "RGB")
        im = im.resize((max(1, int(w * r)), max(1, int(h * r))), Image.LANCZOS)

    if has_alpha:
        rgba = im.convert("RGBA")
        try:
            octree = getattr(Image, "Quantize", Image).FASTOCTREE
            pbuf = io.BytesIO()
            rgba.quantize(128, method=octree).save(pbuf, "PNG", optimize=True)
            out, ext = pbuf.getvalue(), ".png"
        except Exception:
            pbuf = io.BytesIO()
            rgba.save(pbuf, "PNG", optimize=True)
            out, ext = pbuf.getvalue(), ".png"
    else:
        rgb = im.convert("RGB")
        probe = rgb.resize((32, 32))
        gray = all(abs(r - g) < _GRAY_TOL and abs(g - b) < _GRAY_TOL
                   for r, g, b in probe.get_flattened_data())
        jbuf = io.BytesIO()
        (im.convert("L") if gray else rgb).save(jbuf, "JPEG", quality=DIET_JPEG_Q, optimize=True)
        try:
            pbuf = io.BytesIO()
            (im.convert("L") if gray else rgb).quantize(64 if gray else 128).save(
                pbuf, "PNG", optimize=True)
        except Exception:
            pbuf = jbuf
        out, ext = min((jbuf.getvalue(), ".jpg"), (pbuf.getvalue(), ".png"),
                       key=lambda t: len(t[0]))

    os.makedirs(CACHE_DIR, exist_ok=True)
    if len(out) >= len(raw):
        open(marker, "wb").write(b"")
        return raw, orig_ext
    open(os.path.join(CACHE_DIR, key + ext), "wb").write(out)
    return out, ext
