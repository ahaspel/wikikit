"""Single source-of-truth parser for the Wikisource ``{{img float}}`` template.

Used to live as three independent regexes — one in
``pipeline.stages.elements`` (for body rendering, the sole survivor), one in
the old ``extract_images`` stage (deleted with the ArticleImage table), and one
in the pre-split ``clean_pages`` (a salvage fallback for leaked templates;
removed when ``prepare_wikitext`` was tightened to two ops).
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


# ``{{img float}}`` uses ``file=``/``cap=``/``align=``; the ``{{figure}}``
# variant uses the synonyms ``image=``/``caption=``/``position=`` (and an
# ``image=`` value may carry a ``File:``/``Image:`` prefix).  Accepting both
# vocabularies here recovers the ~50 ``{{figure|image=…}}`` figures the
# ``file=``-only pattern dropped (silently, in the old producer too).
_FILE_RE = re.compile(
    r"\|\s*(?:file|image)\s*=\s*(?:(?:File|Image):\s*)?([^|}\n]+)", re.IGNORECASE)

# Caption value can include nested ``{{…{{…}}…}}`` up to two levels.
_CAPTION_RE = re.compile(
    r"\|\s*(?:caption|cap)\s*=\s*((?:[^|{}]|\{\{(?:[^{}]|\{\{[^{}]*\}\})*\}\})+)",
    re.IGNORECASE,
)
# ``width=Npx`` (the only size form these templates use); capture the
# leading width, tolerate a trailing ``xMpx`` height defensively.
_WIDTH_RE = re.compile(r"\|\s*width\s*=\s*(\d+)(?:x\d+)?px", re.IGNORECASE)
_ALIGN_RE = re.compile(
    r"\|\s*(?:align|position)\s*=\s*(center|centre|left|right)\b", re.IGNORECASE)


class ImgFloat(NamedTuple):
    filename: str
    caption: str  # raw caption with formatting markers; "" if absent
    width: int | None = None   # px, from width=Npx; None if absent
    align: str | None = None   # center | left | right; None when the
    #                            template gives no explicit align keyword


def parse(body: str) -> ImgFloat | None:
    """Parse the *inner* body of an ``{{img float | … }}`` template.

    ``body`` is the content between ``{{`` and ``}}`` — typically
    something like ``"img float |file=Foo.jpg |cap=A caption |width=200px"``.
    A leading ``|`` is prepended internally so the first parameter still
    matches the leading-pipe regex anchor.

    Returns ``None`` when no ``file=`` parameter is found (the template
    is unsalvageable without it).  Caption is empty string when absent.

    Only the *explicit* ``align=`` keyword is carried; ``align`` is None
    when absent (71% of img-floats).  The template's implicit default
    float side is deliberately NOT asserted here — the source data
    doesn't say it, and the explicit values skew ``left`` over ``right``,
    so a default would be an unverified guess on the majority case.
    """
    anchored = "|" + body
    file_m = _FILE_RE.search(anchored)
    if not file_m:
        return None
    filename = file_m.group(1).strip()
    cap_m = _CAPTION_RE.search(anchored)
    caption = cap_m.group(1).strip() if cap_m else ""
    w_m = _WIDTH_RE.search(anchored)
    width = int(w_m.group(1)) if w_m else None
    a_m = _ALIGN_RE.search(anchored)
    align = a_m.group(1).lower().replace("centre", "center") if a_m else None
    return ImgFloat(filename, caption, width, align)
