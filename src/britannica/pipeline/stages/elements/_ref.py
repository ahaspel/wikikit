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


def resolve_ref_bodies(tree, context=None) -> dict[str, str]:
    """Article-scoped resolution of named / continuation footnotes.

    ref and note are SPLIT by necessity: a ``<ref name=X/>`` reuse, the
    ``<ref name=X>body</ref>`` definition, and any ``<ref follow=X>…``
    continuation can each live anywhere in the article.  This reunites
    them into NAME → body.

    Each contributing body is RECURSED to the ground through the element
    pipeline (``process_elements``) — a table / figure / poem inside a
    named or ``follow`` footnote becomes a real nested element, not
    flattened prose.  The plain ``<ref>`` path already recurses (its inner
    is classified and ``produce_tree`` substitutes the child markers); this
    brings name/follow into line, instead of flattening
    the concatenated RAW bodies (which dropped a ``<table>`` to ``<td …>``
    debris — the MACHINE-GUN table-in-ref leak).  [[project_walker_one_matcher]]

    Falls back to the old flatten only when no ``context`` is supplied
    (``process_elements`` needs it); production always passes one.
    """
    def _iter_refs(reg):
        # Named/follow ref collection is ARTICLE-WIDE: recurse every nesting level so a ref
        # inside a styler / table cell / figure is collected too (was top-level only — which,
        # once stylers became composites, dropped styler-nested follow/multi-part bodies and
        # left reuses resolving against a name the collector never saw).  Depth-first in
        # registry (document) order preserves multi-part concatenation order.  Yield REF
        # (definition / follow — carries a body) AND REF_SELF (`<ref name=X/>` reuse — an
        # anchor with no body, but the resolved subtree must still hang on it); a ref's own
        # body is its concern, so don't descend into one.
        for _ph, ce in reg.items():
            if ce.label in ("REF", "REF_SELF"):
                yield ce
            elif ce.inner_registry:
                yield from _iter_refs(ce.inner_registry)

    parts: dict[str, list[str]] = {}
    anchors: dict[str, list] = {}      # NAME → the REF nodes that emit «FN[NAME]:body
    for ce in _iter_refs(tree):
        name, follow = _ref_attrs(ce.raw)
        body = re.sub(
            r"<ref(?:\s[^>]*)?>|</ref>", "", ce.raw,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        target = follow or name
        if target and body:
            parts.setdefault(target, []).append(body)
        # A name-only ref — `<ref name=X>…</ref>` (definition) or `<ref name=X/>` (reuse) —
        # emits «FN[X]:body at its anchor; a `follow=` continuation emits nothing (its body
        # folds into X).  Collect the anchors so the resolved body SUBTREE hangs on each.
        if name and not follow:
            anchors.setdefault(name, []).append(ce)
    resolved: dict[str, str] = {}
    for nm, bodies in parts.items():
        if context is None:
            resolved[nm] = " ".join(bodies).strip()   # no-context fallback: raw concat
            continue
        # CLASSIFY (not flatten) each contributing body into real child NODES and hang the
        # merged registry on every «FN[X] anchor.  `produce_tree` then substitutes those
        # children into the placeholderized body, reproducing exactly the string the old
        # `process_elements` built (byte-identical) — while a table / verse / figure inside a
        # NAMED footnote is now a NODE `render_tree` can walk, not flattened prose.  The body
        # is shared by NAME (definition + continuations, N reuse anchors), so ONE subtree
        # registry hangs on every anchor.
        from britannica.pipeline.stages.elements._classifier import classify_article
        ph_parts: list[str] = []
        body_reg: dict = {}
        for b in bodies:
            body_ph, body_children = classify_article(b, _allow_figure=False)
            ph_parts.append(body_ph.strip())
            body_reg.update(body_children)
        resolved[nm] = " ".join(ph_parts).strip()
        for ce in anchors.get(nm, []):
            ce.inner_registry = body_reg
    return resolved
