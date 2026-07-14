"""Per-article processing context threaded through element handlers.

Replaces the loose ``context: dict`` that used to be passed around.
Carries the small amount of cross-element state a handler may need:

  * ``volume`` — for score / chart-image lookups that key on physical location.
  * ``ref_bodies`` — name → resolved-footnote-body map, built once per
    article by ``resolve_ref_bodies`` and consumed by ``<ref name=X/>``
    anchors.
  * ``contributor_initials`` — normalized initials of every known
    contributor (front-matter + vol-29 index), loaded once by the caller.
    The Author-link producer routes on membership: a display whose initials
    are in here is a contributor signature → render the initials; everything
    else → «LN» (xref).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ElementContext:
    volume: int = 0
    ref_bodies: dict[str, str] | None = None
    contributor_initials: frozenset[str] = field(default_factory=frozenset)
    # PER-NODE (not article-constant): the label of the node this one hangs under,
    # threaded down by ``produce_tree`` on an immutable per-level copy.  Lets a
    # producer read its own parent — the BODY producer keys on it to tell a verse
    # line break (parent POEM/PPOEM → «BR») from a prose soft-wrap (→ space).
    parent_label: str | None = None
