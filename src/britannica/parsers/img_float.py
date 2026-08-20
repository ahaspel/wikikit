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
parameter name and ``=``.  A caption value can nest templates to ANY depth
(``{{center|{{lh|88%|{{smaller|{{sc|Fig.}} 1.}}}}}}`` is four), which is why
the parameter list is split rather than shape-matched.
"""
from __future__ import annotations

import re
from typing import NamedTuple

from britannica.wikitext import split_top_pipes


# ``{{img float}}`` uses ``file=``/``cap=``/``align=``; the ``{{figure}}``
# variant uses the synonyms ``image=``/``caption=``/``position=`` (and an
# ``image=`` value may carry a ``File:``/``Image:`` prefix).  Accepting both
# vocabularies here recovers the ~50 ``{{figure|image=…}}`` figures the
# ``file=``-only pattern dropped (silently, in the old producer too).
_FILE_RE = re.compile(
    r"\|\s*(?:file|image)\s*=\s*(?:(?:File|Image):\s*)?([^|}\n]+)", re.IGNORECASE)

# The caption parameter, in every name the two vocabularies use.  `{{img float}}`
# says `cap=`; `{{figure}}` says `caption=` — and, in all 50 of its EB1911
# instances, `bottomcaption=`.
#
# That last one is why CARNIVORA showed seven figures with no legend under any of
# them.  When `image=` was added to `_FILE_RE` to recover the `{{figure}}` family,
# the matching caption synonym was not: the images came back and their captions
# did not, which reads as "the figures are fine" from a coverage count and as a
# mutilated article to a reader ([[feedback_content_integrity_over_count]]).
#
# `topcaption=` is deliberately ABSENT: it does not occur in the corpus (the
# source is static, so that is final), and accepting it would place a legend the
# source puts ABOVE the plate below it — a wrong render for a name we have never
# seen ([[feedback_report_is_not_ought]]).
_CAPTION_KEYS = ("cap", "caption", "bottomcaption")
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

    # The caption is read by SPLITTING the parameter list, not by matching a
    # nested-brace shape.  The pattern this replaces spelled two levels of `{{…}}`
    # out by hand and therefore returned NOTHING at three — and EB1911's figure
    # legends are exactly where the source stacks templates deepest
    # (`{{c|{{Fs|92%|{{sc|Fig}}. 3.—Skull of ''Eupleres goudoti''.}}}}`).  A regex
    # that enumerates depth is fake recursion: it recognises the depths it wrote
    # down and drops the rest silently ([[feedback_recursion_is_recognition]]).
    # `split_top_pipes` has no depth to exceed.
    caption = ""
    for part in split_top_pipes(anchored)[1:]:
        key, eq, value = part.partition("=")
        if eq and key.strip().lower() in _CAPTION_KEYS:
            caption = value.strip()
            break

    w_m = _WIDTH_RE.search(anchored)
    width = int(w_m.group(1)) if w_m else None
    a_m = _ALIGN_RE.search(anchored)
    align = a_m.group(1).lower().replace("centre", "center") if a_m else None
    return ImgFloat(filename, caption, width, align)
