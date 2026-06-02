"""Image / image-float / DjVu-crop / chart2 element handlers.

All emit ``{{IMG:filename|caption}}`` or ``{{IMG:filename}}`` markers.
"""

from __future__ import annotations

import re

from britannica.image_assets import CHART2_IMAGES
from britannica.parsers import img_float as _img_float_parser
from britannica.pipeline.stages.elements._context import ElementContext
from britannica.pipeline.stages.elements._dual_line import _split_top_level_pipe
from britannica.captions import clean_caption


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

    Applies ``clean_caption`` so every producer emits a clean caption as
    part of producing — not a downstream renormalization pass.  Idempotent,
    so callers that already cleaned their caption can route through it
    harmlessly.  Every IMG-emitting producer should build markers via this
    helper rather than an ad-hoc f-string."""
    if caption:
        caption = clean_caption(caption)
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


def _process_image_float(inner: str, text_transform) -> str:
    """Convert img float content to {{IMG:filename|caption}}.

    Inner is the content between {{ and }}, e.g.:
    'img float |file=Foo.jpg |cap=A caption |width=240px'
    or the BOILER multi-line form with whitespace around equals:
    '''img float
     | file = Foo.jpg
     | cap = {{sc|Fig.}} 4.—…
     | width = 200px'''
    """
    parsed = _img_float_parser.parse(inner)
    if parsed is None:
        return ""
    caption = parsed.caption
    if caption:
        caption = text_transform(caption)
    return build_img_marker(
        parsed.filename, caption or None,
        align=parsed.align, width=parsed.width)


_CSS_CROP_RE = re.compile(
    r"\{\{Css image crop\s*\n(.*?)\}\}", re.DOTALL | re.IGNORECASE
)


def _parse_crop_param(body: str, name: str) -> str:
    m = re.search(rf"\|{name}\s*=\s*([^\n|]*)", body, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _process_djvu_crop(raw: str, text_transform, context: ElementContext) -> str:
    """Process a table containing a {{Css image crop}} template.

    Extracts the crop filename and caption from the table, producing
    {{IMG:djvu_volNN_pageNNNN_cropN.jpg|caption}}.

    Crop indices are tracked in context.djvu_crop_counters to match
    the order used by tools/download_djvu_crops.py.
    """
    crop_m = _CSS_CROP_RE.search(raw)
    if not crop_m:
        return ""

    body = crop_m.group(1)
    image = _parse_crop_param(body, "Image")
    if not image:
        return ""

    # Build the local filename
    if image.endswith(".djvu"):
        page_str = _parse_crop_param(body, "Page")
        if not page_str:
            return ""
        page = int(page_str)
        vol_m = re.search(r"Volume (\d+)", image)
        vol = int(vol_m.group(1)) if vol_m else context.volume
        counters = context.djvu_crop_counters
        key = (vol, page)
        idx = counters.get(key, 0)
        counters[key] = idx + 1
        filename = f"djvu_vol{vol:02d}_page{page:04d}_crop{idx}.jpg"
    else:
        filename = image.replace(" ", "_")

    # Extract caption from the table (everything that isn't the crop template
    # or wiki table markup)
    caption_text = raw[:crop_m.start()] + raw[crop_m.end():]
    # Strip table delimiters and markup
    caption_text = re.sub(r"^\{\|[^\n]*\n?", "", caption_text)
    caption_text = re.sub(r"\n?\|\}\s*$", "", caption_text)
    caption_text = re.sub(r"^\|[\-\+].*$", "", caption_text, flags=re.MULTILINE)
    # Strip cell-attribute prefix on each cell line: any leading run of
    # `attr=value` pairs (quoted or unquoted) followed by the cell-content
    # pipe.  ALPHABET uses `|rowspan=4|`, BANTU `|colspan=2|`, plate pages
    # `|align="center" valign="top"|`.  The previous pattern handled only
    # `colspan=...` — broader leak surface than expected.
    caption_text = re.sub(
        r'^\|\s*(?:(?:align|valign|colspan|rowspan|width|style|class|'
        r'cellpadding|cellspacing|bgcolor|border|nowrap|height|id|scope)'
        r'\s*=\s*(?:"[^"]*"|\S+)\s*)+\|\s*',
        "", caption_text, flags=re.MULTILINE,
    )
    # Plain `|` cell-opener (no attrs) — strip the leading pipe.
    caption_text = re.sub(r"^\|\s*", "", caption_text, flags=re.MULTILINE)
    caption_text = re.sub(r"^\!", "", caption_text, flags=re.MULTILINE)
    # Collapse to single line
    caption_text = re.sub(r"\s*<br\s*/?>", " ", caption_text, flags=re.IGNORECASE)
    caption_text = re.sub(r"\s*\n\s*", " ", caption_text)
    caption_text = re.sub(r"  +", " ", caption_text).strip()

    if caption_text:
        caption_text = text_transform(caption_text)
        caption_text = clean_caption(caption_text)
        return f"{{{{IMG:{filename}|{caption_text}}}}}"
    return f"{{{{IMG:{filename}}}}}"


# `{{raw image|EB1911 - Volume N.djvu/PPP}}` — EB1911 alternate image syntax for
# a full-page DjVu scan; the arg is a DjVu page-ref normalized to the local
# full-page render `djvu_volNN_pagePPPP.jpg` (matching download_djvu_crops.py).
_RAW_IMAGE_ARG_RE = re.compile(r"\{\{\s*raw\s+image\s*\|([^{}|]+)\}\}", re.IGNORECASE)
_RAW_DJVU_REF_RE = re.compile(r"EB1911\s*-\s*Volume\s*(\d+)\.djvu/(\d+)", re.IGNORECASE)
_RAW_CAPTION_RE = re.compile(
    r"\{\{\s*c\s*\|((?:[^{}]|\{\{[^{}]*\}\})*)\}\}", re.IGNORECASE)


def _process_raw_image(raw: str, text_transform) -> str:
    """`{{raw image|…}}` → `{{IMG:…}}`.  A DjVu page-ref arg becomes the local
    full-page render; any other arg passes through as a filename.  Folds an
    optional trailing `{{c|caption}}` the walker carried along."""
    m = _RAW_IMAGE_ARG_RE.match(raw)
    if not m:
        return raw
    arg = m.group(1).strip()
    dref = _RAW_DJVU_REF_RE.match(arg)
    if dref:
        filename = f"djvu_vol{int(dref.group(1)):02d}_page{int(dref.group(2)):04d}.jpg"
    else:
        # Keep spaces — a plain filename resolves as-is, like every other IMAGE
        # producer (MediaWiki treats space/underscore alike; spaces match the
        # regular [[File:…]] output and the old bundler).
        filename = arg
    cap_m = _RAW_CAPTION_RE.search(raw, m.end())
    caption = text_transform(cap_m.group(1).strip()) if cap_m else ""
    return build_img_marker(filename, caption or None)


# `{{Plain image with caption|image=File:…|align=…|width=…px|caption=…|caption
# position=…}}` — a Wikisource named-parameter figure macro (MAP, vol 17, uses
# ~17 of them for its historical-cartography plates).  Same destination as every
# other image producer — a `{{IMG:…}}` marker — parsed from the named params:
# `image=` is the File ref (drop the `File:`/`Image:` prefix), `align`/`width`
# map straight onto the marker's layout metadata, `caption` is a normal inline
# caption run.  `caption position` is layout-only (our caption always renders
# under the image) and ignored.  Without this the walker doesn't recognize the
# macro and all ~17 figures fall to body-text's catch-all and vanish.
def _process_plain_image(inner: str, text_transform) -> str:
    params: dict[str, str] = {}
    # inner is `Plain image with caption|key=val|key=val…`; segment 0 is the
    # template name, the rest are named params (split at depth-0 pipes so a
    # nested `caption={{center|…|…}}` value stays whole).
    for seg in _split_top_level_pipe(inner)[1:]:
        if "=" not in seg:
            continue
        key, val = seg.split("=", 1)
        params[key.strip().lower()] = val.strip()
    filename = re.sub(r"^\s*(?:File|Image):", "", params.get("image", ""),
                      flags=re.IGNORECASE).strip()
    if not filename:
        return ""
    align = _ALIGN_KEYWORDS.get(params.get("align", "").lower())
    width, height = _parse_image_size(params.get("width", ""))
    caption = params.get("caption", "")
    if caption:
        caption = text_transform(caption)
    return build_img_marker(filename, caption or None,
                            align=align, width=width, height=height)


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
