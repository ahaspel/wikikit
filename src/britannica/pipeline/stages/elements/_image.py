"""Image / image-float / DjVu-crop / chart2 element handlers.

All emit ``{{IMG:filename|caption}}`` or ``{{IMG:filename}}`` markers.
"""

from __future__ import annotations

import hashlib
import re

from britannica.image_assets import GENEALOGY_IMAGES
from britannica.pipeline.stages.elements._context import ElementContext
from britannica.pipeline.stages.elements._dual_line import _split_top_level_pipe


def _img_bracket_meta(raw: str) -> tuple[str, int | None, str | None]:
    """``[[File:X|200px|left|…]]`` → ``(filename, width, align)``.  The bracket's
    trailing text is ``alt``/caption — read by the caller (`_parse_image`), not here."""
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
    return fn, width, align


def _img_marker(raw: str) -> str:
    """Bare ``[[File:…]]`` / ``[[Image:…]]`` block image → ``{{IMG:…}}`` leaf, built
    through the ONE constructor (`build_img_marker`) — no ad-hoc f-string.  The
    bracket's trailing text is ``alt``, not a caption, and is dropped
    ([[feedback_no_caption_concept]])."""
    fn, width, align = _img_bracket_meta(raw)
    return build_img_marker(fn, align=align, width=width)


# A `thumb`/`frame` image renders its trailing positional as a CAPTION below the
# image (MediaWiki semantics), unlike a plain image whose trailing text is alt.
# These recognize the layout params so the single image producer can peel the
# caption off the tail.
_THUMB_KW = ("thumb", "thumbnail", "frame")
_IMG_LAYOUT_PARAM = re.compile(
    r"^\s*(?:\d+\s*px|x\d+\s*px|\d+x\d+\s*px|upright(?:=[\d.]+)?|"
    r"center|centre|right|left|none|thumb|thumbnail|frame|frameless|border|"
    r"\w+\s*=[^|]*)\s*$", re.IGNORECASE)


def _thumb_caption_raw(raw: str) -> str:
    """The trailing CAPTION of a `thumb`/`frame` image, RAW, or "".

    MediaWiki renders a thumb/frame image's trailing positional below the image
    — a real caption — whereas a plain image's trailing text is alt.  The single
    image producer peels this off and emits `{{IMG:…}}«BR»<caption>` (the
    `[[File:]]<br>caption` shape every other figure has).  "" for a plain image."""
    inner = re.sub(r"\]\]\s*$", "", re.sub(r"^\s*\[\[(?:File|Image):", "", raw, flags=re.I))
    parts = inner.split("|")
    if not any(p.strip().lower() in _THUMB_KW for p in parts[1:]):
        return ""
    for i in range(1, len(parts)):
        if not _IMG_LAYOUT_PARAM.match(parts[i]):
            return "|".join(parts[i:]).strip()
    return ""


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
_GENEALOGY_INNER_REF = re.compile(r"<ref\b[^>]*>[\s\S]*?</ref>", re.IGNORECASE)


def _process_genealogy(raw: str, context: ElementContext, recurse) -> str:
    """A ``{{chart2}}`` / ``{{familytree}}`` / ``{{Tree chart}}`` genealogical-tree
    block → its pre-cropped page-scan image (the grid macro renders to a mess).

    The crops are a fixed, corpus-verified set — exactly seven, each on a distinct
    volume (``GENEALOGY_IMAGES``) — so the lookup keys on volume.  A tree node can
    carry an inner ``<ref>`` footnote (vol-28 chart2; vol-7 COWPER familytree); the
    tree becomes a flat image so the note can't sit on a node — ``recurse`` it to a
    ``«FN»`` emitted after the image, where it survives as an ordinary article
    footnote.  (The old preprocess chart2 substitution dropped that vol-28 ref; the
    familytree one rescued it.  Both now flow through here, handled alike.)"""
    vol = context.volume
    filename = next(
        (fn for (v, _p), fn in GENEALOGY_IMAGES.items() if v == vol), None)
    if not filename:
        return ""   # unknown volume — strip rather than leak the raw grid macro
    refs = "".join(recurse(r) for r in _GENEALOGY_INNER_REF.findall(raw))
    return build_img_marker(filename, "Genealogical table") + refs
