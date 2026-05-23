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
    t = _SC.sub(r"\1", t)
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


def produce_title(raw: str, body_after: str) -> tuple[str, str]:
    """Produce ``(plain_title, comma-consumed body)`` from the leading heading
    and the text immediately after it."""
    title = re.sub(r"\s+,", ",", clean_title(raw)).strip()
    body = body_after.lstrip(" \t,.")
    return title, body
