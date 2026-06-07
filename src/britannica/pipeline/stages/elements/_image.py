"""Image / image-float / DjVu-crop / chart2 element handlers.

All emit ``{{IMG:filename|caption}}`` or ``{{IMG:filename}}`` markers.
"""

from __future__ import annotations

import hashlib
import re

from britannica.image_assets import CHART2_IMAGES
from britannica.parsers import img_float as _img_float_parser
from britannica.pipeline.stages.elements._context import ElementContext
from britannica.pipeline.stages.elements._dual_line import _split_top_level_pipe


def _img_marker(raw: str) -> str:
    """Bare ``[[File:…]]`` / ``[[Image:…]]`` block image → ``{{IMG:…}}`` leaf (filename +
    px-width / center-left-right align).  The bracket's trailing text is ``alt``, not a
    caption, and is dropped ([[feedback_no_caption_concept]]).  Relocated verbatim from
    the deleted ``_figure_faithful`` shadow producer."""
    inner = re.sub(r"\]\]\s*$", "", re.sub(r"^\s*\[\[(?:File|Image):", "", raw, flags=re.I))
    parts = [p.strip() for p in inner.split("|")]
    fn = parts[0]
    width = align = None
    for p in parts[1:]:
        mw = re.match(r"(\d+)\s*px$", p)
        if mw:
            width = int(mw.group(1))
        elif p.lower() in ("center", "right", "left"):
            align = p.lower()
    meta = (f"|align={align}" if align else "") + (f"|width={width}" if width else "")
    return f"{{{{IMG:{fn}{meta}}}}}"


def build_img_marker(
    filename: str,
    caption: str | None = None,
    *,
    align: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> str:
    """The single constructor for ``{{IMG:…}}`` markers.

    Emits ``{{IMG:filename[|align=…][|width=N][|height=N][|caption]}}`` —
    layout metadata first (in that fixed order, only when non-default),
    caption last so it may contain ``|`` / ``=`` freely.  The grammar is
    defined once in ``britannica.markers`` (``IMG_PARTS_RE``) and decoded
    only by the renderer; see that module for the contract.

    ``align``/``width``/``height`` are *optional* — producers that don't
    have the source params (the layout/plate/table sites that work from a
    bare filename) call ``build_img_marker(fn, cap)`` exactly as before and
    get the identical metadata-free marker.

    Does NOT clean the caption — the caption is the caller's already-recursed content
    (the producer template: transform the outer, recurse the inner).  Re-cleaning it here
    would maul the «SC»/«I»/… markers the recursion produced.  This helper only FORMATS;
    every IMG-emitting producer builds markers through it rather than an ad-hoc f-string."""
    fields: list[str] = []
    if align:
        fields.append(f"align={align}")
    if width:
        fields.append(f"width={width}")
    if height:
        fields.append(f"height={height}")
    if caption:
        fields.append(caption)
    if fields:
        return f"{{{{IMG:{filename}|{'|'.join(fields)}}}}}"
    return f"{{{{IMG:{filename}}}}}"


# MediaWiki image sizing: ``Npx`` = width, ``xNpx`` = height (width auto),
# ``WxHpx`` = bounding box.  Returns ``(width, height)`` in px, either may
# be None.  ``upright``/``upright=F`` (scale factor, ~0 occurrences) and
# anything unrecognized → ``(None, None)`` (renderer falls back to default).
_SIZE_TOKEN_RE = re.compile(r"(\d+)?(?:x(\d+))?px$", re.IGNORECASE)


def _parse_image_size(token: str) -> tuple[int | None, int | None]:
    m = _SIZE_TOKEN_RE.fullmatch(token.strip())
    if not m:
        return (None, None)
    w = int(m.group(1)) if m.group(1) else None
    h = int(m.group(2)) if m.group(2) else None
    return (w, h)


# Explicit alignment keywords in an image's own params.  ``centre`` folds
# to ``center``; ``none`` is not an alignment (it suppresses the default
# float, i.e. block flow — our default already), so it is ignored.
_ALIGN_KEYWORDS = {"center": "center", "centre": "center",
                   "left": "left", "right": "right"}
_DISPLAY_KEYWORDS = frozenset({
    "thumb", "thumbnail", "frameless", "frame", "border", "none", "upright"})


def _process_image(inner: str, text_transform, default_align: str | None = None) -> str:
    """Convert image content (already stripped of [[File:...]]) to {{IMG:filename|clean caption}}.

    ``default_align`` is the alignment to use when the image's own params don't
    name one — used by ``_process_image_from_raw`` to stamp ``align=inline``
    on a bare image whose raw shows no trailing separator (walker structural
    signal: same-line content follows ``]]``, so nothing was absorbed).  When
    the image params DO carry an alignment (``[[File:foo|450px|center]]``),
    that explicit choice wins and the default is ignored."""
    # Check for external caption (from plate pages: image + caption on next line)
    ext_caption = ""
    if "|EXTCAP:" in inner:
        inner, ext_caption = inner.rsplit("|EXTCAP:", 1)

    parts = [p.strip() for p in inner.split("|")]
    filename = parts[0]

    # Single forward pass over the params, partitioning into layout
    # metadata (align/size — carried into the marker) and positional
    # values (the caption is the last positional, matching the prior
    # "last non-keyword part" rule).
    align: str | None = None
    width: int | None = None
    height: int | None = None
    positional: list[str] = []
    for part in parts[1:]:
        lower = part.lower()
        if lower in _ALIGN_KEYWORDS:
            if align is None:
                align = _ALIGN_KEYWORDS[lower]
            continue
        if lower in _DISPLAY_KEYWORDS or lower.startswith("upright="):
            continue
        w, h = _parse_image_size(lower)
        if w or h:
            if w and width is None:
                width = w
            if h and height is None:
                height = h
            continue
        if "=" in part:  # named parameter (link=, alt=, …)
            continue
        positional.append(part)
    caption = positional[-1] if positional else ""

    # Use external caption if no inline caption
    if not caption and ext_caption:
        caption = ext_caption

    if caption:
        caption = text_transform(caption)
    return build_img_marker(
        filename, caption or None, align=align or default_align,
        width=width, height=height)


def _parse_crop_param(body: str, name: str) -> str:
    # Value runs to the next `\n`, `|`, or `}` — the last matters once the crop
    # is newline-collapsed (the final `|oLeft=10}}` must yield `10`, not `10}}`).
    m = re.search(r"\|" + re.escape(name) + r"\s*=\s*([^\n|}]*)",
                  body, re.IGNORECASE)
    return m.group(1).strip() if m else ""


# Crop geometry defaults — must match the download tool's scan defaults so the
# producer, the tool, and the rename all derive byte-identical filenames.
_CROP_GEOM = (("bSize", 600), ("cWidth", 600), ("cHeight", 600),
              ("oTop", 0), ("oLeft", 0))


def crop_filename(image: str, page, bsize, cwidth, cheight, otop, oleft) -> str:
    """STATELESS crop filename from EXPLICIT params — a pure function of the
    crop's IDENTITY (DjVu volume + page + geometry rectangle).  No positional
    counter, no document order, no context.  THE one filename authority, called
    by both `djvu_crop_filename` (body-parsing, for the producer) and
    `download_djvu_crops.py` — so they agree by construction."""
    vm = re.search(r"Volume\s+(\d+)", str(image), re.IGNORECASE)
    vol = int(vm.group(1)) if vm else 0
    geom = "|".join(str(int(x)) for x in (bsize, cwidth, cheight, otop, oleft))
    h = hashlib.sha1(geom.encode("utf-8")).hexdigest()[:8]
    return f"djvu_vol{vol:02d}_page{int(page):04d}_{h}.jpg"


def djvu_crop_filename(crop_body: str) -> str | None:
    """Body-parsing wrapper: the inside of a ``{{Css image crop … }}`` template →
    `crop_filename`, or ``None`` for a non-DjVu ``Image=`` source."""
    image = _parse_crop_param(crop_body, "Image")
    if not image.lower().endswith(".djvu"):
        return None
    page = _parse_crop_param(crop_body, "Page")
    if not page:
        return None

    def _g(name: str, default: int) -> int:
        v = _parse_crop_param(crop_body, name)
        return int(v) if v else default

    return crop_filename(image, page, *(_g(n, d) for n, d in _CROP_GEOM))


# `{{raw image|EB1911 - Volume N.djvu/PPP}}` — EB1911 alternate image syntax for
# a full-page DjVu scan; the arg is a DjVu page-ref normalized to the local
# full-page render `djvu_volNN_pagePPPP.jpg` (matching download_djvu_crops.py).
_RAW_IMAGE_ARG_RE = re.compile(r"\{\{\s*raw\s+image\s*\|([^{}|]+)\}\}", re.IGNORECASE)
_RAW_DJVU_REF_RE = re.compile(r"EB1911\s*-\s*Volume\s*(\d+)\.djvu/(\d+)", re.IGNORECASE)


# `{{Plain image with caption|image=File:…|align=…|width=…px|caption=…|caption
# position=…}}` — a Wikisource named-parameter figure macro (MAP, vol 17, uses
# ~17 of them for its historical-cartography plates).  Same destination as every
# other image producer — a `{{IMG:…}}` marker — parsed from the named params:
# `image=` is the File ref (drop the `File:`/`Image:` prefix), `align`/`width`
# map straight onto the marker's layout metadata, `caption` is a normal inline
# caption run.  `caption position` is layout-only (our caption always renders
# under the image) and ignored.  Without this the walker doesn't recognize the
# macro and all ~17 figures fall to body-text's catch-all and vanish.
def _process_chart2(raw: str, context: ElementContext) -> str:
    """Replace a chart2 genealogical tree with a pre-cropped page scan image.

    The 5 chart2 blocks in the encyclopedia have been manually cropped
    from DjVu page scans and saved as chart2_volNN_pageNNNN.jpg.
    """
    vol = context.volume
    # Try all known charts for this volume
    for (v, p), filename in CHART2_IMAGES.items():
        if v == vol:
            return f"{{{{IMG:{filename}|Genealogical table}}}}"
    # Unknown chart — strip rather than crash
    return ""


# An IMAGE element's raw is `[[File:…]]` plus an OPTIONAL trailing caption
# block (`]]\n…` / `]]\n\n…`) the walker captured as part of the same unit.
# This is the single source for splitting that raw into (file-ref inner,
# trailing caption) — the parse the producer owns, NOT the walker.  The
# walker carries the raw span unchewed; only here is it interpreted.
_IMAGE_RAW_RE = re.compile(
    r"\[\[(?:File|Image):(.+)\]\](?:\s*\n\n?(.+))?$",
    re.IGNORECASE | re.DOTALL,
)


def _split_image_raw(raw: str) -> tuple[str, str] | None:
    """`(file_ref_inner, trailing_caption)` from a raw image span, or None if
    `raw` is not an `[[File:…]]` span.  `trailing_caption` is "" when absent."""
    m = _IMAGE_RAW_RE.match(raw)
    if not m:
        return None
    return m.group(1), (m.group(2) or "")


def image_extcap_from_raw(raw: str) -> str:
    """The trailing caption (after `]]`) of a raw `[[File:…]]` span, or "".
    The honest replacement for reading a walker-folded `|EXTCAP:` tail —
    parses the caption from the carried-along raw instead."""
    sp = _split_image_raw(raw)
    return sp[1] if sp else ""


def _process_image_from_raw(raw: str, text_transform,
                            inline: bool = False) -> str:
    """Parse a raw `[[File:…]]` + optional trailing capture and produce the
    `{{IMG:…}}` marker.

    The walker has already decided block-vs-inline by emitting
    SHAPE_INLINE_IMAGE (for inline glyphs adjacent to same-line prose) or
    SHAPE_DOUBLE_BRACKET (everything else); the classifier maps those to
    the IMAGE / INLINE_IMAGE label, and the dispatch passes ``inline=True``
    here for the inline case.  The producer just acts on that signal —
    stamping ``align=inline`` as the default when the image's own params
    don't carry an alignment, and otherwise leaving alignment up to the
    image params (which still win)."""
    sp = _split_image_raw(raw)
    if sp is None:
        return raw
    inner, ext_caption = sp
    if ext_caption:
        inner = inner + "|EXTCAP:" + ext_caption
    return _process_image(
        inner, text_transform,
        default_align="inline" if inline else None)
