"""<ref>...</ref> footnote handling.

Three forms:
- ``<ref>body</ref>`` — plain footnote
- ``<ref name=X>body</ref>`` / ``<ref name=X/>`` — defined + reused
- ``<ref follow=X>body</ref>`` — continuation merged into name X

Anchors emit ``«FN[name]:body«/FN»`` or ``«FN:body«/FN»`` markers.
The article-level ``resolve_ref_bodies`` (in `_classifier`) runs
once per article to fold continuations.
"""

from __future__ import annotations

import re

from britannica.pipeline.stages.elements._text import _clean_text


_REF_NAME_ATTR_RE = re.compile(r'\bname\s*=\s*"?([^"\s/>]+)"?', re.IGNORECASE)
_REF_FOLLOW_ATTR_RE = re.compile(r'\bfollow\s*=\s*"?([^"\s/>]+)"?', re.IGNORECASE)
_REF_OPEN_RE = re.compile(r"<ref(?:\s+([^>/]*?))?\s*/?>", re.IGNORECASE)


def _ref_attrs(raw: str) -> tuple[str | None, str | None]:
    """Return (name, follow) parsed from a ``<ref ...>`` opener; either may be None."""
    m = _REF_OPEN_RE.match(raw)
    if not m or not m.group(1):
        return None, None
    attrs = m.group(1)
    nm = _REF_NAME_ATTR_RE.search(attrs)
    fl = _REF_FOLLOW_ATTR_RE.search(attrs)
    return (nm.group(1) if nm else None,
            fl.group(1) if fl else None)


def _process_ref_self(raw: str, ref_bodies):
    """Render ``<ref name=X/>`` self-closing reuse as a FN anchor.

    The body comes from the article-level ref-name registry built by
    ``resolve_ref_bodies`` (folds in any ``<ref follow=X>body</ref>``
    continuation appearing elsewhere in the same article). Anchors
    that don't resolve drop silently.

    Emits ``«FN[NAME]:…«/FN»`` so the viewer can group all anchors for
    NAME under a single footnote number (Wikisource convention: one
    note in the footer list, N superscripts in the body).
    """
    name, _ = _ref_attrs(raw)
    if name and ref_bodies and name in ref_bodies:
        return f"«FN[{name}]:{ref_bodies[name]}«/FN»"
    return ""


def _process_ref(raw, inner, text_transform, ref_bodies=None):
    """Convert ``<ref ...>body</ref>`` to a FN anchor with clean text.

    ``<ref follow=NAME>body</ref>`` continuations emit nothing at the
    call site; their body is folded into ``ref_bodies[NAME]`` by
    ``resolve_ref_bodies`` in `_classifier`. ``<ref name=NAME>body</ref>``
    emits an anchor whose text is the resolved body for NAME (so an
    earlier ``<ref name=NAME/>`` reuse and any later ``follow=NAME``
    continuation are merged into one footnote). Plain ``<ref>``
    without name/follow keeps the legacy single-body behavior.
    """
    name, follow = _ref_attrs(raw)
    if follow:
        return ""
    if name and ref_bodies and name in ref_bodies:
        return f"«FN[{name}]:{ref_bodies[name]}«/FN»"
    content = text_transform(inner)
    content = _clean_text(content)
    if name:
        return f"«FN[{name}]:{content}«/FN»"
    return f"«FN:{content}«/FN»"
