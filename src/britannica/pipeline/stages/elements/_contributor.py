"""Contributor-signature producers.

The `{{EB1911 footer …}}` family is recognized by the walker as a
CONTRIBUTOR_FOOTER node.  Here we render ONLY what a reader sees — the
right-aligned initials signoff.  The byline binding is harvested separately
by extract_contributors off the same initials (resolved through the vol-29 /
front-matter index), so this produces the initials and nothing else: no name,
no marker, no link.
"""
from __future__ import annotations


def _process_contributor_footer(raw: str) -> str:
    """Render a `{{EB1911 footer (double) initials|…}}` to its faithful
    right-aligned initials signoff, reusing the shared footer parse.

    `{{EB1911 footer initials|Frank Richardson Cana|F. R. C.}}`
        → `«DIV[style:float:right]»(F. R. C.)«/DIV»`
    The double variant joins its contributors with `;` inside the parens,
    matching the manual `{{float right|(E. He.; F. R. C.)}}` signature shape.
    """
    from britannica.pipeline.stages.extract_contributors import (
        _FOOTER_PATTERN, _parse_contributors)
    m = _FOOTER_PATTERN.search(raw)
    if not m:
        return ""  # footer template with no parseable initials
    inits = [c["initials"] for c in _parse_contributors(m.group(1))]
    if not inits:
        return ""
    return f"«DIV[style:float:right]»({'; '.join(inits)})«/DIV»"
