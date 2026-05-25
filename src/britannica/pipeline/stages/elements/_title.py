"""TITLE element producer (new super-walker architecture).

A detected article's leading heading IS its title.  This produces the title's
plain text from that heading's wikitext, handling the markup shapes correctly —
including the two the legacy ``title.clean_title`` gets wrong:

  * ``{{abbr|DISPLAY|tooltip}}`` — the shown text is the FIRST arg, not the
    tooltip.  Legacy's generic unwrap takes the last arg, turning PIETAS into
    "Latin: 'Piety'".
  * raw HTML tags (``<span>``, ``<big>``, ``<ins>``, ``<nowiki>``) — stripped
    to their inner text.  Legacy leaves them, e.g. ``<big>S</big>UCCINIC ACID``
    and ``<span …>SEMMELWEISS</span>``.

The legacy ``title.clean_title`` stays frozen — the old pipeline depends on it.
This is the producer the super-walker and the element walk use.
"""
from __future__ import annotations

import re

_FN = re.compile(r"«FN(?:\[[^\]]+\])?:.*?«/FN»", re.DOTALL)
_REF = re.compile(r"<ref[^>]*>.*?</ref>", re.DOTALL)
_REF_SELF = re.compile(r"<ref[^/]*/\s*>")
# {{abbr|DISPLAY|tooltip}} — keep the DISPLAY (first) arg.
_ABBR = re.compile(r"\{\{abbr\|([^{}|]*)\|[^{}]*\}\}", re.I)
_LINK_PIPE = re.compile(r"\[\[(?:Author:)?[^\]|]*\|([^\]]+)\]\]")
_LINK = re.compile(r"\[\[([^\]|]+)\]\]")
_SC = re.compile(r"\{\{(?:sc|asc|small[\s\-]?caps?)\|([^{}|]*)\}\}", re.I)
_UC = re.compile(r"\{\{uc\|([^{}|]*)\}\}", re.I)
_TMPL3 = re.compile(r"\{\{[^{}|]+\|[^{}]*\|([^{}|]*)\}\}")
_TMPL2 = re.compile(r"\{\{[^{}|]+\|([^{}|]*)\}\}")
_TMPL0 = re.compile(r"\{\{[^{}|]+\}\}")
_HTML = re.compile(r"<[^>]+>")
_MARK = re.compile(r"«/?(?:B|I|SC)»")


def clean_title(raw: str) -> str:
    """Flatten a heading's wikitext to the plain title string."""
    t = _FN.sub("", raw)
    t = _REF.sub("", t)
    t = _REF_SELF.sub("", t)
    t = _ABBR.sub(r"\1", t)
    t = _LINK_PIPE.sub(r"\1", t)
    t = _LINK.sub(r"\1", t)
    # {{sc|…}} small-caps renders as CAPITALS; uppercase the content so a
    # fully small-capped headword (`{{sc|[[Author:…|Holland, Josiah Gilbert]]}}`)
    # reads as the all-caps title it is — not a Title-case taxonomy subsection.
    t = _SC.sub(lambda m: m.group(1).upper(), t)
    for _ in range(8):
        before = t
        t = _UC.sub(lambda m: m.group(1).upper(), t)
        t = _TMPL3.sub(r"\1", t)
        t = _TMPL2.sub(r"\1", t)
        t = _TMPL0.sub("", t)
        if t == before:
            break
    t = _HTML.sub("", t)
    t = _MARK.sub("", t)
    t = re.sub(r"\s+", " ", t).strip().rstrip(",.;:")
    return t


# A bold heading span, optionally wrapped in an [[Author:…|…]] link — consume the
# link's closing `]]` (after «/B») so clean_title's link-stripper can fire.
# `\s*` after the pipe: the «B» can sit on the next line (STAWELL).
_BOLD = re.compile(r"(?:\[\[[^\]|]*\|\s*)?«B».*?«/B»(?:\s*\]\])?", re.DOTALL)
_DROPINITIAL = re.compile(r"\{\{\s*drop\s*initial\s*\|\s*([^{}|]+)", re.I)
# [[Author:…|inner]] / [[Portal:…|inner]] link wrapper — unwrapped to its inner
# heading in the title_raw span so the downstream title_display transform keeps
# the bold run instead of mangling the link (HOLLAR, WENZEL or WENCESLAUS).
_AUTHORLINK = re.compile(
    r"\[\[(?:Author|Portal):[^\]|]*\|(.*?)\]\]", re.DOTALL | re.IGNORECASE)


def _is_connective_gap(gap: str) -> bool:
    """True if the gap between two bold spans is heading CONNECTIVE (a
    parenthetical alt-name / `,` / `and`·`or` / surname particle) rather than the
    descriptive body.  Keeps the title run going across same-line forename/joint
    bolds (BELLARMINE …, ROBERTO …; ABANA … and PHARPAR) but stops at the body."""
    cleaned = _MARK.sub("", _SC.sub(r"\1", gap)).strip()
    if len(cleaned) > 70:                      # a clause, not a connective
        return False
    if re.search(r"\(\s*[cbfl]?\.?\s*\d", gap):  # (1542– / (c. 1036 / (b. … = body date
        return False
    if not re.search(r"[(),;]|\b(?:and|or|surnamed|né|née|nee|called|alias)\b",
                     gap, re.I):
        return False
    return True


def _title_span(opening: str) -> tuple[str, str]:
    """Split a raw article opening into (title_span, rest).  The title is the
    heading RUN: the first `«B»` through the LAST `«B»` reachable across
    connective gaps; it stops where the descriptive body begins."""
    s = opening.lstrip()
    lead = len(opening) - len(s)
    m = _BOLD.match(s)
    if not m:
        return "", opening
    end = m.end()
    while True:
        nb = _BOLD.search(s, end)
        if not nb or not _is_connective_gap(s[end:nb.start()]):
            break
        end = nb.end()
    # If the last title-bold sat inside an open parenthetical (AMYNTAS II.
    # (or «B»III.«/B»)), the span stops at «/B» before the `)` — pull it in.
    span = opening[:lead + end]
    if span.count("(") > span.count(")"):
        cm = re.match(r"[^«(]*?\)", opening[lead + end:])
        if cm:
            end += cm.end()
    return opening[:lead + end], opening[lead + end:]


# Leading page-chrome the honest walker now carries into a first-segment opening
# (B3): the page-header `<noinclude>…</noinclude>`, a `<section …>` tag, running
# headers.  Skipped before the heading is sought — it is not the title.
_LEAD_CHROME = re.compile(
    r"^\s*(?:<noinclude>.*?</noinclude>"
    r"|<section\b[^>]*>"
    r"|\{\{\s*(?:EB1911 Page Heading|rh)\b[^{}]*\}\})+",
    re.DOTALL | re.IGNORECASE)


def produce_title(opening: str) -> tuple[str, str, str]:
    """Produce ``(plain_title, body, title_raw_span)`` from a raw article
    OPENING (the text beginning at the heading, as the walker hands it over).
    The producer owns the title↔body cut — the walker only set the boundary.

    ``title_raw_span`` is the raw heading span (markers/footnote intact) the
    downstream ``title_display`` transform needs to preserve italic/small-caps/
    footnote titles; "" when there is no bold heading (letter / fallback),
    where the plain title needs no formatted override."""
    opening = _LEAD_CHROME.sub("", opening)
    span, rest = _title_span(opening)
    if span:
        title = re.sub(r"\s+,", ",", clean_title(span)).strip()
        # title_raw (for title_display) keeps markers but unwraps the
        # [[Author:…|…]] link so the transform preserves the bold run.
        return title, rest.lstrip(" \t,."), _AUTHORLINK.sub(r"\1", span)
    # Letter articles open with a drop-cap, not a bold heading.
    dm = _DROPINITIAL.search(opening[:200])
    if dm:
        return dm.group(1).strip().upper(), opening, ""
    return clean_title(opening.split("\n", 1)[0]), opening, ""
