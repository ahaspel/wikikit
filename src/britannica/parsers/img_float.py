"""Single source-of-truth parser for the Wikisource ``{{img float}}`` template.

Used to live as three independent regexes — one in
``pipeline.stages.elements`` (for body rendering), one in
``pipeline.stages.extract_images`` (for ArticleImage rows), and one in
the pre-split ``clean_pages`` (a salvage fallback for leaked templates;
since removed when ``prepare_wikitext`` was tightened to two ops).
Each had its own subtly-different ``\\|file=…`` pattern.
A 2026-05 rebuild silently dropped 110 image references because two
of the three regexes lacked whitespace tolerance around the
``=`` — the renderer emitted ``{{IMG:…}}`` markers that pointed at
files no one had downloaded.

Parameters live in any order, separated by ``|``, and the template is
often pretty-printed across multiple lines with whitespace around the
parameter name and ``=``.  Caption values can contain nested templates
up to two levels deep (``{{EB1911 Fine Print|{{sc|Fig.}} 4.—…}}``).
"""
from __future__ import annotations

import re
from typing import NamedTuple


_FILE_RE = re.compile(r"\|\s*file\s*=\s*([^|}\n]+)", re.IGNORECASE)

# Caption value can include nested ``{{…{{…}}…}}`` up to two levels.
_CAPTION_RE = re.compile(
    r"\|\s*cap\s*=\s*((?:[^|{}]|\{\{(?:[^{}]|\{\{[^{}]*\}\})*\}\})+)",
    re.IGNORECASE,
)


class ImgFloat(NamedTuple):
    filename: str
    caption: str  # raw caption with formatting markers; "" if absent


def parse(body: str) -> ImgFloat | None:
    """Parse the *inner* body of an ``{{img float | … }}`` template.

    ``body`` is the content between ``{{`` and ``}}`` — typically
    something like ``"img float |file=Foo.jpg |cap=A caption"``.  A
    leading ``|`` is prepended internally so the first parameter still
    matches the leading-pipe regex anchor.

    Returns ``None`` when no ``file=`` parameter is found (the template
    is unsalvageable without it).  Caption is empty string when absent.
    """
    anchored = "|" + body
    file_m = _FILE_RE.search(anchored)
    if not file_m:
        return None
    filename = file_m.group(1).strip()
    cap_m = _CAPTION_RE.search(anchored)
    caption = cap_m.group(1).strip() if cap_m else ""
    return ImgFloat(filename, caption)
