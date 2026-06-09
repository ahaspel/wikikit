"""Contributor-signature producers.

The `{{EB1911 footer …}}` family AND the bare-initials sign-off shortcut
(`{{EB1911 TAs}}` → Thomas Ashby's "(T. As.)") are recognized by the walker as a
CONTRIBUTOR_FOOTER node.  Here we render ONLY what a reader sees — the
right-aligned initials signoff.  The byline binding is harvested separately by
extract_contributors off the same initials (resolved through the vol-29 /
front-matter index), so this produces the initials and nothing else: no name,
no marker, no link.
"""
from __future__ import annotations

import re

# `{{EB1911 TAs}}` — the squashed-initials sign-off SHORTCUT (`TAs` = Thomas
# Ashby's "(T. As.)"), the bare twin of `{{EB1911 footer initials|…}}`.  Capital-led
# + immediately closed is what separates it from the lowercase-word EB1911 templates
# (footer / article link / fine print / coordinates).
_SHORTCUT_RE = re.compile(r"\{\{\s*[Ee][Bb]1911\s+([A-Z][A-Za-z*.\-]{0,5})\s*\}\}")


def _expand_initials_shortcut(token: str) -> str:
    """`TAs` → `T. As.`: split the squashed initials on capital boundaries, dot
    each, and keep a trailing disambiguation `*`.  Clean for the common forms;
    hyphenated / oddly-cased shortcuts (JF-K, JHlR) mis-split — the queued tail."""
    parts = re.findall(r"[A-Z][a-z]*", token)
    if not parts:
        return ""
    star = "*" if token.rstrip().endswith("*") else ""
    return " ".join(f"{p}." for p in parts) + star


def _process_contributor_footer(raw: str) -> str:
    """Render a contributor sign-off to its faithful right-aligned initials.

    Full footer: `{{EB1911 footer initials|Frank Richardson Cana|F. R. C.}}`
        → `«DIV[style:float:right]»(F. R. C.)«/DIV»` (double variant joins with `;`).
    Shortcut:    `{{EB1911 TAs}}` → `«DIV[style:float:right]»(T. As.)«/DIV»`.

    Either way the harvest binds the contributor off the rendered initials via the
    index, so this emits the initials and nothing else.
    """
    from britannica.pipeline.stages.extract_contributors import (
        _FOOTER_PATTERN, _parse_contributors)
    m = _FOOTER_PATTERN.search(raw)
    if m:
        inits = [c["initials"] for c in _parse_contributors(m.group(1))]
        return f"«DIV[style:float:right]»({'; '.join(inits)})«/DIV»" if inits else ""
    sm = _SHORTCUT_RE.match(raw.strip())
    if sm:
        inits = _expand_initials_shortcut(sm.group(1))
        return f"«DIV[style:float:right]»({inits})«/DIV»" if inits else ""
    return ""
