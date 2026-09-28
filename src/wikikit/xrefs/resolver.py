"""The shared core of every name→article index.

The old resolution cascade that lived here (``build_index`` / ``resolve`` /
``disambiguate_among`` — exact-title dict, display-parenthetical hints, LLM
disambiguation cache) is RETIRED: article xrefs now resolve through the one
``wikikit.link_resolver.LinkResolver`` (fill + prose-fish), the same
machinery as the classified-TOC topics — docs/xref_resolution_strategy.md,
[[project_resolver_consolidation]].  What remains is the one primitive every
index builder shares.
"""
from __future__ import annotations

from wikikit.xrefs.normalizer import normalize_xref_target


def build_core_maps(items, *, value_of):
    """The shared core of every name→article index: a collision-keeping
    ``normalize_xref_target(title) → [candidate]`` map, plus a first-wins
    ``… → value_of(candidate)`` map for the fuzzy matcher.

    ``candidate`` is opaque — a DB ``Article`` (pipeline paths) or an
    ``(filename, title)`` tuple (the tool-side topic / reader's-guide / linker
    paths that read the exported ``index.json``).  So every caller shares ONE
    keying + collision bucketing and differs only in what it hangs off each
    bucket.
    """
    by_norm: dict[str, list] = {}
    tmap: dict = {}
    for title, cand in items:
        k = normalize_xref_target(title or "")
        if not k:
            continue
        by_norm.setdefault(k, []).append(cand)
        tmap.setdefault(k, value_of(cand))
    return by_norm, tmap
