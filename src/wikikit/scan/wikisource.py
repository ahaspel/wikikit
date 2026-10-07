"""A page's scan from the volume's DjVu on Wikisource/Commons: rendered at the
smallest standard width that holds the file's own, fetched once and cached under `cache`
(data/scans/ — never committed).  The volume's file is the book's own
`Corpus.scan_name`; the page is the DjVu's page, the number Wikisource gives
it.  For the pages a book transcribes itself (`wikikit.scan`)."""
from __future__ import annotations

import io
import re
import time
from pathlib import Path

import requests
from PIL import Image

from wikikit.corpora import current_corpus
from wikikit.source_pages import volume_dir

API = "https://en.wikisource.org/w/api.php"


def file_title(volume: int) -> str:
    """The volume's DjVu file: "File:EB1911 - Volume 01.djvu"."""
    return "File:" + current_corpus().scan_name(volume)


def page_url(volume: int, page: int) -> str:
    """The page on Wikisource — what a transcription names as its source."""
    return f"https://en.wikisource.org/wiki/{current_corpus().page_title(volume, page).replace(' ', '_')}"


#: volume -> (a page-1 rendering's address, the width to ask for).
_THUMB: dict[int, tuple[str, int]] = {}
_PAGE_WIDTH = re.compile(r"/page1-\d+px-")
#: The widths Wikimedia's renderer serves (https://w.wiki/GHai): any other
#: is refused (400).  An EB1911 volume is 1880 px wide; 1920 is its rendering.
STANDARD_WIDTHS = (1280, 1920, 3840)


def rendering_url(volume: int, page: int, headers: dict) -> str:
    """Where the page is rendered at the smallest standard width that holds
    the file's own.  Two API calls a VOLUME, not a page — Wikisource allows
    about 500 requests an hour, and a 400-page run asking per page would stop
    halfway: every page's address follows page 1's.  The width is written
    in, not taken from the answer, which names a smaller standard size
    (1280 px for an 1880-px file)."""
    if volume not in _THUMB:
        info = requests.get(API, params={
            "action": "query", "titles": file_title(volume), "prop": "imageinfo",
            "iiprop": "size", "format": "json", "formatversion": 2},
            headers=headers, timeout=60).json()["query"]["pages"][0]["imageinfo"][0]
        width = info["width"]
        thumb = requests.get(API, params={
            "action": "query", "titles": file_title(volume), "prop": "imageinfo", "iiprop": "url",
            "iiurlwidth": width, "iiurlparam": f"page1-{width}px", "format": "json",
            "formatversion": 2}, headers=headers, timeout=60).json()["query"]["pages"][0]["imageinfo"][0]
        if not _PAGE_WIDTH.search(thumb["thumburl"]):
            raise ValueError(f"unexpected rendering address for {file_title(volume)}: {thumb['thumburl']}")
        _THUMB[volume] = thumb["thumburl"], next((w for w in STANDARD_WIDTHS if w >= width),
                                                 STANDARD_WIDTHS[-1])
    url, width = _THUMB[volume]
    return _PAGE_WIDTH.sub(f"/page{page}-{width}px-", url, count=1)


def page_image(volume: int, page: int, agent: str, cache: Path = Path("data/scans")) -> Image.Image:
    """The page's scan.  `agent` is the book's User-Agent, naming who asks."""
    path = volume_dir(volume, cache) / f"page{page:04d}.png"
    if not path.exists():
        headers = {"User-Agent": agent}
        r = requests.get(rendering_url(volume, page, headers), headers=headers, timeout=180)
        r.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.open(io.BytesIO(r.content)).save(path)
        time.sleep(1)                      # one page a second: Wikisource's servers are a commons
    return Image.open(path)
