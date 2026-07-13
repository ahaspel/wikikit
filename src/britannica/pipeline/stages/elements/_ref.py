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


def _process_ref(raw, inner, ref_bodies=None):
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
    content = inner.strip()
    if name:
        return f"«FN[{name}]:{content}«/FN»"
    return f"«FN:{content}«/FN»"


_REF_TAGS_RE = re.compile(r"<ref(?:\s[^>]*)?>|</ref>", re.IGNORECASE | re.DOTALL)


def _produce_ref_body(ce, context) -> str:
    """Produce ONE ref/continuation body from the node the walker already classified —
    produce its child subtree, then substitute the child markers into its placeholderized
    ``inner_text``.  The decompose-native render of a footnote body: a ``<table>`` / styler /
    link inside is a real child node (``produce_tree`` owns it), NOT a re-``process_elements``
    of the raw body that re-walks + re-classifies it in a throwaway tree (the last producer-
    side flattener).  Producing the subtree here sets the same markers the main producer pass
    sets — idempotent — so calling it before that pass is safe.

    Byte-identical to the old ``process_elements`` recurse: the body text is the same (no ref
    nests in a ref) and it classifies the same way — the ref inner the walker already built."""
    from britannica.pipeline.stages.elements._classifier import (
        produce_tree, substitute_top_level_markers)
    if not ce.inner_registry:
        return ce.inner_text
    produce_tree(ce.inner_registry, context)
    return substitute_top_level_markers(ce.inner_text, ce.inner_registry)


def resolve_ref_bodies(tree, context=None) -> dict[str, str]:
    """Article-scoped resolution of named / continuation footnotes.

    ref and note are SPLIT by necessity: a ``<ref name=X/>`` reuse, the
    ``<ref name=X>body</ref>`` definition, and any ``<ref follow=X>…``
    continuation can each live anywhere in the article.  This reunites
    them into NAME → body.

    Each contributing body is produced from the node the walker already CLASSIFIED
    (``_produce_ref_body`` — produce its child subtree, substitute the markers) — a table /
    styler / link inside a named or ``follow`` footnote is a real nested element, not
    flattened prose.  This is the decompose the plain ``<ref>`` path already gets (its inner
    is classified and ``produce_tree`` substitutes the child markers); name/follow now render
    the SAME way, off the classified tree, instead of re-``process_elements``-ing the raw
    bodies in a throwaway tree (the last producer-side flattener; the earlier flatten dropped
    a ``<table>`` to ``<td …>`` debris — the MACHINE-GUN table-in-ref leak).
    [[project_walker_one_matcher]]

    Falls back to the raw bodies only when no ``context`` is supplied (production always
    passes one).
    """
    def _iter_refs(reg):
        # Named/follow ref collection is ARTICLE-WIDE: recurse every nesting level so a ref
        # inside a styler / table cell / figure is collected too (was top-level only — which,
        # once stylers became composites, dropped styler-nested follow/multi-part bodies and
        # left reuses resolving against a name the collector never saw).  Depth-first in
        # registry (document) order preserves multi-part concatenation order.  A REF's own
        # body is its concern (_produce_ref_body recurses it), so don't descend into one.
        for _ph, ce in reg.items():
            if ce.label == "REF":
                yield ce
            elif ce.inner_registry:
                yield from _iter_refs(ce.inner_registry)

    parts: dict[str, list] = {}
    for ce in _iter_refs(tree):
        name, follow = _ref_attrs(ce.raw)
        if not _REF_TAGS_RE.sub("", ce.raw).strip():   # empty body contributes nothing
            continue
        target = follow or name
        if not target:
            continue
        parts.setdefault(target, []).append(ce)
    resolved: dict[str, str] = {}
    for nm, nodes in parts.items():
        if context is not None:
            produced = [_produce_ref_body(ce, context) for ce in nodes]
        else:
            produced = [_REF_TAGS_RE.sub("", ce.raw).strip() for ce in nodes]
        resolved[nm] = " ".join(p.strip() for p in produced).strip()
    return resolved
