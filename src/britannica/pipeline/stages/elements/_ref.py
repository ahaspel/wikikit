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
    # Produce the body the same way the main body is produced — keep its
    # markers («I»/«B»/«SC»/«LN»/IMG/nested-element placeholders), which
    # produce_tree substitutes afterwards.  No _clean_text flatten: that
    # stripped formatting and mangled rendered markers (the footnote
    # producer must own its body, not delegate to a generic flattener).
    from britannica.pipeline.stages.transform_articles.body_text import (
        strip_known_wrapper_tags,
    )
    content = strip_known_wrapper_tags(text_transform(inner)).strip()
    if name:
        return f"«FN[{name}]:{content}«/FN»"
    return f"«FN:{content}«/FN»"


def resolve_ref_bodies(tree, text_transform) -> dict[str, str]:
    """Article-scoped resolution of named / continuation footnotes.

    ref and note are SPLIT by necessity: a ``<ref name=X/>`` reuse, the
    ``<ref name=X>body</ref>`` definition, and any ``<ref follow=X>…``
    continuation can each live anywhere in the article.  This reunites
    them into NAME → body.  It is footnote-owned (it used to live in the
    classifier) and, like the producer, KEEPS the body's markers — no
    flatten — so footnote formatting / links / glyphs survive.

    Walks the tree's top-level entries only — under today's walker, refs
    inside poems / tables surface as flat top-level extracts, so a
    top-level scan suffices.
    """
    parts: dict[str, list[str]] = {}
    for _ph, ce in tree.items():
        if ce.label != "REF":
            continue
        name, follow = _ref_attrs(ce.raw)
        body = re.sub(
            r"<ref(?:\s[^>]*)?>|</ref>", "", ce.raw,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        if not body:
            continue
        target = follow or name
        if not target:
            continue
        parts.setdefault(target, []).append(body)
    resolved: dict[str, str] = {}
    for nm, bodies in parts.items():
        resolved[nm] = text_transform(" ".join(bodies)).strip()
    return resolved
