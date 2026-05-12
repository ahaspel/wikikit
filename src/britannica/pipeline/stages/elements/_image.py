"""Image / image-float / DjVu-crop / chart2 element handlers.

All emit ``{{IMG:filename|caption}}`` or ``{{IMG:filename}}`` markers.
"""

from __future__ import annotations

import re

from britannica.image_assets import CHART2_IMAGES
from britannica.parsers import img_float as _img_float_parser
from britannica.pipeline.stages.elements._context import ElementContext
from britannica.pipeline.stages.elements._text import clean_caption


def _process_image(inner: str, text_transform) -> str:
    """Convert image content (already stripped of [[File:...]]) to {{IMG:filename|clean caption}}."""
    # Check for external caption (from plate pages: image + caption on next line)
    ext_caption = ""
    if "|EXTCAP:" in inner:
        inner, ext_caption = inner.rsplit("|EXTCAP:", 1)

    parts = [p.strip() for p in inner.split("|")]
    filename = parts[0]

    # Extract caption (last non-keyword, non-size part)
    keywords = {"center", "left", "right", "thumb", "thumbnail", "frameless",
                "frame", "border", "upright", "none"}
    caption = ""
    for part in reversed(parts[1:]):
        lower = part.lower()
        if lower in keywords:
            continue
        if re.match(r"^\d+px$|^x\d+px$|^\d+x\d+px$", lower):
            continue
        if "=" in part:  # named parameter
            continue
        caption = part
        break

    # Use external caption if no inline caption
    if not caption and ext_caption:
        caption = ext_caption

    if caption:
        caption = text_transform(caption)
        caption = clean_caption(caption)
        return f"{{{{IMG:{filename}|{caption}}}}}"
    return f"{{{{IMG:{filename}}}}}"


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
        caption = clean_caption(caption)
    if caption:
        return f"{{{{IMG:{parsed.filename}|{caption}}}}}"
    return f"{{{{IMG:{parsed.filename}}}}}"


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
        return f"\n\n{{{{IMG:{filename}|{caption_text}}}}}\n\n"
    return f"\n\n{{{{IMG:{filename}}}}}\n\n"


def _process_chart2(raw: str, context: ElementContext) -> str:
    """Replace a chart2 genealogical tree with a pre-cropped page scan image.

    The 5 chart2 blocks in the encyclopedia have been manually cropped
    from DjVu page scans and saved as chart2_volNN_pageNNNN.jpg.
    """
    vol = context.volume
    # Try all known charts for this volume
    for (v, p), filename in CHART2_IMAGES.items():
        if v == vol:
            return f"\n\n{{{{IMG:{filename}|Genealogical table}}}}\n\n"
    # Unknown chart — strip rather than crash
    return ""


def _process_image_from_raw(raw: str, text_transform) -> str:
    """Convenience: strip `[[File:...]]` and call _process_image."""
    m = re.match(r"\[\[(?:File|Image):(.+)\]\](?:\s*\n\n?(.+))?$",
                 raw, re.IGNORECASE | re.DOTALL)
    if not m:
        return raw
    inner = m.group(1)
    ext_caption = m.group(2)
    if ext_caption:
        inner = inner + "|EXTCAP:" + ext_caption
    return _process_image(inner, text_transform)
